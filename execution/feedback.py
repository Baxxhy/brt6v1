"""Main feedback loop for BRT6."""

from __future__ import annotations

import copy
import json
import fcntl
import os
import shlex
import shutil
import subprocess
import traceback
import re
from pathlib import Path
from typing import Any

from ..execution.executor import run_command_in_conda, run_subprocess_tree
from ..execution.delta_loop import (
    repeated_keep,
    semantic_round_budget,
    static_failure_execution,
)
from ..generation.generator import (
    format_effective_source_context,
    generate_candidate,
    materialize_current_test,
)
from ..context.host_context import build_host_context, rank_related_tests, select_related_test
from ..context.protocol_recovery import audit_recovered_protocol, recover_test_protocol
from ..mutation.seed_mutator import propose_semantic_delta
from ..validation.strict_semantic_verifier import verify_strict_semantics
from ..retrieval.icore_runtime import (
    dump_spec,
    ensure_icore_environment,
    ensure_isolated_runtime_environment,
    env_name_for,
    env_lock_path,
    icore_setup_command,
    icore_test_command,
    first_test_selector,
    make_instance_spec,
)
from ..runtime.conda_env_manager import environment_manifest
from ..core.behavior_evidence import (
    BehaviorEvidence,
    behavior_target_payload,
    is_behavior_target,
    raw_issue_payload,
)
from ..core.ablation import AblationConfig
from ..core.schema import (
    CandidateCheckpoint,
    DualVersionResult,
    ExecutionResult,
    FinalResult,
    HostContext,
    InstanceContext,
    RawIssueContext,
    SemanticDelta,
    VerifierDecision,
)
from ..core.utils import ensure_dir, safe_json_dump, write_text
from ..validation.verifier import verify_buggy_only
from ..validation.oracle_risk import assess_oracle_risk
from ..validation.semantic_guard import oracle_contract_summary
from ..validation.delta_guard import check_candidate


def _load_cached_behavior(context: InstanceContext, output_dir: str) -> Any:
    from ..issue.issue_rewriter import behavior_from_dict
    from ..core.utils import safe_json_load

    local_path = Path(output_dir) / "behavior_target.json"
    raw_roots = os.environ.get("BRT4_BEHAVIOR_CACHE_DIR", "")
    cache_roots = [
        Path(item)
        for item in raw_roots.split(os.pathsep)
        if item.strip()
    ]
    candidates = [local_path]
    candidates.extend(
        root / context.instance_id / "behavior_target.json"
        for root in cache_roots
    )
    for path in candidates:
        if path.is_file():
            behavior = behavior_from_dict(context.instance_id, safe_json_load(path))
            from ..issue.issue_rewriter import apply_behavior_safety_constraints
            behavior = apply_behavior_safety_constraints(context.issue_text, behavior)
            if path != local_path:
                behavior.save_json(str(local_path))
            return behavior
    raise FileNotFoundError(
        "missing cached behavior_target.json; generation is configured not to "
        f"rerun issue rewrite for {context.instance_id}. Checked: "
        + ", ".join(str(path) for path in candidates)
        + ". Use the full launcher without --behavior-target-cache to regenerate "
        "IssueRewrite, or pass an explicit validated cache."
    )


def _load_behavior_evidence(
    context: InstanceContext,
    output_dir: str,
    enable_behavior_target: bool,
) -> BehaviorEvidence:
    """Load structured behavior or create the raw-Issue ablation input.

    The disabled branch intentionally does not call ``_load_cached_behavior``
    and therefore cannot import IssueRewrite or consume a stale cache.
    """
    if enable_behavior_target:
        return _load_cached_behavior(context, output_dir)
    evidence = RawIssueContext(context.instance_id, context.issue_text)
    evidence.save_json(str(Path(output_dir) / "raw_issue_context.json"))
    safe_json_dump(
        {
            "method_variant": "w/o Behavior Target",
            "behavior_target_enabled": False,
            "issue_rewrite_invoked": False,
            "structured_fields_present": False,
            "raw_issue_context": evidence.to_dict(),
            "retrieved_code": [item.to_dict() for item in context.retrieved_code],
            "retrieved_tests": [item.to_dict() for item in context.retrieved_tests],
            "control_contract": {
                "seed_order": "unchanged_iCoRe_order",
                "protocol_recovery": "unchanged",
                "mutation_and_repair_budgets": "unchanged",
                "candidate_ranking": "unchanged",
                "formal_evaluation": "unchanged",
            },
        },
        str(Path(output_dir) / "behavior_ablation_manifest.json"),
    )
    return evidence


def _save_behavior_evidence(evidence: BehaviorEvidence, output_dir: str | Path) -> None:
    filename = (
        "behavior_target.json"
        if is_behavior_target(evidence)
        else "raw_issue_context.json"
    )
    evidence.save_json(str(Path(output_dir) / filename))


def _method_version(config: AblationConfig) -> str:
    if config.ablation_id == "full":
        return "p0-lossless-target-direct-fallback-v5"
    if config.ablation_id == "wo_behavior_target":
        return "p0-wo-behavior-target-v1"
    if config.ablation_id == "wo_mutation":
        return "p0-wo-explicit-mutation-planning-v1"
    return f"p0-{config.ablation_id.replace('_', '-')}-v1"


def _uses_adaptive_seed_pipelines(
    config: AblationConfig,
    *,
    adaptive_disabled: bool,
    generate_only: bool,
    protocol_recovery_enabled: bool,
    forced_seed_index: int | None,
) -> bool:
    """Whether Top-3 seeds should be executed as separate pipelines."""

    _ = config
    return bool(
        not adaptive_disabled
        and not generate_only
        and protocol_recovery_enabled
        and forced_seed_index is None
    )


def _empty_repair_route_counts() -> dict[str, int]:
    return {
        "dependency_recovery": 0,
        "environment": 0,
        "trigger": 0,
        "assertion": 0,
        "generic": 0,
    }


def _should_run_direct_fallback(
    config: AblationConfig,
    attempts: list[dict[str, Any]],
) -> bool:
    """Run the direct route only after the full target-guided route exhausts.

    The fallback is part of the full method rather than any one-factor
    ablation.  A merely failing candidate is insufficient: the strict route
    must have accepted it and exported an actual test file.
    """

    if config.ablation_id != "full":
        return False
    return not any(
        str(item.get("status") or "") == "ISSUE_ALIGNED_FAIL"
        and Path(str(item.get("final_test_path") or "")).is_file()
        for item in attempts
    )


def _selected_protocol_result_fields(
    selected_summary: dict[str, Any],
) -> dict[str, Any]:
    """Preserve the selected seed's executable protocol at adaptive Top-3.

    The recursive seed pipeline writes the authoritative placement, selector,
    runner command, and validation switches.  Dropping those fields while
    constructing the outer ``FinalResult`` makes formal evaluation guess a new
    runner protocol instead of auditing/replaying the selected candidate.
    """

    return {
        "protocol_recovery_enabled": bool(
            selected_summary.get("protocol_recovery_enabled")
        ),
        "seed_mutation_enabled": bool(
            selected_summary.get("seed_mutation_enabled")
        ),
        "strict_verifier_enabled": bool(
            selected_summary.get("strict_verifier_enabled")
        ),
        "selected_seed_file": str(
            selected_summary.get("selected_seed_file") or ""
        ),
        "selected_seed_name": str(
            selected_summary.get("selected_seed_name") or ""
        ),
        "seed_fallback_used": bool(
            selected_summary.get("seed_fallback_used")
        ),
        "oracle_type": str(selected_summary.get("oracle_type") or ""),
        "strict_verifier_decision": str(
            selected_summary.get("strict_verifier_decision") or ""
        ),
        "strict_failure_class": str(
            selected_summary.get("strict_failure_class") or ""
        ),
        "candidate_repo_path": str(
            selected_summary.get("candidate_repo_path") or ""
        ),
        "pytest_nodeid": str(selected_summary.get("pytest_nodeid") or ""),
        "command": str(selected_summary.get("command") or ""),
        "direct_test_repo_path_hint": str(
            selected_summary.get("direct_test_repo_path_hint") or ""
        ),
        "placement_dir": str(selected_summary.get("placement_dir") or ""),
        "runner_kind": str(selected_summary.get("runner_kind") or ""),
        "selector": str(selected_summary.get("selector") or ""),
    }


def _repair_focus(
    decision: VerifierDecision,
    strict_result: Any | None,
    execution: ExecutionResult,
) -> str:
    """Normalize semantic routing from mechanics plus failure class.

    In particular, a generic ``reject`` must not silently become Trigger
    feedback when the verifier's failure class says the Oracle or setup is the
    actual problem.
    """

    if execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
        return "setup"
    explicit = {
        "repair_setup": "setup",
        "repair_trigger": "trigger",
        "repair_oracle": "oracle",
    }.get(decision.decision)
    if explicit:
        return explicit
    failure_class = str(getattr(strict_result, "failure_class", "") or "")
    if failure_class in {"setup", "syntax", "collect"}:
        return "setup"
    if failure_class in {"oracle_wrong", "oracle_too_strong"}:
        return "oracle"
    if failure_class == "side_path":
        # A wrong observation protocol is an Oracle problem even when the
        # verifier labels the resulting failure as a side path.  This matters
        # for non-assert Oracles such as logger namespace, warning category,
        # exception type, matcher, or snapshot selection.
        feedback_text = " ".join(
            (
                str(decision.reason or ""),
                str(decision.next_action or ""),
                str(getattr(strict_result, "reason", "") or ""),
            )
        ).lower()
        oracle_markers = (
            "oracle",
            "assert",
            "logger",
            "logging",
            "日志",
            "warning",
            "warns",
            "警告",
            "raises",
            "exception type",
            "异常类型",
            "matcher",
            "snapshot",
            "expected value",
            "期望值",
        )
        if any(marker in feedback_text for marker in oracle_markers):
            return "oracle"
        return "trigger"
    if failure_class in {"buggy_pass", "target_not_hit"}:
        return "trigger"
    return "reject"


def _seed_residual_state(host: HostContext) -> dict[str, Any]:
    """Use the existing seed run only as evidence that the test host works."""

    status = host.seed_execution_status
    host_ready = status in {"PASS", "ISSUE_ALIGNED_FAIL", "ASSERTION_FAIL"}
    return {
        "source": "seed",
        "round_id": "seed",
        "stage": "TRIGGER" if host_ready else "SETUP",
        "depth": 1 if host_ready else 0,
        "host_ready": host_ready,
        "trigger_satisfied": None,
        "next_gap": "TRIGGER" if host_ready else "SETUP",
        "semantic_status": "UNKNOWN_NOT_VERIFIED",
    }


def _candidate_residual_state(
    round_id: int,
    execution: ExecutionResult,
    decision: VerifierDecision,
    strict_result: Any | None,
) -> dict[str, Any]:
    """Map the current execution verdict to the next semantic gap."""

    focus = _repair_focus(decision, strict_result, execution)
    if decision.decision == "accept":
        stage = "SEARCH_ACCEPTED"
    elif focus == "setup":
        stage = "SETUP"
    elif focus == "trigger":
        stage = "TRIGGER"
    elif focus == "oracle":
        stage = "ORACLE"
    else:
        stage = "TERMINAL"
    depth = {
        "TERMINAL": -1,
        "SETUP": 0,
        "TRIGGER": 1,
        "ORACLE": 2,
        "SEARCH_ACCEPTED": 3,
    }[stage]
    strict_evidence = strict_result.to_dict() if strict_result else {}
    return {
        "source": "candidate",
        "round_id": round_id,
        "stage": stage,
        "depth": depth,
        "observed_behavior": str(
            getattr(strict_result, "observed_behavior", "") or execution.status
        ),
        "target_behavior": str(
            getattr(strict_result, "target_behavior", "") or ""
        ),
        "gap": str(
            getattr(strict_result, "semantic_gap", "") or stage
        ),
        "preserve": list(getattr(strict_result, "preserve", []) or []),
        "change": list(getattr(strict_result, "change", []) or []),
        "avoid": list(getattr(strict_result, "avoid", []) or []),
        "operator": str(getattr(strict_result, "next_operator", "") or ""),
        "expected_effect": str(
            getattr(strict_result, "expected_effect", "") or ""
        ),
        "next_gap": (
            stage if stage in {"SETUP", "TRIGGER", "ORACLE"} else None
        ),
        "evidence": {
            "execution_status": execution.status,
            "verifier_decision": decision.decision,
            "failure_origin": str(
                getattr(strict_result, "failure_origin", "") or ""
            ),
            "post_fix_failure_risk": str(
                getattr(strict_result, "post_fix_failure_risk", "unknown")
                or "unknown"
            ),
            **strict_evidence,
        },
    }


def _residual_transition(
    parent: dict[str, Any],
    child: dict[str, Any],
    *,
    initialization: bool = False,
) -> dict[str, Any]:
    """Describe whether one test transformation closed the current gap."""

    parent_depth = int(parent.get("depth", 0))
    child_depth = int(child.get("depth", 0))
    if initialization:
        relation = "INITIALIZED"
    elif child.get("stage") == "SEARCH_ACCEPTED":
        relation = "SEARCH_ACCEPTED"
    elif child_depth > parent_depth:
        relation = "IMPROVED"
    elif child_depth < parent_depth:
        relation = "REGRESSED"
    else:
        relation = "STAGNANT"
    return {
        "parent_round": parent.get("round_id"),
        "child_round": child.get("round_id"),
        "parent_stage": parent.get("stage"),
        "child_stage": child.get("stage"),
        "depth_delta": child_depth - parent_depth,
        "relation": relation,
        "closed_gap": (
            parent.get("next_gap")
            if relation in {"IMPROVED", "SEARCH_ACCEPTED"}
            else None
        ),
        "remaining_gap": child.get("next_gap"),
        "initialization": initialization,
    }


def _residual_instruction(
    state: dict[str, Any],
    transition: dict[str, Any],
) -> str:
    """Turn the residual state into one concise edit objective."""

    stage = str(state.get("stage") or "")
    instructions = {
        "SETUP": "Fix the test setup and preserve the intended trigger and oracle.",
        "TRIGGER": (
            "The target behavior is not yet confirmed. Preserve the working "
            "setup and oracle; change the input, state, or call sequence."
        ),
        "ORACLE": (
            "The target behavior is reached. Preserve setup and trigger. "
            "Change only the public, falsifiable oracle."
        ),
        "SEARCH_ACCEPTED": "The current search has an accepted issue-aligned candidate.",
        "TERMINAL": "No supported repair route remains for this candidate.",
    }
    instruction = instructions.get(stage, "")
    relation = transition.get("relation")
    if relation == "REGRESSED":
        instruction = (
            "The last edit regressed the test. Restore the previously satisfied "
            f"{transition.get('parent_stage')} stage. {instruction}"
        )
    elif relation == "STAGNANT":
        instruction = (
            "The last edit did not close the current gap. Use a materially "
            "different semantic operator and do not repeat the previous change. "
            f"Previous operator: {state.get('operator') or 'unknown'}. {instruction}"
        )
    return instruction


def _delta_result_fields(
    deltas: list[Any],
    candidate: Any | None = None,
) -> dict[str, Any]:
    statuses = [str(getattr(delta, "status", "")) for delta in deltas]
    actions = [str(getattr(delta, "action", "")) for delta in deltas]
    return {
        "delta_calls": len(deltas),
        "valid_delta_calls": statuses.count("VALID"),
        "keep_delta_calls": actions.count("KEEP"),
        "final_semantic_delta": dict(getattr(candidate, "semantic_delta", {}) or {}),
        "delta_history": list(getattr(candidate, "delta_history", []) or []),
        "final_delta_application": dict(
            getattr(candidate, "delta_application", {}) or {}
        ),
    }


def _propose_delta_safely(
    instance_id: str,
    round_id: int,
    output_dir: str,
    *args: Any,
    **kwargs: Any,
) -> SemanticDelta:
    """Keep a planner implementation/service failure local to one seed."""

    try:
        return propose_semantic_delta(instance_id, round_id, *args, output_dir=output_dir, **kwargs)
    except Exception as exc:  # noqa: BLE001
        delta = SemanticDelta(
            instance_id=instance_id,
            round_id=round_id,
            status="INVALID",
            reason="planner failed at pipeline boundary",
            errors=[str(exc)],
        )
        delta.save_json(str(Path(output_dir) / f"delta_round_{round_id}.json"))
        return delta


def _evidence_result_fields(
    evidence: BehaviorEvidence,
    ablation_config: AblationConfig | None = None,
) -> dict[str, Any]:
    enabled = is_behavior_target(evidence)
    config = (
        ablation_config
        or AblationConfig(behavior_target=enabled)
    ).validate()
    return {
        "behavior_target": behavior_target_payload(evidence),
        "raw_issue_context": raw_issue_payload(evidence),
        "behavior_target_enabled": enabled,
        "method_variant": config.method_variant,
        "method_version": _method_version(config),
        "ablation_id": config.ablation_id,
        "ablation_signature": config.signature,
        "ablation_config": config.to_dict(),
        "behavior_schema_version": evidence.schema_version,
    }


def _run_local(command: str, cwd: str, timeout: int = 300) -> dict[str, Any]:
    try:
        proc = run_subprocess_tree(
            command,
            cwd,
            timeout,
            shell=True,
            executable="/bin/bash",
        )
        return {
            "command": command,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "cwd": cwd,
            "returncode": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace"),
            "stderr": exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace"),
            "timeout": True,
        }


def _refresh_candidate_command(context: InstanceContext, candidate: Any) -> None:
    candidate.command = icore_test_command(
        context.repo,
        str(context.metadata.get("version") or ""),
        candidate.candidate_repo_path,
        first_test_selector(candidate.code),
    )


def _checkpoint_score(
    execution: ExecutionResult,
    decision: VerifierDecision,
    strict_result: Any | None,
) -> tuple[int, str]:
    executable_fail = execution.returncode != 0 and execution.status not in {
        "SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT",
    }
    issue_aligned = bool(
        strict_result and strict_result.failure_class == "issue_aligned"
    )
    target_hit = bool(strict_result and strict_result.target_hit)
    grounded = bool(strict_result and strict_result.oracle_grounded_in_issue)
    public = bool(strict_result and strict_result.uses_public_behavior)
    if decision.decision == "accept" and executable_fail:
        score = 600
        score += 40 if target_hit else 0
        score += 20 if grounded else 0
        score += 10 if public else 0
        return score, "buggy fail accepted by full-Issue LLM semantic verifier"
    if issue_aligned and executable_fail:
        return 300, "LLM classified the buggy failure as issue-aligned"
    if executable_fail:
        return 100, "executable buggy failure not accepted as issue-aligned"
    if execution.returncode == 0:
        return 10, "test passes on buggy source"
    return 0, f"non-executable candidate: {execution.status}"


def _strict_no_exception_contract(
    decision: VerifierDecision,
    strict_result: Any | None,
) -> bool:
    """Recognize a fully verified public no-exception Oracle."""

    oracle_kinds = {
        item.strip().upper()
        for item in str(
            getattr(strict_result, "oracle_kind", "") or ""
        ).split(",")
        if item.strip()
    }
    return bool(
        decision.decision == "accept"
        and strict_result is not None
        and getattr(strict_result, "failure_class", "") == "issue_aligned"
        and getattr(strict_result, "target_hit", False)
        and getattr(strict_result, "oracle_grounded_in_issue", False)
        and getattr(strict_result, "uses_public_behavior", False)
        and getattr(strict_result, "oracle_falsifiable", False)
        and "NO_EXCEPTION" in oracle_kinds
    )


def _semantic_feedback_payload(
    decision: VerifierDecision,
    strict_result: Any | None,
    current_residual: dict[str, Any] | None = None,
    transition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preserve strict semantic fields for the specialized repair stage."""

    payload = decision.to_dict()
    if strict_result is not None:
        payload.update(
            {
                "failure_class": str(
                    getattr(strict_result, "failure_class", "") or ""
                ),
                "target_hit": bool(
                    getattr(strict_result, "target_hit", False)
                ),
                "oracle_grounded_in_issue": bool(
                    getattr(strict_result, "oracle_grounded_in_issue", False)
                ),
                "uses_public_behavior": bool(
                    getattr(strict_result, "uses_public_behavior", False)
                ),
                "oracle_kind": str(
                    getattr(strict_result, "oracle_kind", "") or ""
                ),
                "oracle_falsifiable": bool(
                    getattr(strict_result, "oracle_falsifiable", False)
                ),
                "observed_behavior": str(getattr(strict_result, "observed_behavior", "") or ""),
                "target_behavior": str(getattr(strict_result, "target_behavior", "") or ""),
                "semantic_gap": str(getattr(strict_result, "semantic_gap", "") or ""),
                "preserve": list(getattr(strict_result, "preserve", []) or []),
                "change": list(getattr(strict_result, "change", []) or []),
                "avoid": list(getattr(strict_result, "avoid", []) or []),
                "next_operator": str(getattr(strict_result, "next_operator", "") or ""),
                "expected_effect": str(getattr(strict_result, "expected_effect", "") or ""),
                "failure_origin": str(getattr(strict_result, "failure_origin", "") or ""),
                "post_fix_failure_risk": str(getattr(strict_result, "post_fix_failure_risk", "unknown") or "unknown"),
            }
        )
    if current_residual is not None and transition is not None:
        payload["residual_feedback"] = {
            "current_state": current_residual,
            "last_transition": transition,
            "next_gap": current_residual.get("next_gap"),
            "instruction": _residual_instruction(
                current_residual, transition
            ),
        }
    return payload


def _save_residual_trace(
    output_dir: str,
    instance_id: str,
    host: HostContext,
    rounds: list[dict[str, Any]],
    selected_round: int | None = None,
) -> None:
    """Save the small state trajectory used by the residual search."""

    selected = rounds[-1]
    if selected_round is not None:
        selected = next(
            item for item in rounds if item["round_id"] == selected_round
        )
    relations = [
        str(item.get("transition", {}).get("relation") or "")
        for item in rounds
    ]
    safe_json_dump(
        {
            "instance_id": instance_id,
            "seed": {
                "file": host.host_file,
                "name": host.seed_test_name,
                "execution_status": host.seed_execution_status,
                "state": _seed_residual_state(host),
            },
            "rounds": rounds,
            "final": {
                "round_id": selected["round_id"],
                "stage": selected["state"]["stage"],
                "depth": selected["state"]["depth"],
            },
            "counts": {
                relation.lower(): relations.count(relation)
                for relation in (
                    "IMPROVED",
                    "STAGNANT",
                    "REGRESSED",
                    "SEARCH_ACCEPTED",
                )
            },
        },
        str(Path(output_dir) / "residual_trace.json"),
    )


def _save_checkpoint(
    output_dir: str,
    attempt_id: int,
    candidate: Any,
    execution: ExecutionResult,
    decision: VerifierDecision,
    dual: DualVersionResult | None,
    behavior: Any | None = None,
    issue_text: str = "",
    retrieved_paths: set[str] | None = None,
    strict_result: Any | None = None,
    residual_state: dict[str, Any] | None = None,
    residual_transition: dict[str, Any] | None = None,
) -> CandidateCheckpoint:
    del retrieved_paths
    checkpoint_dir = ensure_dir(Path(output_dir) / "checkpoints")
    code_path = str(Path(checkpoint_dir) / f"candidate_attempt_{attempt_id}.py")
    write_text(code_path, candidate.code)
    score, reason = _checkpoint_score(execution, decision, strict_result)
    accepted = decision.decision == "accept"
    issue_aligned = bool(
        strict_result and strict_result.failure_class == "issue_aligned"
    )
    target_hit = bool(strict_result and strict_result.target_hit)
    grounded = bool(strict_result and strict_result.oracle_grounded_in_issue)
    public = bool(strict_result and strict_result.uses_public_behavior)
    oracle_contract_preserved = bool(
        getattr(candidate, "oracle_contract_preserved", True)
    )
    oracle_contract_violation = str(
        getattr(candidate, "oracle_contract_violation", "") or ""
    )
    executable_fail = execution.returncode != 0 and execution.status not in {
        "SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT",
    }
    oracle_contract = (
        oracle_contract_summary(behavior, candidate.code) if behavior else {}
    )
    oracle_risk = (
        assess_oracle_risk(
            candidate.code,
            behavior,
            issue_text=issue_text,
            execution_log=execution.stdout + "\n" + execution.stderr,
        )
        if behavior
        else {}
    )
    strict_no_exception = _strict_no_exception_contract(
        decision, strict_result
    )
    effective_oracle_kinds = list(oracle_contract.get("kinds") or [])
    if strict_no_exception and "NO_EXCEPTION" not in effective_oracle_kinds:
        effective_oracle_kinds.append("NO_EXCEPTION")
    hard_eligible = bool(
        executable_fail
        and oracle_contract_preserved
        and (
            bool(oracle_contract.get("falsifiable"))
            or strict_no_exception
        )
    )
    post_fix_risk = str(
        getattr(strict_result, "post_fix_failure_risk", "unknown") or "unknown"
    ).lower()
    risk_preference = {"low": 3, "unknown": 2, "medium": 1, "high": 0}.get(
        post_fix_risk, 1
    )
    rank_key = [
        int(hard_eligible),
        int(accepted),
        int(issue_aligned),
        int(target_hit),
        int(grounded),
        int(public),
        int(executable_fail),
        risk_preference,
        1,
        int(oracle_contract_preserved),
        int((oracle_risk or {}).get("level") != "high"),
        -len(candidate.code.splitlines()),
        -attempt_id,
    ]
    verifier_payload = decision.to_dict()
    if residual_state is not None:
        verifier_payload["residual_state"] = residual_state
    if residual_transition is not None:
        verifier_payload["residual_transition"] = residual_transition
    checkpoint = CandidateCheckpoint(
        instance_id=candidate.instance_id,
        round_id=attempt_id,
        code_path=code_path,
        score=score,
        reason=reason,
        oracle_risk=oracle_risk,
        surrogate_risk={},
        selector_score_before_risk=score,
        selector_score_after_risk=score,
        selector_penalty_reasons=[],
        execution=execution.to_dict(),
        verifier=verifier_payload,
        surrogate={},
        issue_aligned=issue_aligned,
        target_hit=target_hit,
        oracle_grounded_in_issue=grounded,
        uses_public_behavior=public,
        semantic_delta=dict(getattr(candidate, "semantic_delta", {}) or {}),
        delta_history=list(getattr(candidate, "delta_history", []) or []),
        delta_application=dict(getattr(candidate, "delta_application", {}) or {}),
        oracle_contract_kinds=sorted(effective_oracle_kinds),
        oracle_contract_preserved=oracle_contract_preserved,
        oracle_contract_violation=oracle_contract_violation,
        rank_key=rank_key,
    )
    checkpoint.save_json(
        str(Path(checkpoint_dir) / f"candidate_attempt_{attempt_id}.json")
    )
    return checkpoint


def _copy_if_exists(source_dir: Path, target_dir: Path, name: str) -> None:
    source = source_dir / name
    if source.exists():
        target = target_dir / name
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if source.is_dir():
            try:
                os.symlink(source, target, target_is_directory=True)
            except OSError:
                shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


def _best_checkpoint_from_summary(seed_dir: Path) -> dict[str, Any]:
    ranking_path = seed_dir / "candidate_ranking.json"
    if not ranking_path.is_file():
        return {}
    try:
        ranking = json.loads(ranking_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    checkpoints = ranking.get("checkpoints")
    selected = ranking.get("selected_attempt")
    if not isinstance(checkpoints, list):
        return {}
    for item in checkpoints:
        if isinstance(item, dict) and item.get("round_id") == selected:
            return item
    return checkpoints[-1] if checkpoints else {}


def _seed_result_score(summary: dict[str, Any], checkpoint: dict[str, Any]) -> int:
    status = str(summary.get("status") or "")
    # A checkpoint can predate a later setup/collection failure.  Such a seed
    # is not replayable by formal evaluation and must never tie with an
    # executable seed merely because the earlier checkpoint had a score.
    if status in {"ERROR", "SETUP_ERROR", "ENV_UNRESOLVED"}:
        return -1000
    if checkpoint:
        return int(checkpoint.get("score") or 0)
    if status == "ISSUE_ALIGNED_FAIL" or summary.get("strict_failure_class") == "issue_aligned":
        return 200
    buggy = summary.get("buggy_execution") if isinstance(summary.get("buggy_execution"), dict) else {}
    if buggy.get("returncode") not in {None, 0} and buggy.get("status") not in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}:
        return 100
    if buggy.get("returncode") == 0 or status in {"PASS", "BUGGY_PASS"}:
        return 10
    return 0


def _semantic_signature(checkpoint: dict[str, Any]) -> str:
    """Build a readable Top-3 consensus key without hashing artifacts."""

    verifier = checkpoint.get("verifier") if isinstance(checkpoint, dict) else {}
    verifier = verifier if isinstance(verifier, dict) else {}
    residual = verifier.get("residual_state")
    residual = residual if isinstance(residual, dict) else {}
    fields = (
        str(residual.get("target_behavior") or "").strip().lower(),
        str(verifier.get("failure_class") or "").strip().lower(),
        str(verifier.get("oracle_kind") or "").strip().upper(),
        str(residual.get("gap") or residual.get("next_gap") or "").strip().lower(),
    )
    return " | ".join(fields)


def _should_try_next_seed(
    summary: dict[str, Any],
    checkpoint: dict[str, Any],
    has_next: bool,
) -> tuple[bool, str]:
    if not has_next:
        return False, "no next seed"
    del summary, checkpoint
    return True, "fixed top-3 exploration; no semantic accept may stop later seeds"


def _missing_dependency_hint(log: str) -> str:
    patterns = [
        r"No module named ['\"]([^'\"]+)['\"]",
        r"requires the ([A-Za-z0-9_.-]+) python package",
    ]
    for pattern in patterns:
        match = re.search(pattern, log, flags=re.IGNORECASE)
        if match:
            return match.group(1).split(".", 1)[0]
    return ""


def _find_declared_requirement(repo_path: str, module_hint: str) -> str:
    if not module_hint:
        return ""
    token = re.sub(r"\d+$", "", module_hint.lower().replace("_", "-"))
    root = Path(repo_path)
    candidates = sorted(root.glob("requirements*.txt"))
    candidates += sorted(root.glob("requirements/*.txt"))
    candidates += sorted(root.glob("*/requirements*.txt"))
    candidates += sorted(root.glob("*/*/requirements*.txt"))
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", "-", "git+", "http://", "https://")):
                continue
            normalized = line.lower().replace("_", "-")
            if token and token in normalized:
                return line
    return ""


def _recover_declared_dependency(
    context: InstanceContext,
    execution: ExecutionResult,
    conda_env: str,
    timeout: int,
    no_conda: bool,
    output_dir: str,
    round_id: int,
) -> bool:
    from ..runtime.official_docker_runtime import active_runtime

    if active_runtime(context.instance_id) is not None:
        # Official benchmark images are immutable generation infrastructure.
        # A missing import is feedback for repairing the candidate/protocol,
        # never authorization for BRT6 to mutate the benchmark environment.
        safe_json_dump(
            {
                "status": "DISABLED_OFFICIAL_DOCKER",
                "runtime_backend": "official_docker",
                "reason": "host/container dependency mutation is forbidden",
            },
            str(Path(output_dir) / f"dependency_recovery_round_{round_id}.json"),
        )
        return False
    log = execution.stdout + "\n" + execution.stderr
    hint = _missing_dependency_hint(log)
    requirement = _find_declared_requirement(context.buggy_repo_path, hint)
    if not requirement:
        return False
    install_result = run_command_in_conda(
        f"python -m pip install {shlex.quote(requirement)}",
        context.buggy_repo_path,
        conda_env,
        timeout,
        no_conda,
        None,
        context.instance_id,
    )
    safe_json_dump(
        {
            "module_hint": hint,
            "requirement": requirement,
            "execution": install_result.to_dict(),
        },
        str(Path(output_dir) / f"dependency_recovery_round_{round_id}.json"),
    )
    return install_result.returncode == 0


def prepare_instance_worktree(
    context: InstanceContext,
    output_dir: str,
    conda_env: str,
    timeout: int,
    no_conda: bool,
) -> tuple[str, dict[str, Any]]:
    source_repo = context.buggy_repo_path
    base_commit = context.base_commit
    if not source_repo or not base_commit:
        return source_repo, {"status": "SKIPPED", "reason": "missing source repo or base_commit", "repo_path": source_repo}
    worktree = Path(output_dir) / "worktree"

    try:
        worktree_timeout = max(
            1, int(os.environ.get("BRT_WORKTREE_TIMEOUT", "1200"))
        )
    except ValueError:
        worktree_timeout = 1200

    def discard_partial_worktree() -> None:
        _run_local(
            f"git worktree remove --force {shlex.quote(str(worktree))}",
            source_repo,
            timeout=worktree_timeout,
        )
        if worktree.exists():
            shutil.rmtree(worktree)
        _run_local("git worktree prune", source_repo, timeout=worktree_timeout)

    if worktree.exists():
        discard_partial_worktree()
    ensure_dir(worktree.parent)
    add_cmd = f"git worktree add --force --detach {shlex.quote(str(worktree))} {shlex.quote(base_commit)}"
    add_result = _run_local(add_cmd, source_repo, timeout=worktree_timeout)
    if add_result["returncode"] != 0:
        discard_partial_worktree()
        clone_cmd = f"git clone --shared {shlex.quote(source_repo)} {shlex.quote(str(worktree))}"
        clone_result = _run_local(
            clone_cmd, str(Path(output_dir)), timeout=worktree_timeout
        )
        checkout_result = _run_local(
            f"git checkout --force {shlex.quote(base_commit)}",
            str(worktree),
            timeout=worktree_timeout,
        ) if clone_result["returncode"] == 0 else {}
        add_result = {"worktree_add": add_result, "clone": clone_result, "checkout": checkout_result}
        if clone_result["returncode"] != 0 or checkout_result.get("returncode") != 0:
            return str(worktree), {"status": "WORKTREE_ERROR", "details": add_result, "repo_path": str(worktree)}
    from ..runtime.official_docker_runtime import active_runtime

    official_runtime = active_runtime(context.instance_id)
    if official_runtime is not None:
        # The worktree is only a source/candidate staging area.  Dependency
        # setup, project installation, and all executions live in the official
        # benchmark image prepared before this function is entered.
        return str(worktree), {
            "status": "PASS",
            "source_repo": source_repo,
            "repo_path": str(worktree),
            "base_commit": base_commit,
            "runtime_backend": "official_docker",
            "official_runtime_manifest": str(official_runtime.manifest_path),
            "host_project_environment_created": False,
            "environment": {"status": "SKIPPED_OFFICIAL_DOCKER"},
            "runtime_environment": {"status": "SKIPPED_OFFICIAL_DOCKER"},
            "setup_execution": {
                "status": "PASS",
                "returncode": 0,
                "runtime_backend": "official_docker",
            },
            "worktree": add_result,
        }
    submodule_result = _run_local(
        "git submodule update --init --recursive",
        str(worktree),
        timeout=600,
    )
    cached_astropy_helpers = Path(source_repo) / "astropy_helpers"
    worktree_astropy_helpers = worktree / "astropy_helpers"
    if (
        context.repo == "astropy/astropy"
        and cached_astropy_helpers.is_dir()
        and not worktree_astropy_helpers.exists()
    ):
        shutil.copytree(
            cached_astropy_helpers,
            worktree_astropy_helpers,
            symlinks=True,
        )
    version = str(context.metadata.get("version") or "")
    environment_setup_commit = str(
        context.metadata.get("environment_setup_commit") or base_commit
    )
    spec = make_instance_spec(
        context.instance_id,
        context.repo,
        version,
        base_commit,
        environment_setup_commit,
    )
    dump_spec(spec, str(Path(output_dir) / "icore_exec_spec.json"))
    resolved_env = conda_env or env_name_for(
        context.repo,
        version,
        base_commit,
        environment_setup_commit,
    )
    env_result = ensure_icore_environment(
        spec, resolved_env, str(worktree), timeout
    )
    requested_env = resolved_env
    # The environment manager may validate and reuse a compatible canonical
    # dependency environment. All subsequent setup/test commands must activate
    # the resolved name it returns, not the originally requested alias.
    template_env = str(env_result.get("env_name") or resolved_env)
    if env_result.get("returncode") != 0:
        return str(worktree), {
            "status": "ENV_CREATE_ERROR",
            "source_repo": source_repo,
            "repo_path": str(worktree),
            "base_commit": base_commit,
            "env_name": template_env,
            "template_env_name": template_env,
            "requested_env": requested_env,
            "environment": env_result,
            "worktree": add_result,
        }
    runtime_env_result = (
        {
            "status": "SKIPPED_NO_CONDA",
            "returncode": 0,
            "created": False,
            "env_name": template_env,
            "template_env_name": template_env,
        }
        if no_conda
        else ensure_isolated_runtime_environment(
            template_env,
            context.instance_id,
            str(worktree),
            timeout,
            "generation",
            spec.install,
            project_distribution=context.repo.split("/")[-1],
        )
    )
    resolved_env = str(runtime_env_result.get("env_name") or template_env)
    if runtime_env_result.get("returncode") != 0:
        return str(worktree), {
            "status": "ENV_CREATE_ERROR",
            "source_repo": source_repo,
            "repo_path": str(worktree),
            "base_commit": base_commit,
            "env_name": resolved_env,
            "template_env_name": template_env,
            "requested_env": requested_env,
            "environment": env_result,
            "runtime_environment": runtime_env_result,
            "worktree": add_result,
        }
    setup = icore_setup_command(spec, str(worktree))
    setup_lock = env_lock_path(resolved_env, "project_setup")
    setup_lock.parent.mkdir(parents=True, exist_ok=True)
    with open(setup_lock, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        setup_result = run_command_in_conda(
            setup,
            str(worktree),
            resolved_env,
            timeout,
            no_conda,
            None,
            context.instance_id,
        )
        setup_log = setup_result.stdout + "\n" + setup_result.stderr
        if (
            setup_result.returncode != 0
            and "missing the 'build_editable' hook" in setup_log
            and " -e ." in setup
        ):
            fallback_setup = setup.replace(" -e .", " .")
            fallback_result = run_command_in_conda(
                fallback_setup,
                str(worktree),
                resolved_env,
                timeout,
                no_conda,
                None,
                context.instance_id,
            )
            if fallback_result.returncode == 0:
                setup = fallback_setup
                setup_result = fallback_result
        setup_log = setup_result.stdout + "\n" + setup_result.stderr
        if (
            setup_result.returncode != 0
            and "uninstall-no-record-file" in setup_log
            and "python -m pip install" in setup
            and " -e ." in setup
        ):
            fallback_setup = re.sub(
                r"python -m pip install(?![^&]*--ignore-installed)([^&]*\s-e\s+\.)",
                r"python -m pip install --ignore-installed --no-deps\1",
                setup,
                count=1,
            )
            fallback_result = run_command_in_conda(
                fallback_setup,
                str(worktree),
                resolved_env,
                timeout,
                no_conda,
                None,
                context.instance_id,
            )
            if fallback_result.returncode == 0:
                setup = fallback_setup
                setup_result = fallback_result
    runtime_manifest = (
        {}
        if no_conda
        else environment_manifest(
            resolved_env, timeout=min(max(timeout, 120), 300), refresh=True
        )
    )
    safe_json_dump(
        {
            "instance_id": context.instance_id,
            "template_env_name": template_env,
            "runtime_env_name": resolved_env,
            "template_manifest": runtime_env_result.get("template_manifest", {}),
            "runtime_manifest_after_setup": runtime_manifest,
        },
        str(Path(output_dir) / "environment_manifest.json"),
    )
    status = "PASS" if setup_result.returncode == 0 else "SETUP_ERROR"
    return str(worktree), {
        "status": status,
        "source_repo": source_repo,
        "repo_path": str(worktree),
        "base_commit": base_commit,
        "environment_setup_commit": environment_setup_commit,
        "env_name": resolved_env,
        "template_env_name": template_env,
        "requested_env": requested_env,
        "environment": env_result,
        "runtime_environment": runtime_env_result,
        "runtime_environment_manifest": runtime_manifest,
        "worktree": add_result,
        "submodule": submodule_result,
        "setup_command": setup,
        "setup_execution": setup_result.to_dict(),
    }


def run_instance_pipeline(
    context: InstanceContext,
    llm_client: Any,
    output_dir: str,
    conda_env: str = "",
    timeout: int = 120,
    no_conda: bool = False,
    max_semantic_rounds: int = 5,
    validation_mode: str = "buggy_only",
    generate_only: bool = False,
    enable_protocol_recovery: bool = True,
    enable_seed_mutation: bool = True,
    enable_strict_semantic_verifier: bool = True,
    enable_behavior_target: bool = True,
    ablation_config: AblationConfig | None = None,
    _adaptive_disabled: bool = False,
    _forced_seed_index: int | None = None,
    _prepared_repo_path: str = "",
    _prepare_meta: dict[str, Any] | None = None,
    alignment_verifier: str = "strict",
) -> FinalResult:
    ensure_dir(output_dir)
    ensure_dir(Path(output_dir) / "prompts")
    ensure_dir(Path(output_dir) / "responses")
    ensure_dir(Path(output_dir) / "logs")
    try:
        config = (
            ablation_config
            or AblationConfig(
                behavior_target=enable_behavior_target,
                mutation=enable_seed_mutation,
            )
        ).validate()
        enable_behavior_target = config.behavior_target
        enable_seed_mutation = config.mutation
        safe_json_dump(
            config.to_dict(), str(Path(output_dir) / "ablation_manifest.json")
        )
        if validation_mode != "buggy_only":
            raise ValueError(
                "surrogate patch generation/validation is disabled; "
                "validation_mode must be 'buggy_only'"
            )
        if _uses_adaptive_seed_pipelines(
            config,
            adaptive_disabled=_adaptive_disabled,
            generate_only=generate_only,
            protocol_recovery_enabled=enable_protocol_recovery,
            forced_seed_index=_forced_seed_index,
        ):
            behavior = _load_behavior_evidence(
                context, output_dir, enable_behavior_target
            )
            ranked_tests = rank_related_tests(context.retrieved_tests, behavior)
            if not ranked_tests:
                ranked_tests = [select_related_test(context.retrieved_tests, behavior)]
            ranked_tests = [seed for seed in ranked_tests if seed is not None]
            seeds_to_try = ranked_tests[:3] or []
            if not seeds_to_try:
                return run_instance_pipeline(
                    context,
                    llm_client,
                    output_dir,
                    conda_env,
                    timeout,
                    no_conda,
                    max_semantic_rounds,
                    validation_mode,
                    generate_only,
                    enable_protocol_recovery,
                    enable_seed_mutation,
                    enable_strict_semantic_verifier,
                    enable_behavior_target=enable_behavior_target,
                    ablation_config=config,
                    alignment_verifier=alignment_verifier,
                    _adaptive_disabled=True,
                    _forced_seed_index=None,
                )
            prepared_repo_path = ""
            prepared_meta: dict[str, Any] | None = None
            if not generate_only:
                if _prepared_repo_path and _prepare_meta is not None:
                    prepared_repo_path = _prepared_repo_path
                    prepared_meta = dict(_prepare_meta)
                else:
                    prepared_repo_path, prepared_meta = prepare_instance_worktree(
                        context, output_dir, conda_env, timeout, no_conda
                    )
                conda_env = str(prepared_meta.get("env_name") or conda_env)
                context.buggy_repo_path = prepared_repo_path
                safe_json_dump(
                    prepared_meta,
                    str(Path(output_dir) / "repo_prepare.json"),
                )
                if prepared_meta.get("status") in {
                    "WORKTREE_ERROR",
                    "ENV_CREATE_ERROR",
                    "SETUP_ERROR",
                }:
                    result = FinalResult(
                        instance_id=context.instance_id,
                        status="SETUP_ERROR",
                        final_test_path="",
                        rounds_used=0,
                        buggy_execution=prepared_meta.get("setup_execution", {}),
                        dual_version_result={"mode": validation_mode, "status": "SKIPPED"},
                        **_evidence_result_fields(behavior, config),
                        host_context={},
                        observation_report={},
                        notes="repository worktree/setup failed before BRT generation",
                        protocol_recovery_enabled=enable_protocol_recovery,
                        seed_mutation_enabled=enable_seed_mutation,
                        strict_verifier_enabled=enable_strict_semantic_verifier,
                        delta_calls=0,
                        repair_route_counts=_empty_repair_route_counts(),
                        final_reason="repository worktree/setup failed before BRT generation",
                        seed_mode="adaptive_top3",
                    )
                    result.save_json(str(Path(output_dir) / "summary.json"))
                    return result
            seed_root = Path(output_dir) / "seed_candidates"
            ensure_dir(seed_root)
            attempts: list[dict[str, Any]] = []
            switch_reasons: list[str] = []
            best: tuple[int, int, int, Path, dict[str, Any], dict[str, Any]] | None = None
            for seed_index, seed in enumerate(seeds_to_try):
                seed_dir = seed_root / f"seed_{seed_index}"
                ensure_dir(seed_dir)
                _save_behavior_evidence(behavior, seed_dir)
                seed_context = copy.deepcopy(context)
                result = run_instance_pipeline(
                    seed_context,
                    llm_client,
                    str(seed_dir),
                    conda_env,
                    timeout,
                    no_conda,
                    max_semantic_rounds,
                    validation_mode,
                    generate_only,
                    enable_protocol_recovery,
                    enable_seed_mutation,
                    enable_strict_semantic_verifier,
                    enable_behavior_target=enable_behavior_target,
                    ablation_config=config,
                    alignment_verifier=alignment_verifier,
                    _adaptive_disabled=True,
                    _forced_seed_index=seed_index,
                    _prepared_repo_path=prepared_repo_path,
                    _prepare_meta=prepared_meta,
                )
                summary_path = seed_dir / "summary.json"
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    summary = result.to_dict()
                checkpoint = _best_checkpoint_from_summary(seed_dir)
                score = _seed_result_score(summary, checkpoint)
                final_residual = checkpoint.get("verifier", {}).get(
                    "residual_state", {}
                )
                attempt = {
                    "seed_index": seed_index,
                    "seed_file": seed.file,
                    "seed_name": seed.name,
                    "status": summary.get("status"),
                    "score": score,
                    "checkpoint": checkpoint,
                    "summary_path": str(summary_path),
                    "final_test_path": str(seed_dir / "final_test.py"),
                    "delta_calls": int(summary.get("delta_calls") or 0),
                    "valid_delta_calls": int(summary.get("valid_delta_calls") or 0),
                    "keep_delta_calls": int(summary.get("keep_delta_calls") or 0),
                    "repair_route_counts": dict(
                        summary.get("repair_route_counts") or {}
                    ),
                    "final_residual_stage": final_residual.get("stage"),
                    "final_residual_depth": final_residual.get("depth"),
                    "semantic_signature": _semantic_signature(checkpoint),
                }
                attempts.append(attempt)
                order_key = (score, -seed_index, -int(checkpoint.get("round_id") or 0))
                if best is None or order_key > (best[0], best[1], best[2]):
                    best = (order_key[0], order_key[1], order_key[2], seed_dir, summary, checkpoint)
                has_next = seed_index < len(seeds_to_try) - 1
                reason = "all retrieved Top-3 seeds run independently" if has_next else "all seeds completed"
                attempts[-1]["switch_decision"] = "try_next_seed" if has_next else "stop"
                attempts[-1]["switch_reason"] = reason
                if has_next:
                    switch_reasons.append(f"seed_{seed_index}: {reason}")

            # Component 1 is additive: retain its accepted output, and invoke
            # direct adaptation only when none of the three target-guided
            # branches produced a strictly accepted BRT.  This route never
            # reads fixed-side outcomes and therefore remains usable at
            # generation time.
            direct_fallback_attempted = _should_run_direct_fallback(
                config, attempts
            )
            direct_fallback_used = False
            direct_fallback_status = "NOT_ATTEMPTED"
            direct_fallback_dir = Path(output_dir) / "direct_fallback"
            direct_fallback_summary: dict[str, Any] = {}
            if direct_fallback_attempted:
                direct_config = AblationConfig(
                    behavior_target=False,
                    mutation=config.mutation,
                    specialized_feedback=config.specialized_feedback,
                    environment_feedback=config.environment_feedback,
                    trigger_feedback=config.trigger_feedback,
                    assertion_feedback=config.assertion_feedback,
                    semantic_delta=config.semantic_delta,
                ).validate()
                fallback_result = run_instance_pipeline(
                    copy.deepcopy(context),
                    llm_client,
                    str(direct_fallback_dir),
                    conda_env,
                    timeout,
                    no_conda,
                    max_semantic_rounds,
                    validation_mode,
                    generate_only,
                    enable_protocol_recovery,
                    enable_seed_mutation,
                    enable_strict_semantic_verifier,
                    enable_behavior_target=False,
                    ablation_config=direct_config,
                    alignment_verifier=alignment_verifier,
                    _prepared_repo_path=prepared_repo_path,
                    _prepare_meta=prepared_meta,
                )
                direct_fallback_status = fallback_result.status
                fallback_summary_path = direct_fallback_dir / "summary.json"
                if fallback_summary_path.is_file():
                    try:
                        direct_fallback_summary = json.loads(
                            fallback_summary_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        direct_fallback_summary = fallback_result.to_dict()
                else:
                    direct_fallback_summary = fallback_result.to_dict()
                direct_fallback_used = bool(
                    direct_fallback_status == "ISSUE_ALIGNED_FAIL"
                    and (direct_fallback_dir / "final_test.py").is_file()
                )
            # Prefer agreement among independent iCoRe seeds, but only after
            # hard executability/Oracle eligibility. The consensus key stays
            # readable and is never converted to a digest.
            signature_counts: dict[str, int] = {}
            for item in attempts:
                signature = str(item.get("semantic_signature") or "")
                if signature.strip(" |"):
                    signature_counts[signature] = signature_counts.get(signature, 0) + 1
            for item in attempts:
                item["semantic_consensus"] = signature_counts.get(
                    str(item.get("semantic_signature") or ""), 0
                )
            selected_attempt = max(
                attempts,
                key=lambda item: (
                    int(bool(((item.get("checkpoint") or {}).get("rank_key") or [0])[0])),
                    int(item.get("semantic_consensus") or 0),
                    tuple((item.get("checkpoint") or {}).get("rank_key") or []),
                    -int(item.get("seed_index") or 0),
                ),
            )
            selected_dir_by_consensus = seed_root / "seed_{}".format(
                int(selected_attempt["seed_index"])
            )
            selected_summary_by_consensus = json.loads(
                (selected_dir_by_consensus / "summary.json").read_text(encoding="utf-8")
            )
            best = (
                int(selected_attempt.get("score") or 0),
                -int(selected_attempt.get("seed_index") or 0),
                -int((selected_attempt.get("checkpoint") or {}).get("round_id") or 0),
                selected_dir_by_consensus,
                selected_summary_by_consensus,
                dict(selected_attempt.get("checkpoint") or {}),
            )
            assert best is not None
            _, _, _, selected_dir, selected_summary, selected_checkpoint = best
            target_primary_status = str(selected_summary.get("status") or "")
            selection_route = "target_primary"
            if direct_fallback_used:
                selected_dir = direct_fallback_dir
                selected_summary = dict(direct_fallback_summary)
                selected_checkpoint = _best_checkpoint_from_summary(selected_dir)
                selection_route = "direct_fallback_after_target_exhaustion"
            selected_seed_index = int(
                selected_summary.get("selected_seed_index")
                if direct_fallback_used
                else selected_dir.name.rsplit("_", 1)[-1]
            )
            selected_seed_delta_calls = int(
                selected_summary.get("selected_seed_delta_calls")
                or selected_summary.get("delta_calls")
                or 0
            )
            selected_seed_routes = dict(
                selected_summary.get("selected_seed_repair_route_counts")
                or selected_summary.get("repair_route_counts")
                or {}
            )
            fallback_attempts = list(
                direct_fallback_summary.get("seed_attempts_summary") or []
            )
            selected_attempts = fallback_attempts if direct_fallback_used else attempts
            selected_switch_reasons = list(
                direct_fallback_summary.get("seed_switch_reasons") or []
            ) if direct_fallback_used else switch_reasons
            all_seed_delta_calls = sum(int(item.get("delta_calls") or 0) for item in attempts)
            all_seed_valid_calls = sum(int(item.get("valid_delta_calls") or 0) for item in attempts)
            all_seed_keep_calls = sum(int(item.get("keep_delta_calls") or 0) for item in attempts)
            all_seed_routes = {
                route: sum(
                    int((item.get("repair_route_counts") or {}).get(route) or 0)
                    for item in attempts
                )
                for route in _empty_repair_route_counts()
            }
            if direct_fallback_attempted:
                all_seed_delta_calls += int(
                    direct_fallback_summary.get("all_seed_delta_calls")
                    or direct_fallback_summary.get("delta_calls")
                    or 0
                )
                all_seed_valid_calls += int(
                    direct_fallback_summary.get("valid_delta_calls") or 0
                )
                all_seed_keep_calls += int(
                    direct_fallback_summary.get("keep_delta_calls") or 0
                )
                fallback_routes = dict(
                    direct_fallback_summary.get("all_seed_repair_route_counts")
                    or direct_fallback_summary.get("repair_route_counts")
                    or {}
                )
                all_seed_routes = {
                    route: int(all_seed_routes.get(route) or 0)
                    + int(fallback_routes.get(route) or 0)
                    for route in _empty_repair_route_counts()
                }
            selected_exportable = True
            top_final = Path(output_dir) / "final_test.py"
            if selected_exportable:
                _copy_if_exists(selected_dir, Path(output_dir), "final_test.py")
            elif top_final.exists() or top_final.is_symlink():
                top_final.unlink()
            for name in (
                "summary.json",
                "host_context.json",
                "protocol_recovery.json",
                "candidate_ranking.json",
                "residual_trace.json",
                "dual_version_result.json",
                "repo_prepare.json",
                "icore_exec_spec.json",
                "worktree",
            ):
                _copy_if_exists(selected_dir, Path(output_dir), name)
            selected_summary.update(
                {
                    "final_test_path": str(top_final) if selected_exportable else "",
                    "seed_mode": "adaptive_top3",
                    "selected_seed_index": selected_seed_index,
                    "seed_attempts_count": len(selected_attempts),
                    "seed_attempts_summary": selected_attempts,
                    "seed_switch_reasons": selected_switch_reasons,
                    "target_seed_attempts_summary": attempts,
                    "direct_fallback_seed_attempts_summary": fallback_attempts,
                    "selected_seed_reason": selected_checkpoint.get("reason")
                    or selected_summary.get("final_reason")
                    or "selected by adaptive seed score",
                    "selection_route": selection_route,
                    "target_primary_status": target_primary_status,
                    "direct_fallback_attempted": direct_fallback_attempted,
                    "direct_fallback_used": direct_fallback_used,
                    "direct_fallback_status": direct_fallback_status,
                    "final_oracle_risk": {},
                    "final_surrogate_risk": {},
                    "behavior_target_enabled": enable_behavior_target,
                    "method_variant": config.method_variant,
                    "ablation_id": config.ablation_id,
                    "ablation_signature": config.signature,
                    "ablation_config": config.to_dict(),
                    "selected_seed_delta_calls": selected_seed_delta_calls,
                    "all_seed_delta_calls": all_seed_delta_calls,
                    "delta_calls": all_seed_delta_calls,
                    "valid_delta_calls": all_seed_valid_calls,
                    "keep_delta_calls": all_seed_keep_calls,
                    "selected_seed_repair_route_counts": selected_seed_routes,
                    "all_seed_repair_route_counts": all_seed_routes,
                    "repair_route_counts": all_seed_routes,
                }
            )
            safe_json_dump(
                selected_attempts,
                str(Path(output_dir) / "seed_attempts_summary.json"),
            )
            safe_json_dump(
                attempts,
                str(Path(output_dir) / "target_seed_attempts_summary.json"),
            )
            safe_json_dump(
                fallback_attempts,
                str(Path(output_dir) / "direct_fallback_seed_attempts_summary.json"),
            )
            safe_json_dump(
                {
                    "selected_seed_index": selected_seed_index,
                    "selected_seed_dir": str(selected_dir),
                    "selected_seed_reason": selected_summary["selected_seed_reason"],
                    "selection_route": selection_route,
                    "direct_fallback_attempted": direct_fallback_attempted,
                    "direct_fallback_used": direct_fallback_used,
                    "direct_fallback_status": direct_fallback_status,
                },
                str(Path(output_dir) / "selected_seed_summary.json"),
            )
            safe_json_dump(selected_summary, str(Path(output_dir) / "summary.json"))
            return FinalResult(
                instance_id=context.instance_id,
                status=str(selected_summary.get("status") or ""),
                final_test_path=str(top_final) if selected_exportable else "",
                rounds_used=int(selected_summary.get("rounds_used") or 0),
                buggy_execution=selected_summary.get("buggy_execution") or {},
                dual_version_result=selected_summary.get("dual_version_result") or {},
                behavior_target=(
                    selected_summary.get("behavior_target")
                    or behavior_target_payload(behavior)
                ),
                raw_issue_context=(
                    selected_summary.get("raw_issue_context")
                    or raw_issue_payload(behavior)
                ),
                behavior_target_enabled=enable_behavior_target,
                method_variant=config.method_variant,
                method_version=_method_version(config),
                ablation_id=config.ablation_id,
                ablation_signature=config.signature,
                ablation_config=config.to_dict(),
                behavior_schema_version=behavior.schema_version,
                host_context=selected_summary.get("host_context") or {},
                observation_report=selected_summary.get("observation_report") or {},
                notes=str(selected_summary.get("notes") or ""),
                seed_mode="adaptive_top3",
                selected_seed_index=selected_seed_index,
                seed_attempts_count=len(selected_attempts),
                seed_attempts_summary=selected_attempts,
                seed_switch_reasons=selected_switch_reasons,
                target_seed_attempts_summary=attempts,
                direct_fallback_seed_attempts_summary=fallback_attempts,
                selected_seed_reason=str(selected_summary.get("selected_seed_reason") or ""),
                selection_route=selection_route,
                target_primary_status=target_primary_status,
                direct_fallback_attempted=direct_fallback_attempted,
                direct_fallback_used=direct_fallback_used,
                direct_fallback_status=direct_fallback_status,
                final_oracle_risk=selected_summary.get("final_oracle_risk") or {},
                final_surrogate_risk=selected_summary.get("final_surrogate_risk") or {},
                final_reason=str(selected_summary.get("final_reason") or ""),
                delta_calls=int(selected_summary.get("delta_calls") or 0),
                valid_delta_calls=int(selected_summary.get("valid_delta_calls") or 0),
                keep_delta_calls=int(selected_summary.get("keep_delta_calls") or 0),
                selected_seed_delta_calls=selected_seed_delta_calls,
                all_seed_delta_calls=all_seed_delta_calls,
                final_semantic_delta=dict(selected_summary.get("final_semantic_delta") or {}),
                delta_history=list(selected_summary.get("delta_history") or []),
                final_delta_application=dict(
                    selected_summary.get("final_delta_application") or {}
                ),
                repair_route_counts=all_seed_routes,
                selected_seed_repair_route_counts=selected_seed_routes,
                all_seed_repair_route_counts=all_seed_routes,
                surrogate_patch_calls=0,
                **_selected_protocol_result_fields(selected_summary),
            )
        if not generate_only:
            if _prepared_repo_path and _prepare_meta is not None:
                prepared_repo, prepare_meta = _prepared_repo_path, dict(_prepare_meta)
            else:
                prepared_repo, prepare_meta = prepare_instance_worktree(
                    context, output_dir, conda_env, timeout, no_conda
                )
            conda_env = str(prepare_meta.get("env_name") or conda_env)
            context.buggy_repo_path = prepared_repo
            safe_json_dump(prepare_meta, str(Path(output_dir) / "repo_prepare.json"))
            if prepare_meta.get("status") in {
                "WORKTREE_ERROR",
                "ENV_CREATE_ERROR",
                "SETUP_ERROR",
            }:
                result = FinalResult(
                    instance_id=context.instance_id,
                    status="SETUP_ERROR",
                    final_test_path="",
                    rounds_used=0,
                    buggy_execution=prepare_meta.get("setup_execution", {}),
                    dual_version_result={"mode": validation_mode, "status": "SKIPPED"},
                    behavior_target={},
                    raw_issue_context=(
                        RawIssueContext(context.instance_id, context.issue_text).to_dict()
                        if not enable_behavior_target
                        else {}
                    ),
                    behavior_target_enabled=enable_behavior_target,
                    method_variant=config.method_variant,
                    method_version=_method_version(config),
                    ablation_id=config.ablation_id,
                    ablation_signature=config.signature,
                    ablation_config=config.to_dict(),
                    behavior_schema_version=(
                        "behavior_target.lossless.v1"
                        if enable_behavior_target
                        else "raw_issue_context.v1"
                    ),
                    host_context={},
                    observation_report={},
                    notes="repository worktree/setup failed before BRT generation",
                    protocol_recovery_enabled=enable_protocol_recovery,
                    seed_mutation_enabled=enable_seed_mutation,
                    strict_verifier_enabled=enable_strict_semantic_verifier,
                    delta_calls=0,
                    repair_route_counts=_empty_repair_route_counts(),
                    final_reason="repository worktree/setup failed before BRT generation",
                )
                result.save_json(str(Path(output_dir) / "summary.json"))
                return result
        else:
            safe_json_dump({"status": "SKIPPED", "reason": "generate_only"}, str(Path(output_dir) / "repo_prepare.json"))
        behavior = _load_behavior_evidence(
            context, output_dir, enable_behavior_target
        )
        _save_behavior_evidence(behavior, output_dir)
        protocol = None
        seed_fallback_used = False
        seed_attempts: list[dict[str, Any]] = []
        ranked_tests = rank_related_tests(context.retrieved_tests, behavior)
        related_test = ranked_tests[0] if ranked_tests else select_related_test(context.retrieved_tests, behavior)
        if not ranked_tests and related_test is not None:
            ranked_tests = [related_test]
        host = None
        if _forced_seed_index is not None and 0 <= _forced_seed_index < len(ranked_tests):
            seeds_to_try = [ranked_tests[_forced_seed_index]]
        else:
            seeds_to_try = ranked_tests[:3] if enable_protocol_recovery else ([related_test] if related_test else [])
        for seed_index, seed in enumerate(seeds_to_try):
            candidate_host = build_host_context(
                context.instance_id, seed, context.buggy_repo_path, behavior,
                context.retrieved_code, conda_env, timeout, no_conda,
                skip_execution=generate_only, repo=context.repo,
                version=str(context.metadata.get("version") or ""),
            )
            candidate_protocol = recover_test_protocol(
                context.instance_id, seed, context.buggy_repo_path, behavior,
                context.retrieved_code, context.repo,
                str(context.metadata.get("version") or ""),
            ) if enable_protocol_recovery else None
            seed_attempts.append({
                "rank": seed_index,
                "file": seed.file,
                "name": seed.name,
                "execution_status": candidate_host.seed_execution_status,
                "selected": False,
                "protocol_risks": candidate_protocol.protocol_risks if candidate_protocol else [],
            })
            related_test, host, protocol = seed, candidate_host, candidate_protocol
            if generate_only or candidate_host.seed_execution_status not in {
                "SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT", "ERROR"
            }:
                break
            seed_fallback_used = seed_index < min(2, len(seeds_to_try) - 1)
        if host is None:
            host = build_host_context(
                context.instance_id, None, context.buggy_repo_path, behavior,
                context.retrieved_code, conda_env, timeout, no_conda,
                skip_execution=generate_only, repo=context.repo,
                version=str(context.metadata.get("version") or ""),
            )
        if seed_attempts:
            seed_attempts[-1]["selected"] = True
        safe_json_dump({"fallback_used": seed_fallback_used, "attempts": seed_attempts}, str(Path(output_dir) / "seed_fallback.json"))
        if protocol is not None:
            try:
                protocol = audit_recovered_protocol(
                    protocol,
                    behavior,
                    related_test,
                    llm_client,
                    output_dir,
                    config,
                )
            except Exception as exc:  # noqa: BLE001
                protocol.protocol_risks.append(
                    f"Protocol model audit failed; retaining the AST recovery result: {exc}"
                )
            protocol.save_json(str(Path(output_dir) / "protocol_recovery.json"))
        host.save_json(str(Path(output_dir) / "host_context.json"))
        candidate = None
        execution = None
        decision = None
        observation = None
        dual = None
        final_code = ""
        semantic_deltas: list[SemanticDelta] = []
        repair_route_counts = _empty_repair_route_counts()
        strict_result = None
        oracle_type = ""
        initial_delta = _propose_delta_safely(
            context.instance_id,
            0,
            output_dir,
            behavior,
            host,
            protocol,
            llm_client,
            related_source=context.retrieved_code,
            related_test=related_test,
            issue_text=context.issue_text,
        ) if enable_seed_mutation else None
        if initial_delta is not None:
            semantic_deltas.append(initial_delta)
        if initial_delta is not None and initial_delta.is_actionable:
            candidate = generate_candidate(
                context.instance_id,
                behavior,
                host,
                related_test,
                context.retrieved_code,
                llm_client,
                output_dir,
                context.buggy_repo_path,
                0,
                write_to_repo=not generate_only,
                protocol=protocol,
                semantic_delta=initial_delta,
                delta_history=[],
                ablation_config=config,
                issue_text=context.issue_text,
            )
        else:
            candidate = materialize_current_test(
                context.instance_id,
                host,
                related_test.code_content if related_test else host.seed_test_code,
                output_dir,
                context.buggy_repo_path,
                semantic_delta=initial_delta,
                delta_history=[],
                write_to_repo=not generate_only,
            )
        _refresh_candidate_command(context, candidate)
        if generate_only:
            final_code = candidate.code
            write_text(str(Path(output_dir) / "final_test.py"), final_code)
            execution_stub = {"status": "SKIPPED", "reason": "generate_only"}
            result = FinalResult(
                instance_id=context.instance_id,
                status="GENERATED",
                final_test_path=str(Path(output_dir) / "final_test.py"),
                rounds_used=1,
                buggy_execution=execution_stub,
                dual_version_result={"mode": "buggy_only", "status": "SKIPPED"},
                **_evidence_result_fields(behavior, config),
                host_context=host.to_dict(),
                observation_report={},
                notes="generate_only: complete same-directory test file generated without execution",
                protocol_recovery_enabled=enable_protocol_recovery,
                seed_mutation_enabled=enable_seed_mutation,
                strict_verifier_enabled=enable_strict_semantic_verifier,
                selected_seed_file=related_test.file if related_test else "",
                selected_seed_name=related_test.name if related_test else "",
                seed_fallback_used=seed_fallback_used,
                **_delta_result_fields(semantic_deltas, candidate),
                repair_route_counts=repair_route_counts,
                final_reason="generate_only: generation completed without execution",
                seed_mode=(
                    "single_forced_seed"
                    if _forced_seed_index is not None
                    else "single_seed"
                ),
                selected_seed_index=0,
                seed_attempts_count=len(seed_attempts),
                seed_attempts_summary=seed_attempts,
            )
            result.save_json(str(Path(output_dir) / "summary.json"))
            return result

        brt_attempt = 0
        max_brt_attempts = semantic_round_budget(max_semantic_rounds)
        checkpoints: list[CandidateCheckpoint] = []
        best_score = -1
        best_rank_key: tuple[int, ...] | None = None
        best_index = -1
        best_candidate = None
        best_execution = None
        best_decision = None
        best_dual = None
        best_observation = None
        best_strict_result = None
        seed_residual = _seed_residual_state(host)
        previous_residual = None
        residual_rounds: list[dict[str, Any]] = []
        while brt_attempt < max_brt_attempts:
            guard = check_candidate(candidate.code, candidate.candidate_repo_path)
            if not guard.ok:
                execution = static_failure_execution(
                    context.instance_id,
                    candidate.command,
                    context.buggy_repo_path,
                    guard,
                )
            elif brt_attempt > 0 or execution is None:
                execution = run_command_in_conda(candidate.command, context.buggy_repo_path, conda_env, timeout, no_conda, behavior, context.instance_id)
            safe_json_dump(execution.to_dict(), str(Path(output_dir) / f"execution_round_{brt_attempt}.json"))
            write_text(str(Path(output_dir) / "logs" / f"execution_round_{brt_attempt}.log"), execution.stdout + "\n" + execution.stderr)
            effective_source = format_effective_source_context(
                behavior, context.retrieved_code, context.buggy_repo_path
            )
            if alignment_verifier == "issue2test":
                from ..validation.issue2test_verifier import verify_issue2test
                decision, strict_result = verify_issue2test(
                    context.issue_text, behavior, protocol, candidate, execution,
                    effective_source, llm_client, output_dir, brt_attempt,
                )
            elif enable_strict_semantic_verifier:
                decision, strict_result = verify_strict_semantics(
                    context.issue_text, behavior, protocol, candidate,
                    execution, effective_source, llm_client, output_dir,
                    brt_attempt,
                    ablation_config=config,
                )
            else:
                decision = verify_buggy_only(
                    context.issue_text, behavior, candidate, execution,
                    llm_client, host.to_dict(), effective_source,
                    ablation_config=config,
                )
            safe_json_dump(decision.to_dict(), str(Path(output_dir) / f"verifier_round_{brt_attempt}.json"))
            current_residual = None
            residual_transition = None
            if alignment_verifier == "issue2test":
                semantic_feedback = decision.to_dict()
                semantic_feedback["verifier"] = "issue2test_local_phase3_no_gold_v1"
            elif config.specialized_feedback and config.semantic_delta:
                current_residual = _candidate_residual_state(
                    brt_attempt, execution, decision, strict_result
                )
                residual_transition = _residual_transition(
                    previous_residual or seed_residual,
                    current_residual,
                    initialization=previous_residual is None,
                )
                semantic_feedback = _semantic_feedback_payload(
                    decision,
                    strict_result,
                    current_residual,
                    residual_transition,
                )
            else:
                semantic_feedback = _semantic_feedback_payload(
                    decision, strict_result
                )
            candidate_dual = None
            checkpoint = _save_checkpoint(
                output_dir,
                brt_attempt,
                candidate,
                execution,
                decision,
                candidate_dual,
                behavior,
                context.issue_text,
                {item.path for item in context.retrieved_code if item.path},
                strict_result=strict_result,
                residual_state=current_residual,
                residual_transition=residual_transition,
            )
            checkpoints.append(checkpoint)
            if current_residual is not None:
                residual_rounds.append(
                    {
                        "round_id": brt_attempt,
                        "state": current_residual,
                        "transition": residual_transition,
                    }
                )
                previous_residual = current_residual
                _save_residual_trace(
                    output_dir,
                    context.instance_id,
                    host,
                    residual_rounds,
                )
            checkpoint_rank_key = tuple(int(item) for item in checkpoint.rank_key)
            if best_rank_key is None or checkpoint_rank_key > best_rank_key:
                best_rank_key = checkpoint_rank_key
                best_score = checkpoint.score
                best_index = len(checkpoints) - 1
                best_candidate = copy.deepcopy(candidate)
                best_execution = copy.deepcopy(execution)
                best_decision = copy.deepcopy(decision)
                best_dual = copy.deepcopy(candidate_dual)
                best_observation = copy.deepcopy(observation)
                best_strict_result = copy.deepcopy(strict_result)
            if (
                residual_transition is not None
                and residual_transition.get("relation") == "REGRESSED"
                and best_candidate is not None
            ):
                candidate = copy.deepcopy(best_candidate)
                semantic_feedback["search_control"] = {
                    "action": "RESTORE_PARENT",
                    "reason": "last semantic mutation broke an already satisfied stage",
                    "preserve": list(current_residual.get("preserve") or []),
                }
            if decision.decision == "accept":
                # Accept ends repair for this seed only. The outer fixed
                # top-3 loop still evaluates later iCoRe seeds before rank.
                break
            next_round = brt_attempt + 1
            if next_round >= max_brt_attempts:
                break
            delta = _propose_delta_safely(
                context.instance_id,
                next_round,
                output_dir,
                behavior,
                host,
                protocol,
                llm_client,
                execution_feedback=execution.stdout + "\n" + execution.stderr,
                verifier_feedback=semantic_feedback,
                related_source=context.retrieved_code,
                related_test=related_test,
                current_candidate_code=candidate.code,
                delta_history=[item.to_dict() for item in semantic_deltas],
                issue_text=context.issue_text,
            ) if enable_seed_mutation else None
            if delta is not None:
                semantic_deltas.append(delta)
            if repeated_keep(semantic_deltas):
                break
            if delta is None:
                break
            if not delta.is_actionable:
                brt_attempt += 1
                continue
            candidate = generate_candidate(
                context.instance_id,
                behavior,
                host,
                related_test,
                context.retrieved_code,
                llm_client,
                output_dir,
                context.buggy_repo_path,
                next_round,
                feedback=json.dumps(semantic_feedback, ensure_ascii=False),
                write_to_repo=True,
                protocol=protocol,
                semantic_delta=delta,
                current_test_code=candidate.code,
                delta_history=[item.to_dict() for item in semantic_deltas[:-1]],
                ablation_config=config,
                issue_text=context.issue_text,
            )
            _refresh_candidate_command(context, candidate)
            route = _repair_focus(decision, strict_result, execution)
            repair_route_counts[
                "environment" if route == "setup" else (
                    "assertion" if route == "oracle" else "trigger"
                )
            ] += 1
            brt_attempt += 1
            continue
        if best_candidate is not None:
            candidate = best_candidate
            execution = best_execution
            decision = best_decision
            dual = best_dual
            observation = best_observation
            strict_result = best_strict_result
            final_code = candidate.code
            write_text(candidate.candidate_file_path, candidate.code)
            _refresh_candidate_command(context, candidate)
            checkpoints[best_index].selected = True
            checkpoints[best_index].save_json(
                str(
                    Path(output_dir)
                    / "checkpoints"
                    / f"candidate_attempt_{checkpoints[best_index].round_id}.json"
                )
            )
            if residual_rounds:
                _save_residual_trace(
                    output_dir,
                    context.instance_id,
                    host,
                    residual_rounds,
                    checkpoints[best_index].round_id,
                )
            safe_json_dump(
                {
                    "selection_policy": (
                        "Hard eligibility (executable buggy fail, falsifiable Oracle, "
                        "minimal guard passed) > semantic "
                        "consensus > LLM accept > "
                        "issue_aligned > semantic target_hit > issue-grounded Oracle > "
                        "public behavior > post-fix/Oracle risk > shorter test > earliest round"
                    ),
                    "selected_attempt": checkpoints[best_index].round_id,
                    "checkpoints": [item.to_dict() for item in checkpoints],
                },
                str(Path(output_dir) / "candidate_ranking.json"),
            )
        assert candidate is not None and execution is not None
        if decision is not None and decision.decision == "accept":
            status = "ISSUE_ALIGNED_FAIL"
        elif execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}:
            status = execution.status
        elif execution.returncode != 0:
            # Executor keyword matching is only a triage hint. A rejected
            # verifier decision must never become an accepted issue failure.
            status = "UNRELATED_FAIL"
        elif execution.status == "PASS":
            status = "TRIGGER_UNRESOLVED"
        else:
            status = execution.status
        dual = DualVersionResult(
            context.instance_id,
            "buggy_only",
            execution.to_dict(),
            {},
            "SKIPPED_NO_SURROGATE",
            "Surrogate patch generation and validation are disabled by method definition.",
        )
        dual.save_json(str(Path(output_dir) / "dual_version_result.json"))
        final_output_path = Path(output_dir) / "final_test.py"
        write_text(str(final_output_path), final_code or candidate.code)
        exported_final_path = str(final_output_path)
        final_oracle_risk = {}
        final_surrogate_risk = {}
        candidate_selector = first_test_selector(final_code or candidate.code)
        placement_dir = str(Path(candidate.candidate_repo_path).parent)
        result = FinalResult(
            instance_id=context.instance_id,
            status=status,
            final_test_path=exported_final_path,
            rounds_used=len(checkpoints),
            buggy_execution=execution.to_dict(),
            dual_version_result=dual.to_dict(),
            **_evidence_result_fields(behavior, config),
            host_context=host.to_dict(),
            observation_report=observation.to_dict() if observation else {},
            notes=decision.reason if decision else "",
            protocol_recovery_enabled=enable_protocol_recovery,
            seed_mutation_enabled=enable_seed_mutation,
            strict_verifier_enabled=enable_strict_semantic_verifier,
            selected_seed_file=related_test.file if related_test else "",
            selected_seed_name=related_test.name if related_test else "",
            seed_fallback_used=seed_fallback_used,
            **_delta_result_fields(semantic_deltas, candidate),
            repair_route_counts=repair_route_counts,
            oracle_type=oracle_type,
            strict_verifier_decision=strict_result.decision if strict_result else "",
            strict_failure_class=strict_result.failure_class if strict_result else "",
            final_reason=decision.reason if decision else "",
            seed_mode=(
                "single_forced_seed"
                if _forced_seed_index is not None
                else "single_seed"
            ),
            selected_seed_index=_forced_seed_index if _forced_seed_index is not None else 0,
            seed_attempts_count=len(seed_attempts),
            seed_attempts_summary=seed_attempts,
            final_oracle_risk=final_oracle_risk,
            final_surrogate_risk=final_surrogate_risk,
            candidate_repo_path=candidate.candidate_repo_path,
            pytest_nodeid=candidate.pytest_nodeid,
            command=candidate.command,
            direct_test_repo_path_hint=candidate.candidate_repo_path,
            placement_dir=placement_dir,
            runner_kind=context.repo.split("/")[-1],
            selector=candidate_selector,
            surrogate_patch_calls=0,
        )
        result.save_json(str(Path(output_dir) / "summary.json"))
        return result
    except Exception as exc:  # noqa: BLE001
        fallback_config = (
            ablation_config
            or AblationConfig(
                behavior_target=enable_behavior_target,
                mutation=enable_seed_mutation,
            )
        )
        safe_json_dump({
            "instance_id": context.instance_id,
            "status": "ERROR",
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "protocol_recovery_enabled": enable_protocol_recovery,
            "seed_mutation_enabled": enable_seed_mutation,
            "strict_verifier_enabled": enable_strict_semantic_verifier,
            "behavior_target_enabled": enable_behavior_target,
            "method_variant": fallback_config.method_variant,
            "ablation_id": fallback_config.ablation_id,
            "ablation_signature": fallback_config.signature,
            "ablation_config": fallback_config.to_dict(),
            "selected_seed_file": "",
            "selected_seed_name": "",
            "seed_fallback_used": False,
            "delta_calls": 0,
            "repair_route_counts": _empty_repair_route_counts(),
            "oracle_type": "",
            "strict_verifier_decision": "",
            "strict_failure_class": "",
            "final_reason": str(exc),
        }, str(Path(output_dir) / "summary.json"))
        return FinalResult(
            instance_id=context.instance_id,
            status="ERROR",
            notes=str(exc),
            behavior_target_enabled=enable_behavior_target,
            method_variant=fallback_config.method_variant,
            method_version=_method_version(fallback_config),
            ablation_id=fallback_config.ablation_id,
            ablation_signature=fallback_config.signature,
            ablation_config=fallback_config.to_dict(),
            behavior_schema_version=(
                "behavior_target.lossless.v1"
                if enable_behavior_target
                else "raw_issue_context.v1"
            ),
            delta_calls=0,
            repair_route_counts=_empty_repair_route_counts(),
        )

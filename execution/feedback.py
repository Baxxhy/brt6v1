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

from ..execution.executor import run_command_in_conda
from ..generation.generator import (
    format_effective_source_context,
    generate_candidate,
    repair_candidate,
)
from ..context.host_context import build_host_context, rank_related_tests, select_related_test
from ..context.protocol_recovery import audit_recovered_protocol, recover_test_protocol
from ..mutation.seed_mutator import build_mutation_plan
# Kept as a compatibility seam for older callers/tests; the solid feedback
# path no longer uses buggy-only observation rebinding as its default repair.
from ..generation.observation_oracle import rebind_observation_oracle
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
    InstanceContext,
    MutationPlan,
    RawIssueContext,
    VerifierDecision,
)
from ..core.utils import ensure_dir, safe_json_dump, write_text
from ..validation.verifier import verify_buggy_only
from ..validation.oracle_risk import assess_oracle_risk
from ..validation.semantic_guard import audit_candidate, oracle_contract_summary


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
        return "p0-validated-mutation-oracle-feedback-v4"
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
        "observation_oracle_enabled": bool(
            selected_summary.get("observation_oracle_enabled")
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
        "oracle_rebound": bool(selected_summary.get("oracle_rebound")),
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


def _mutation_result_fields(
    plans: list[Any],
    trigger_replan_calls: int = 0,
    candidate: Any | None = None,
) -> dict[str, Any]:
    statuses = [str(getattr(plan, "status", "")) for plan in plans]
    return {
        "mutation_ops": list(
            dict.fromkeys(
                op for plan in plans for op in getattr(plan, "mutation_ops", [])
            )
        ),
        "mutation_plan_calls": len(plans),
        "mutation_plan_valid_calls": statuses.count("VALID"),
        "mutation_plan_invalid_calls": statuses.count("INVALID"),
        "mutation_plan_abstentions": statuses.count("ABSTAIN"),
        "trigger_replan_calls": trigger_replan_calls,
        "final_mutation_plan_status": str(
            getattr(candidate, "mutation_plan_status", "") or ""
        ),
        "final_mutation_plan_risk": str(
            getattr(candidate, "mutation_plan_risk", "") or ""
        ),
        "final_mutation_adherence": dict(
            getattr(candidate, "mutation_adherence", {}) or {}
        ),
    }


def _build_plan_or_invalid(
    instance_id: str,
    round_id: int,
    output_dir: str,
    *args: Any,
    **kwargs: Any,
) -> MutationPlan:
    """Keep a planner implementation/service failure local to the current seed."""

    try:
        return build_mutation_plan(instance_id, round_id, *args, output_dir=output_dir, **kwargs)
    except Exception as exc:  # noqa: BLE001
        plan = MutationPlan(
            instance_id=instance_id,
            round_id=round_id,
            status="INVALID",
            validation_errors=[f"planner failed safely at pipeline boundary: {exc}"],
            validation_evidence={"pipeline_fallback": True},
        )
        plan.save_json(str(Path(output_dir) / f"mutation_round_{round_id}_plan.json"))
        return plan


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
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
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
            }
        )
    return payload


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
    mutation_adherence = dict(
        getattr(candidate, "mutation_adherence", {}) or {}
    )
    plan_violated = mutation_adherence.get("status") == "VIOLATED"
    oracle_contract_preserved = bool(
        getattr(candidate, "oracle_contract_preserved", True)
    )
    oracle_contract_violation = str(
        getattr(candidate, "oracle_contract_violation", "") or ""
    )
    executable_fail = execution.returncode != 0 and execution.status not in {
        "SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT",
    }
    static_problem = (
        audit_candidate(
            behavior,
            candidate.code,
            issue_text=issue_text,
            execution_log=execution.stdout + "\n" + execution.stderr,
        )
        if behavior
        else ""
    )
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
        and not plan_violated
        and oracle_contract_preserved
        and not static_problem
        and (
            bool(oracle_contract.get("falsifiable"))
            or strict_no_exception
        )
    )
    rank_key = [
        int(hard_eligible),
        int(accepted),
        int(issue_aligned),
        int(target_hit),
        int(grounded),
        int(public),
        int(executable_fail),
        int(not plan_violated),
        int(oracle_contract_preserved),
        int((oracle_risk or {}).get("level") != "high"),
        -attempt_id,
    ]
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
        verifier=decision.to_dict(),
        surrogate={},
        issue_aligned=issue_aligned,
        target_hit=target_hit,
        oracle_grounded_in_issue=grounded,
        uses_public_behavior=public,
        mutation_plan_status=str(
            getattr(candidate, "mutation_plan_status", "") or ""
        ),
        mutation_plan_risk=str(
            getattr(candidate, "mutation_plan_risk", "") or ""
        ),
        mutation_adherence=mutation_adherence,
        oracle_contract_kinds=sorted(effective_oracle_kinds),
        oracle_contract_preserved=oracle_contract_preserved,
        oracle_contract_violation=(oracle_contract_violation or static_problem),
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
    if worktree.exists():
        _run_local(
            f"git worktree remove --force {shlex.quote(str(worktree))}",
            source_repo,
            timeout=300,
        )
        if worktree.exists():
            shutil.rmtree(worktree)
    ensure_dir(worktree.parent)
    add_cmd = f"git worktree add --force --detach {shlex.quote(str(worktree))} {shlex.quote(base_commit)}"
    add_result = _run_local(add_cmd, source_repo, timeout=300)
    if add_result["returncode"] != 0:
        clone_cmd = f"git clone --shared {shlex.quote(source_repo)} {shlex.quote(str(worktree))}"
        clone_result = _run_local(clone_cmd, str(Path(output_dir)), timeout=600)
        checkout_result = _run_local(f"git checkout --force {shlex.quote(base_commit)}", str(worktree), timeout=300) if clone_result["returncode"] == 0 else {}
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
    max_feedback_rounds: int = 3,
    max_env_rounds: int | None = None,
    max_brt_rounds: int | None = None,
    max_patch_rounds: int = 3,
    validation_mode: str = "buggy_only",
    patched_repo_base: str = "",
    patch_file: str = "",
    generate_only: bool = False,
    enable_protocol_recovery: bool = True,
    enable_seed_mutation: bool = True,
    enable_observation_oracle: bool = True,
    enable_strict_semantic_verifier: bool = True,
    enable_behavior_target: bool = True,
    ablation_config: AblationConfig | None = None,
    _adaptive_disabled: bool = False,
    _forced_seed_index: int | None = None,
    _prepared_repo_path: str = "",
    _prepare_meta: dict[str, Any] | None = None,
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
                    max_feedback_rounds,
                    max_env_rounds,
                    max_brt_rounds,
                    max_patch_rounds,
                    validation_mode,
                    patched_repo_base,
                    patch_file,
                    generate_only,
                    enable_protocol_recovery,
                    enable_seed_mutation,
                    enable_observation_oracle,
                    enable_strict_semantic_verifier,
                    enable_behavior_target=enable_behavior_target,
                    ablation_config=config,
                    _adaptive_disabled=True,
                    _forced_seed_index=None,
                )
            prepared_repo_path = ""
            prepared_meta: dict[str, Any] | None = None
            if not generate_only:
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
                        observation_oracle_enabled=enable_observation_oracle,
                        strict_verifier_enabled=enable_strict_semantic_verifier,
                        mutation_plan_calls=0,
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
                    max_feedback_rounds,
                    max_env_rounds,
                    max_brt_rounds,
                    max_patch_rounds,
                    validation_mode,
                    patched_repo_base,
                    patch_file,
                    generate_only,
                    enable_protocol_recovery,
                    enable_seed_mutation,
                    enable_observation_oracle,
                    enable_strict_semantic_verifier,
                    enable_behavior_target=enable_behavior_target,
                    ablation_config=config,
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
                attempt = {
                    "seed_index": seed_index,
                    "seed_file": seed.file,
                    "seed_name": seed.name,
                    "status": summary.get("status"),
                    "score": score,
                    "checkpoint": checkpoint,
                    "summary_path": str(summary_path),
                    "final_test_path": str(seed_dir / "final_test.py"),
                    "mutation_plan_calls": int(
                        summary.get("mutation_plan_calls") or 0
                    ),
                    "mutation_plan_valid_calls": int(
                        summary.get("mutation_plan_valid_calls") or 0
                    ),
                    "mutation_plan_invalid_calls": int(
                        summary.get("mutation_plan_invalid_calls") or 0
                    ),
                    "mutation_plan_abstentions": int(
                        summary.get("mutation_plan_abstentions") or 0
                    ),
                    "trigger_replan_calls": int(
                        summary.get("trigger_replan_calls") or 0
                    ),
                    "repair_route_counts": dict(
                        summary.get("repair_route_counts") or {}
                    ),
                }
                attempts.append(attempt)
                order_key = (score, -seed_index, -int(checkpoint.get("round_id") or 0))
                if best is None or order_key > (best[0], best[1], best[2]):
                    best = (order_key[0], order_key[1], order_key[2], seed_dir, summary, checkpoint)
                try_next, reason = _should_try_next_seed(
                    summary, checkpoint, seed_index < len(seeds_to_try) - 1
                )
                attempts[-1]["switch_decision"] = "try_next_seed" if try_next else "stop"
                attempts[-1]["switch_reason"] = reason
                if try_next:
                    switch_reasons.append(f"seed_{seed_index}: {reason}")
                    continue
                break
            assert best is not None
            _, _, _, selected_dir, selected_summary, selected_checkpoint = best
            selected_seed_index = int(selected_dir.name.rsplit("_", 1)[-1])
            selected_seed_plan_calls = int(
                selected_summary.get("mutation_plan_calls") or 0
            )
            selected_seed_routes = dict(
                selected_summary.get("repair_route_counts") or {}
            )
            all_seed_plan_calls = sum(
                int(item.get("mutation_plan_calls") or 0) for item in attempts
            )
            all_seed_valid_calls = sum(
                int(item.get("mutation_plan_valid_calls") or 0)
                for item in attempts
            )
            all_seed_invalid_calls = sum(
                int(item.get("mutation_plan_invalid_calls") or 0)
                for item in attempts
            )
            all_seed_abstentions = sum(
                int(item.get("mutation_plan_abstentions") or 0)
                for item in attempts
            )
            all_seed_replans = sum(
                int(item.get("trigger_replan_calls") or 0)
                for item in attempts
            )
            all_seed_routes = {
                route: sum(
                    int((item.get("repair_route_counts") or {}).get(route) or 0)
                    for item in attempts
                )
                for route in _empty_repair_route_counts()
            }
            for name in (
                "final_test.py",
                "summary.json",
                "host_context.json",
                "protocol_recovery.json",
                "candidate_ranking.json",
                "dual_version_result.json",
                "repo_prepare.json",
                "icore_exec_spec.json",
                "worktree",
            ):
                _copy_if_exists(selected_dir, Path(output_dir), name)
            top_final = Path(output_dir) / "final_test.py"
            selected_summary.update(
                {
                    "final_test_path": str(top_final),
                    "seed_mode": "adaptive_top3",
                    "selected_seed_index": selected_seed_index,
                    "seed_attempts_count": len(attempts),
                    "seed_attempts_summary": attempts,
                    "seed_switch_reasons": switch_reasons,
                    "selected_seed_reason": selected_checkpoint.get("reason")
                    or selected_summary.get("final_reason")
                    or "selected by adaptive seed score",
                    "final_oracle_risk": {},
                    "final_surrogate_risk": {},
                    "behavior_target_enabled": enable_behavior_target,
                    "method_variant": config.method_variant,
                    "ablation_id": config.ablation_id,
                    "ablation_signature": config.signature,
                    "ablation_config": config.to_dict(),
                    "selected_seed_mutation_plan_calls": selected_seed_plan_calls,
                    "all_seed_mutation_plan_calls": all_seed_plan_calls,
                    "mutation_plan_calls": all_seed_plan_calls,
                    "mutation_plan_valid_calls": all_seed_valid_calls,
                    "mutation_plan_invalid_calls": all_seed_invalid_calls,
                    "mutation_plan_abstentions": all_seed_abstentions,
                    "trigger_replan_calls": all_seed_replans,
                    "selected_seed_repair_route_counts": selected_seed_routes,
                    "all_seed_repair_route_counts": all_seed_routes,
                    "repair_route_counts": all_seed_routes,
                }
            )
            safe_json_dump(attempts, str(Path(output_dir) / "seed_attempts_summary.json"))
            safe_json_dump(
                {
                    "selected_seed_index": selected_seed_index,
                    "selected_seed_dir": str(selected_dir),
                    "selected_seed_reason": selected_summary["selected_seed_reason"],
                },
                str(Path(output_dir) / "selected_seed_summary.json"),
            )
            safe_json_dump(selected_summary, str(Path(output_dir) / "summary.json"))
            return FinalResult(
                instance_id=context.instance_id,
                status=str(selected_summary.get("status") or ""),
                final_test_path=str(top_final),
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
                seed_attempts_count=len(attempts),
                seed_attempts_summary=attempts,
                seed_switch_reasons=switch_reasons,
                selected_seed_reason=str(selected_summary.get("selected_seed_reason") or ""),
                final_oracle_risk=selected_summary.get("final_oracle_risk") or {},
                final_surrogate_risk=selected_summary.get("final_surrogate_risk") or {},
                final_reason=str(selected_summary.get("final_reason") or ""),
                mutation_ops=list(selected_summary.get("mutation_ops") or []),
                mutation_plan_calls=int(
                    selected_summary.get("mutation_plan_calls") or 0
                ),
                mutation_plan_valid_calls=int(
                    selected_summary.get("mutation_plan_valid_calls") or 0
                ),
                mutation_plan_invalid_calls=int(
                    selected_summary.get("mutation_plan_invalid_calls") or 0
                ),
                mutation_plan_abstentions=int(
                    selected_summary.get("mutation_plan_abstentions") or 0
                ),
                selected_seed_mutation_plan_calls=selected_seed_plan_calls,
                all_seed_mutation_plan_calls=all_seed_plan_calls,
                trigger_replan_calls=int(
                    selected_summary.get("trigger_replan_calls") or 0
                ),
                final_mutation_plan_status=str(
                    selected_summary.get("final_mutation_plan_status") or ""
                ),
                final_mutation_plan_risk=str(
                    selected_summary.get("final_mutation_plan_risk") or ""
                ),
                final_mutation_adherence=dict(
                    selected_summary.get("final_mutation_adherence") or {}
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
                    observation_oracle_enabled=enable_observation_oracle,
                    strict_verifier_enabled=enable_strict_semantic_verifier,
                    mutation_plan_calls=0,
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
                protocol.protocol_risks.append(f"协议模型审计失败，保留 AST 恢复结果：{exc}")
            protocol.save_json(str(Path(output_dir) / "protocol_recovery.json"))
        host.save_json(str(Path(output_dir) / "host_context.json"))
        candidate = None
        execution = None
        decision = None
        observation = None
        dual = None
        final_code = ""
        mutation_plans = []
        trigger_replan_calls = 0
        repair_route_counts = _empty_repair_route_counts()
        strict_result = None
        oracle_type = ""
        oracle_rebound = False
        env_budget = max_env_rounds if max_env_rounds is not None else max_feedback_rounds
        brt_budget = max_brt_rounds if max_brt_rounds is not None else max_feedback_rounds
        initial_plan = _build_plan_or_invalid(
            context.instance_id,
            0,
            output_dir,
            behavior,
            host,
            protocol,
            llm_client,
            related_source=context.retrieved_code,
            related_test=related_test,
            buggy_repo=context.buggy_repo_path,
        ) if enable_seed_mutation else None
        if initial_plan is not None:
            mutation_plans.append(initial_plan)
        usable_initial_plan = (
            initial_plan
            if initial_plan is not None and initial_plan.is_usable
            else None
        )
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
            mutation_plan=usable_initial_plan,
            ablation_config=config,
            issue_text=context.issue_text,
        )
        if usable_initial_plan is not None:
            write_text(
                str(Path(output_dir) / "mutation_round_0_test.py"), candidate.code
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
                observation_oracle_enabled=enable_observation_oracle,
                strict_verifier_enabled=enable_strict_semantic_verifier,
                selected_seed_file=related_test.file if related_test else "",
                selected_seed_name=related_test.name if related_test else "",
                seed_fallback_used=seed_fallback_used,
                **_mutation_result_fields(
                    mutation_plans, trigger_replan_calls, candidate
                ),
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

        env_rounds_used = 0
        # Generic Iteration owns all candidate-level repairs, so it goes directly
        # to the common execute/verify/repair loop.  Infrastructure preparation
        # above remains identical for every variant.
        if config.specialized_feedback:
            effective_env_budget = max(1, env_budget) if config.environment_feedback else 1
            for env_round in range(effective_env_budget):
                execution = run_command_in_conda(candidate.command, context.buggy_repo_path, conda_env, timeout, no_conda, behavior, context.instance_id)
                safe_json_dump(execution.to_dict(), str(Path(output_dir) / f"env_execution_round_{env_round}.json"))
                write_text(str(Path(output_dir) / "logs" / f"env_execution_round_{env_round}.log"), execution.stdout + "\n" + execution.stderr)
                env_rounds_used = env_round + 1
                if execution.status not in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
                    break
                if not config.environment_feedback:
                    break
                if execution.status == "SETUP_ERROR" and _recover_declared_dependency(
                    context,
                    execution,
                    conda_env,
                    timeout,
                    no_conda,
                    output_dir,
                    env_round,
                ):
                    repair_route_counts["dependency_recovery"] += 1
                    continue
                if env_round == effective_env_budget - 1:
                    break
                candidate = repair_candidate(
                    context.instance_id,
                    behavior,
                    host,
                    candidate,
                    execution,
                    llm_client,
                    output_dir,
                    env_round + 1,
                    "setup",
                    context.retrieved_code,
                    buggy_repo=context.buggy_repo_path,
                    protocol=protocol,
                    ablation_config=config,
                    issue_text=context.issue_text,
                )
                repair_route_counts["environment"] += 1
                _refresh_candidate_command(context, candidate)
        if (
            config.specialized_feedback
            and execution is not None
            and execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}
        ):
            final_code = candidate.code
            write_text(str(Path(output_dir) / "final_test.py"), final_code)
            candidate_selector = first_test_selector(final_code)
            placement_dir = str(Path(candidate.candidate_repo_path).parent)
            result = FinalResult(
                instance_id=context.instance_id,
                status="ENV_UNRESOLVED",
                final_test_path=str(Path(output_dir) / "final_test.py"),
                rounds_used=env_rounds_used,
                buggy_execution=execution.to_dict(),
                dual_version_result={
                    "mode": validation_mode,
                    "status": "SKIPPED_ENV_UNRESOLVED",
                },
                **_evidence_result_fields(behavior, config),
                host_context=host.to_dict(),
                observation_report={},
                notes=(
                    f"environment probe remained {execution.status} after "
                    f"{env_rounds_used} rounds; BRT and dual-version validation skipped"
                ),
                protocol_recovery_enabled=enable_protocol_recovery,
                seed_mutation_enabled=enable_seed_mutation,
                observation_oracle_enabled=enable_observation_oracle,
                strict_verifier_enabled=enable_strict_semantic_verifier,
                selected_seed_file=related_test.file if related_test else "",
                selected_seed_name=related_test.name if related_test else "",
                seed_fallback_used=seed_fallback_used,
                **_mutation_result_fields(
                    mutation_plans, trigger_replan_calls, candidate
                ),
                repair_route_counts=repair_route_counts,
                final_reason="environment qualification remained unresolved",
                seed_mode=(
                    "single_forced_seed"
                    if _forced_seed_index is not None
                    else "single_seed"
                ),
                selected_seed_index=0,
                seed_attempts_count=len(seed_attempts),
                seed_attempts_summary=seed_attempts,
                candidate_repo_path=candidate.candidate_repo_path,
                pytest_nodeid=candidate.pytest_nodeid,
                command=candidate.command,
                direct_test_repo_path_hint=candidate.candidate_repo_path,
                placement_dir=placement_dir,
                runner_kind=context.repo.split("/")[-1],
                selector=candidate_selector,
            )
            result.save_json(str(Path(output_dir) / "summary.json"))
            return result
        else:
            brt_attempt = 0
            semantic_repairs_used = 0
            late_setup_repairs_used = 0
            # Round 0 is the initial BRT. Environment qualification already
            # has its own budget above and must not expand this checkpoint loop.
            max_brt_attempts = 1 + max(
                0,
                max_feedback_rounds
                if not config.specialized_feedback
                else brt_budget,
            )
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
            while brt_attempt < max_brt_attempts:
                if brt_attempt > 0 or execution is None:
                    execution = run_command_in_conda(candidate.command, context.buggy_repo_path, conda_env, timeout, no_conda, behavior, context.instance_id)
                safe_json_dump(execution.to_dict(), str(Path(output_dir) / f"execution_round_{brt_attempt}.json"))
                write_text(str(Path(output_dir) / "logs" / f"execution_round_{brt_attempt}.log"), execution.stdout + "\n" + execution.stderr)
                effective_source = format_effective_source_context(
                    behavior, context.retrieved_code, context.buggy_repo_path
                )
                if enable_strict_semantic_verifier:
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
                )
                checkpoints.append(checkpoint)
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
                if decision.decision == "accept":
                    # Accept ends repair for this seed only. The outer fixed
                    # top-3 loop still evaluates later iCoRe seeds before rank.
                    break
                if not config.specialized_feedback:
                    if semantic_repairs_used >= max(0, max_feedback_rounds):
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    next_round = env_rounds_used + brt_attempt + 1
                    candidate = repair_candidate(
                        context.instance_id,
                        behavior,
                        host,
                        candidate,
                        execution,
                        llm_client,
                        output_dir,
                        next_round,
                        "generic",
                        context.retrieved_code,
                        json.dumps(
                            observation.to_dict() if observation else {},
                            ensure_ascii=False,
                        ),
                        semantic_feedback,
                        context.buggy_repo_path,
                        protocol,
                        None,
                        ablation_config=config,
                        issue_text=context.issue_text,
                    )
                    repair_route_counts["generic"] += 1
                    semantic_repairs_used += 1
                    _refresh_candidate_command(context, candidate)
                    brt_attempt += 1
                    continue
                focus = _repair_focus(decision, strict_result, execution)
                if focus == "reject":
                    final_code = candidate.code
                    write_text(str(Path(output_dir) / "final_test.py"), final_code)
                    break
                if focus == "setup":
                    if not config.environment_feedback:
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    if late_setup_repairs_used >= env_budget:
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    late_setup_repairs_used += 1
                elif focus == "oracle":
                    if not config.assertion_feedback:
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    if semantic_repairs_used >= max(0, brt_budget):
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    next_round = env_rounds_used + brt_attempt + 1
                    # Oracle feedback first uses the complete Issue and all
                    # shared evidence. A buggy-only observation probe is not a
                    # source of expected values and is therefore not the
                    # default repair mechanism.
                    candidate = repair_candidate(
                        context.instance_id,
                        behavior,
                        host,
                        candidate,
                        execution,
                        llm_client,
                        output_dir,
                        next_round,
                        "oracle",
                        context.retrieved_code,
                        json.dumps(
                            observation.to_dict() if observation else {},
                            ensure_ascii=False,
                        ),
                        semantic_feedback,
                        context.buggy_repo_path,
                        protocol,
                        None,
                        ablation_config=config,
                        issue_text=context.issue_text,
                    )
                    final_code = candidate.code
                    contract = oracle_contract_summary(behavior, final_code)
                    oracle_type = ",".join(contract.get("kinds") or [])
                    write_text(
                        str(Path(output_dir) / f"candidate_round_{next_round}.py"),
                        final_code,
                    )
                    _refresh_candidate_command(context, candidate)
                    semantic_repairs_used += 1
                    repair_route_counts["assertion"] += 1
                    brt_attempt += 1
                    continue
                else:
                    if not config.trigger_feedback:
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    if semantic_repairs_used >= max(0, brt_budget):
                        final_code = candidate.code
                        write_text(str(Path(output_dir) / "final_test.py"), final_code)
                        break
                    semantic_repairs_used += 1
                mutation_plan = None
                explicit_trigger_failure = bool(
                    decision.decision == "repair_trigger"
                    or (
                        strict_result is not None
                        and strict_result.failure_class
                        in {"buggy_pass", "target_not_hit"}
                    )
                )
                if (
                    focus == "trigger"
                    and enable_seed_mutation
                    and explicit_trigger_failure
                    and trigger_replan_calls < 1
                ):
                    mutation_plan = _build_plan_or_invalid(
                        context.instance_id,
                        env_rounds_used + brt_attempt + 1,
                        output_dir,
                        behavior,
                        host,
                        protocol,
                        llm_client,
                        execution_feedback=(
                            execution.stdout + "\n" + execution.stderr
                        ),
                        verifier_feedback=semantic_feedback,
                        related_source=context.retrieved_code,
                        related_test=related_test,
                        buggy_repo=context.buggy_repo_path,
                    )
                    mutation_plans.append(mutation_plan)
                    trigger_replan_calls += 1
                usable_mutation_plan = (
                    mutation_plan
                    if mutation_plan is not None and mutation_plan.is_usable
                    else None
                )
                candidate = repair_candidate(
                    context.instance_id,
                    behavior,
                    host,
                    candidate,
                    execution,
                    llm_client,
                    output_dir,
                    env_rounds_used + brt_attempt + 1,
                    focus,
                    context.retrieved_code,
                    json.dumps(observation.to_dict() if observation else {}, ensure_ascii=False),
                    semantic_feedback,
                    context.buggy_repo_path,
                    protocol,
                    usable_mutation_plan,
                    ablation_config=config,
                    issue_text=context.issue_text,
                )
                repair_route_counts[
                    "environment" if focus == "setup" else "trigger"
                ] += 1
                if usable_mutation_plan is not None:
                    write_text(
                        str(
                            Path(output_dir)
                            / f"mutation_round_{usable_mutation_plan.round_id}_test.py"
                        ),
                        candidate.code,
                    )
                _refresh_candidate_command(context, candidate)
                brt_attempt += 1
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
                safe_json_dump(
                    {
                        "selection_policy": (
                            "Hard eligibility (executable buggy fail, falsifiable Oracle, "
                            "no semantic/plan/Oracle-preservation violation) > LLM accept > "
                            "issue_aligned > semantic target_hit > issue-grounded Oracle > "
                            "public behavior > Oracle risk > earliest repair round"
                        ),
                        "selected_attempt": checkpoints[best_index].round_id,
                        "checkpoints": [item.to_dict() for item in checkpoints],
                    },
                    str(Path(output_dir) / "candidate_ranking.json"),
                )
        assert candidate is not None and execution is not None
        dual = DualVersionResult(
            context.instance_id,
            "buggy_only",
            execution.to_dict(),
            {},
            "SKIPPED_NO_SURROGATE",
            "Surrogate patch generation and validation are disabled by method definition.",
        )
        dual.save_json(str(Path(output_dir) / "dual_version_result.json"))
        write_text(str(Path(output_dir) / "final_test.py"), final_code or candidate.code)
        final_oracle_risk = {}
        final_surrogate_risk = {}
        candidate_selector = first_test_selector(final_code or candidate.code)
        placement_dir = str(Path(candidate.candidate_repo_path).parent)
        if decision is not None and decision.decision == "accept":
            status = "ISSUE_ALIGNED_FAIL"
        elif execution.status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR", "TIMEOUT"}:
            status = execution.status
        elif execution.returncode != 0:
            # Executor keyword matching is only a triage hint. A rejected
            # verifier decision must never become an accepted issue failure.
            status = "UNRELATED_FAIL"
        else:
            status = execution.status
        result = FinalResult(
            instance_id=context.instance_id,
            status=status,
            final_test_path=str(Path(output_dir) / "final_test.py"),
            rounds_used=(candidate.round_id + 1),
            buggy_execution=execution.to_dict(),
            dual_version_result=dual.to_dict(),
            **_evidence_result_fields(behavior, config),
            host_context=host.to_dict(),
            observation_report=observation.to_dict() if observation else {},
            notes=decision.reason if decision else "",
            protocol_recovery_enabled=enable_protocol_recovery,
            seed_mutation_enabled=enable_seed_mutation,
            observation_oracle_enabled=enable_observation_oracle,
            strict_verifier_enabled=enable_strict_semantic_verifier,
            selected_seed_file=related_test.file if related_test else "",
            selected_seed_name=related_test.name if related_test else "",
            seed_fallback_used=seed_fallback_used,
            **_mutation_result_fields(
                mutation_plans, trigger_replan_calls, candidate
            ),
            repair_route_counts=repair_route_counts,
            oracle_type=oracle_type,
            strict_verifier_decision=strict_result.decision if strict_result else "",
            strict_failure_class=strict_result.failure_class if strict_result else "",
            oracle_rebound=oracle_rebound,
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
            "observation_oracle_enabled": enable_observation_oracle,
            "strict_verifier_enabled": enable_strict_semantic_verifier,
            "behavior_target_enabled": enable_behavior_target,
            "method_variant": fallback_config.method_variant,
            "ablation_id": fallback_config.ablation_id,
            "ablation_signature": fallback_config.signature,
            "ablation_config": fallback_config.to_dict(),
            "selected_seed_file": "",
            "selected_seed_name": "",
            "seed_fallback_used": False,
            "mutation_ops": [],
            "mutation_plan_calls": 0,
            "repair_route_counts": _empty_repair_route_counts(),
            "oracle_type": "",
            "strict_verifier_decision": "",
            "strict_failure_class": "",
            "oracle_rebound": False,
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
            mutation_plan_calls=0,
            repair_route_counts=_empty_repair_route_counts(),
        )

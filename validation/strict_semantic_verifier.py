"""LLM semantic acceptance gate for tests executed on the buggy source."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.prompts import (
    STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT,
    STRICT_SEMANTIC_VERIFIER_USER_PROMPT,
)
from ..core.ablation import (
    AblationConfig,
    behavior_prompt_payload,
    render_ablation_prompt,
)
from ..core.behavior_evidence import BehaviorEvidence, render_evidence_prompt
from ..core.schema import (
    CandidateTest,
    ExecutionResult,
    ProtocolRecovery,
    StrictVerifierResult,
    VerifierDecision,
)
from ..core.utils import extract_json_object, safe_json_dump, truncate_text, write_text
from .semantic_guard import oracle_contract_summary


_DECISIONS = {"accept", "repair_setup", "repair_trigger", "repair_oracle", "reject"}
_FAILURE_CLASSES = {
    "setup",
    "syntax",
    "collect",
    "timeout",
    "buggy_pass",
    "target_not_hit",
    "side_path",
    "oracle_wrong",
    "oracle_too_strong",
    "issue_aligned",
}
_POST_FIX_RISKS = {"low", "medium", "high", "unknown"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _forced_result(
    instance_id: str,
    execution: ExecutionResult,
    reason: str,
) -> StrictVerifierResult | None:
    """Handle only mechanical outcomes; semantic failures always go to the LLM."""
    mapping = {
        "SETUP_ERROR": ("repair_setup", "setup"),
        "SYNTAX_ERROR": ("repair_setup", "syntax"),
        "COLLECT_ERROR": ("repair_setup", "collect"),
        "TIMEOUT": ("reject", "timeout"),
    }
    if execution.status not in mapping:
        return None
    decision, failure = mapping[execution.status]
    return StrictVerifierResult(
        instance_id=instance_id,
        decision=decision,
        failure_class=failure,
        target_hit=False,
        oracle_grounded_in_issue=False,
        uses_public_behavior=False,
        oracle_kind="",
        oracle_falsifiable=False,
        reason=reason or execution.status,
        next_action=decision,
        observed_behavior=reason or execution.status,
        semantic_gap=("reach the target behavior" if execution.status == "PASS" else "restore executable setup"),
        preserve=["the selected seed protocol and Issue-grounded behavior"],
        change=[decision.replace("repair_", "")],
        avoid=[execution.status],
        next_operator=("CALL_CHAIN_EXTEND" if execution.status == "PASS" else "CONFIG_MUTATION"),
        expected_effect="buggy execution reaches an Issue-aligned, falsifiable failure",
        failure_origin=("test_body" if execution.status == "PASS" else "setup"),
        post_fix_failure_risk="unknown",
    )


def verify_strict_semantics(
    issue_text: str,
    behavior: BehaviorEvidence,
    protocol: ProtocolRecovery | None,
    candidate: CandidateTest,
    execution: ExecutionResult,
    source_context: str,
    llm_client: Any,
    output_dir: str,
    round_id: int,
    ablation_config: AblationConfig | None = None,
) -> tuple[VerifierDecision, StrictVerifierResult]:
    """Judge Issue alignment from the real buggy execution and full Issue text."""
    config = (ablation_config or AblationConfig()).validate()
    result = _forced_result(candidate.instance_id, execution, execution.status)
    if result is None:
        static_oracle = oracle_contract_summary(behavior, candidate.code)
        prompt = STRICT_SEMANTIC_VERIFIER_USER_PROMPT.format(
            issue_text=issue_text,
            behavior_json=json.dumps(
                behavior_prompt_payload(behavior, config), ensure_ascii=False
            ),
            protocol_json=json.dumps(
                protocol.to_dict() if protocol else {}, ensure_ascii=False
            ),
            candidate_code=candidate.code,
            command=execution.command,
            execution_status=execution.status,
            execution_log=truncate_text(
                execution.stdout + "\n" + execution.stderr, 16000
            ),
            source_context=truncate_text(source_context, 18000),
        )
        prompt = render_evidence_prompt(prompt, behavior)
        prompt = render_ablation_prompt(prompt, config)
        system_prompt = render_ablation_prompt(
            STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT,
            config,
            include_banner=False,
        )
        write_text(
            str(
                Path(output_dir)
                / "prompts"
                / f"strict_verifier_round_{round_id}.txt"
            ),
            system_prompt + "\n\n" + prompt,
        )
        response = llm_client.chat(system_prompt, prompt)
        write_text(
            str(
                Path(output_dir)
                / "responses"
                / f"strict_verifier_round_{round_id}.txt"
            ),
            response,
        )
        data = extract_json_object(response)
        decision = str(data.get("decision") or "repair_trigger")
        failure = str(data.get("failure_class") or "side_path")
        result = StrictVerifierResult(
            instance_id=candidate.instance_id,
            decision=decision if decision in _DECISIONS else "repair_trigger",
            failure_class=(
                failure if failure in _FAILURE_CLASSES else "side_path"
            ),
            target_hit=_as_bool(data.get("target_hit")),
            oracle_grounded_in_issue=_as_bool(
                data.get("oracle_grounded_in_issue")
            ),
            uses_public_behavior=_as_bool(data.get("uses_public_behavior")),
            oracle_kind=str(data.get("oracle_kind") or ",".join(static_oracle["kinds"])),
            oracle_falsifiable=bool(static_oracle["falsifiable"])
            or _as_bool(data.get("oracle_falsifiable")),
            reason=str(data.get("reason") or ""),
            next_action=str(data.get("next_action") or decision),
            observed_behavior=str(data.get("observed_behavior") or ""),
            target_behavior=str(data.get("target_behavior") or ""),
            semantic_gap=str(data.get("semantic_gap") or data.get("gap") or ""),
            preserve=_strings(data.get("preserve")),
            # Each execution round changes one semantic coordinate only.
            change=_strings(data.get("change"))[:1],
            avoid=_strings(data.get("avoid")),
            next_operator=str(data.get("next_operator") or data.get("operator") or ""),
            expected_effect=str(data.get("expected_effect") or ""),
            failure_origin=str(data.get("failure_origin") or ""),
            post_fix_failure_risk=(
                str(data.get("post_fix_failure_risk") or "unknown").lower()
                if str(data.get("post_fix_failure_risk") or "unknown").lower() in _POST_FIX_RISKS
                else "unknown"
            ),
        )

        if result.decision == "accept":
            executable_buggy_fail = (
                execution.returncode != 0
                and execution.status
                not in {
                    "SETUP_ERROR",
                    "SYNTAX_ERROR",
                    "COLLECT_ERROR",
                    "TIMEOUT",
                }
            )
            if not executable_buggy_fail:
                result.decision = "repair_trigger"
                result.failure_class = "buggy_pass"
                result.next_action = result.decision
                result.reason = "buggy 版本没有形成可执行失败。" + result.reason
            elif result.failure_class != "issue_aligned" or not result.target_hit:
                result.decision = "repair_trigger"
                result.failure_class = "target_not_hit"
                result.next_action = result.decision
                result.reason = "LLM 未确认失败路径与 Issue 对齐。" + result.reason
            elif not (
                result.oracle_grounded_in_issue
                and result.uses_public_behavior
                and result.oracle_falsifiable
            ):
                result.decision = "repair_oracle"
                result.failure_class = "oracle_wrong"
                result.next_action = result.decision
                result.reason = (
                    "未确认 Oracle 来自 Issue、使用公开行为且具有可证伪协议。"
                    + result.reason
                )
            elif result.post_fix_failure_risk == "high":
                result.decision = "repair_oracle"
                result.failure_class = "oracle_too_strong"
                result.next_action = result.decision
                result.semantic_gap = result.semantic_gap or "remove unrelated or over-constrained oracle clauses"
                result.reason = (
                    "即使目标修复实现，当前测试仍有高风险因无关约束失败。" + result.reason
                )

    safe_json_dump(
        result.to_dict(),
        str(Path(output_dir) / f"strict_verifier_round_{round_id}.json"),
    )
    decision = VerifierDecision(
        instance_id=candidate.instance_id,
        decision=result.decision,
        reason=result.reason,
        focus=[result.next_action.replace("repair_", "")],
        next_action=result.next_action,
    )
    return decision, result

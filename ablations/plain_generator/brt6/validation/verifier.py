"""Buggy-only verifier."""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.prompts import (
    BUGGY_ONLY_VERIFIER_SYSTEM_PROMPT,
    BUGGY_ONLY_VERIFIER_USER_PROMPT,
)
from ..core.ablation import (
    AblationConfig,
    behavior_prompt_payload,
    render_ablation_prompt,
)
from ..core.behavior_evidence import (
    BehaviorEvidence,
    issue_evidence_text,
    render_evidence_prompt,
)
from ..core.schema import CandidateTest, ExecutionResult, VerifierDecision
from ..core.utils import extract_json_object
from ..llm.errors import LLMUnavailableError


def _missing_check_target(
    behavior: BehaviorEvidence,
    source_context: str,
) -> bool:
    text = issue_evidence_text(behavior).lower()
    mentions_check = any(marker in text for marker in ("check", "检查", "验证"))
    describes_missing = any(
        marker in text
        for marker in ("add check", "missing", "no check", "缺少", "没有", "新增", "添加")
    )
    return (
        mentions_check
        and describes_missing
        and "def check(" in source_context
    )


def _missing_logging_target(behavior: BehaviorEvidence) -> bool:
    text = issue_evidence_text(behavior).lower()
    mentions_logging = any(
        marker in text
        for marker in ("logger", "logging", "log message", "日志", "记录异常")
    )
    describes_missing = any(
        marker in text
        for marker in ("missing", "doesn't have", "没有", "缺少", "未记录")
    )
    return mentions_logging and describes_missing


def _normalize_focus(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return default


def _ask_llm(
    issue_text: str,
    behavior: BehaviorEvidence,
    candidate: CandidateTest,
    execution: ExecutionResult,
    llm_client: Any,
    default_decision: str,
    host_context: dict[str, Any] | None = None,
    source_context: str = "",
    ablation_config: AblationConfig | None = None,
) -> VerifierDecision:
    config = (ablation_config or AblationConfig()).validate()
    prompt = BUGGY_ONLY_VERIFIER_USER_PROMPT.format(
        issue_text=issue_text,
        behavior_json=json.dumps(
            behavior_prompt_payload(behavior, config), ensure_ascii=False
        ),
        host_context_json=json.dumps(host_context or {}, ensure_ascii=False),
        source_context=source_context,
        candidate_code=candidate.code,
        execution_json=json.dumps(execution.to_dict(), ensure_ascii=False),
    )
    prompt = render_evidence_prompt(prompt, behavior)
    prompt = render_ablation_prompt(prompt, config)
    system_prompt = render_ablation_prompt(
        BUGGY_ONLY_VERIFIER_SYSTEM_PROMPT, config, include_banner=False
    )
    data = extract_json_object(
        llm_client.chat(system_prompt, prompt)
    )
    decision = str(data.get("decision") or default_decision)
    if decision not in {
        "accept",
        "repair_setup",
        "repair_trigger",
        "repair_oracle",
        "reject",
    }:
        decision = default_decision
    result = VerifierDecision(
        candidate.instance_id,
        decision,
        str(data.get("reason") or ""),
        _normalize_focus(data.get("focus"), ["oracle"]),
        str(data.get("next_action") or ""),
    )
    if result.decision == "accept":
        reason = result.reason.lower()
        setup_markers = (
            "测试设置问题", "setup error", "setup问题", "环境问题", "环境配置问题",
            "测试自身异常", "未注册", "不存在的 fixture", "collection error",
        )
        trigger_markers = (
            "未触发", "没有触发", "未执行到", "没有执行到", "触发条件不满足",
            "路径无关", "与issue无关", "unrelated failure", "did not trigger",
        )
        oracle_markers = (
            "断言方向错误", "断言对象错误", "断言过于", "oracle错误",
            "期望行为不符", "把buggy行为", "assertion direction", "wrong oracle",
        )
        if any(marker in reason for marker in setup_markers):
            result.decision = "repair_setup"
            result.focus = ["setup"]
            result.next_action = result.next_action or "Restore an executable test context, then run again."
        elif any(marker in reason for marker in trigger_markers) or re.search(
            r"未(?:能|能正确|正确)?[^。；，,]{0,12}触发", reason
        ):
            result.decision = "repair_trigger"
            result.focus = ["trigger"]
            result.next_action = result.next_action or "Repair the trigger using the issue's exact input and call path."
        elif any(marker in reason for marker in oracle_markers):
            result.decision = "repair_oracle"
            result.focus = ["oracle"]
            result.next_action = result.next_action or "Rewrite the smallest stable oracle from expected_behavior."
    return result


def verify_buggy_only(
    issue_text: str,
    behavior: BehaviorEvidence,
    candidate: CandidateTest,
    execution: ExecutionResult,
    llm_client: Any | None = None,
    host_context: dict[str, Any] | None = None,
    source_context: str = "",
    ablation_config: AblationConfig | None = None,
) -> VerifierDecision:
    status = execution.status
    missing_check_target = _missing_check_target(behavior, source_context)
    if status in {"SETUP_ERROR", "SYNTAX_ERROR", "COLLECT_ERROR"}:
        next_action = "Repair imports, fixtures, class context, setup, or syntax."
        if missing_check_target and ".check(" in candidate.code:
            next_action = (
                "Keep the real check() call. Repair only missing model binding, name, app_label, "
                "or related metadata. Prefer the HostContext model and obtain fields through "
                "_meta.get_field(); a standalone Field may use set_attributes_from_name()."
            )
        return VerifierDecision(
            candidate.instance_id,
            "repair_setup",
            status,
            ["setup"],
            next_action,
        )
    if status == "PASS":
        if missing_check_target:
            return VerifierDecision(
                candidate.instance_id,
                "repair_trigger",
                (
                    "This issue concerns a missing system check. Passing on the buggy version means "
                    "the test did not use check() to observe evidence expected after the fix."
                ),
                ["trigger", "oracle"],
                (
                    "Call the real check() lifecycle and assert the stable result required by "
                    "expected_behavior. Do not substitute clean(), save(), constructor exceptions, "
                    "or ordinary value-length checks."
                ),
            )
        if llm_client:
            try:
                decision = _ask_llm(
                    issue_text,
                    behavior,
                    candidate,
                    execution,
                    llm_client,
                    "repair_trigger",
                    host_context,
                    source_context,
                    ablation_config,
                )
                if decision.decision == "accept":
                    decision.decision = "repair_trigger"
                    decision.reason = (
                        "The buggy version passes, so this cannot be accepted as a BRT."
                        + (f" Semantic analysis: {decision.reason}" if decision.reason else "")
                    )
                    decision.focus = ["trigger"]
                    decision.next_action = (
                        decision.next_action
                        or "Find the issue input, state, call chain, or optimized branch that remains uncovered."
                    )
                return decision
            except LLMUnavailableError:
                raise
            except Exception:  # noqa: BLE001
                pass
        return VerifierDecision(candidate.instance_id, "repair_trigger", "The buggy version passes, so the defect path was not triggered.", ["trigger"], "Strengthen the issue-grounded input, state, or call-chain adaptation.")
    if status in {"ISSUE_ALIGNED_FAIL", "ASSERTION_FAIL"}:
        if (
            status == "ASSERTION_FAIL"
            and missing_check_target
            and ".check(" in candidate.code
            and "assertRaises" not in candidate.code
            and "pytest.raises" not in candidate.code
        ):
            return VerifierDecision(
                candidate.instance_id,
                "accept",
                (
                    "The test invokes the real check() lifecycle and fails because evidence expected "
                    "after the fix is absent on the buggy version; this matches a missing-check issue."
                ),
                ["trigger", "oracle"],
                "Accept the current BRT.",
            )
        if (
            status == "ASSERTION_FAIL"
            and _missing_logging_target(behavior)
            and ("assertLogs" in candidate.code or "caplog" in candidate.code)
        ):
            return VerifierDecision(
                candidate.instance_id,
                "accept",
                (
                    "The test invokes the target behavior and captures the log expected after the fix. "
                    "It fails on the buggy version because that evidence is absent, matching the issue."
                ),
                ["trigger", "oracle"],
                "Accept the current BRT.",
            )
        if llm_client:
            try:
                return _ask_llm(
                    issue_text,
                    behavior,
                    candidate,
                    execution,
                    llm_client,
                    "repair_oracle" if status == "ASSERTION_FAIL" else "repair_trigger",
                    host_context,
                    source_context,
                    ablation_config,
                )
            except LLMUnavailableError:
                raise
            except Exception:  # noqa: BLE001
                pass
        if status == "ASSERTION_FAIL":
            return VerifierDecision(candidate.instance_id, "repair_oracle", "The assertion fails, but issue alignment is uncertain.", ["oracle"], "Observe the behavior, then rewrite the assertion.")
        return VerifierDecision(candidate.instance_id, "repair_trigger", "Keyword rules suggest relevance, but semantic validation is incomplete.", ["trigger", "oracle"], "Recheck the target API, input, and failure semantics.")
    if status == "UNRELATED_FAIL" and llm_client:
        try:
            return _ask_llm(
                issue_text,
                behavior,
                candidate,
                execution,
                llm_client,
                "repair_trigger",
                host_context,
                source_context,
                ablation_config,
            )
        except LLMUnavailableError:
            raise
        except Exception:  # noqa: BLE001
            pass
    if status == "TIMEOUT":
        return VerifierDecision(candidate.instance_id, "reject", "Execution timed out.", ["setup"], "Abandon or reduce the test.")
    return VerifierDecision(candidate.instance_id, "repair_trigger", f"Failure type {status} is insufficiently aligned with the issue.", ["trigger", "oracle"], "Realign the trigger path.")

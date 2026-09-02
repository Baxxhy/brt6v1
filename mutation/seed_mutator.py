"""Create, parse, and validate a small issue-guided trigger plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.prompts import SEED_MUTATION_PLAN_SYSTEM_PROMPT, SEED_MUTATION_PLAN_USER_PROMPT
from ..core.behavior_evidence import BehaviorEvidence, render_evidence_prompt
from ..core.schema import (
    HostContext,
    MutationPlan,
    MutationStep,
    ProtocolRecovery,
    RetrievedCode,
    RetrievedTest,
)
from ..io.io_utils import format_code_context
from ..core.utils import extract_json_object, safe_json_dump, truncate_text, write_text
from ..validation.mutation_plan_validator import validate_mutation_plan


ALLOWED_MUTATION_OPS = {
    "ARG_VALUE_REPLACE", "ARG_BOUNDARY_EXPAND", "OPERATOR_FLIP",
    "CALL_CHAIN_EXTEND", "STATE_MUTATION", "FIXTURE_DATA_MUTATION",
    "CONFIG_MUTATION", "MOCK_BEHAVIOR_MUTATION", "LIFECYCLE_TRIGGER",
    "SERIALIZATION_TRIGGER", "WARNING_LOG_TRIGGER",
}

MAX_PROMPT_BEHAVIOR_CHARS = 30_000
MAX_PROMPT_HOST_CHARS = 30_000
MAX_PROMPT_PROTOCOL_CHARS = 20_000
MAX_PROMPT_SOURCE_CHARS = 60_000
MAX_PROMPT_SEED_CHARS = 40_000
MAX_PROMPT_EXECUTION_CHARS = 25_000
MAX_PROMPT_VERIFIER_CHARS = 12_000


def _prompt_text(value: Any, limit: int) -> str:
    return truncate_text(str(value or ""), limit)


def _prompt_json(value: Any, limit: int) -> str:
    return _prompt_text(json.dumps(value, ensure_ascii=False), limit)

_OP_ALIASES = {
    "OBJECT_STATE_MUTATION": "STATE_MUTATION",
    "CALL_CHAIN": "CALL_CHAIN_EXTEND",
    "ARGUMENT_VALUE_REPLACE": "ARG_VALUE_REPLACE",
    "BOUNDARY_EXPAND": "ARG_BOUNDARY_EXPAND",
}


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _first(value: Any) -> str:
    values = _strings(value)
    return values[0] if values else ""


def _risk(value: Any) -> str:
    risk = str(value or "medium").strip().lower()
    return risk if risk in {"low", "medium", "high"} else "medium"


def _op(value: Any) -> str:
    name = str(value or "").strip().upper()
    return _OP_ALIASES.get(name, name)


def _global_target_file(data: dict[str, Any]) -> str:
    candidates = _strings(data.get("target_file")) + _strings(data.get("target_path"))
    return next((item for item in candidates if item.replace("\\", "/").endswith(".py")), "")


def _global_target_symbol(data: dict[str, Any]) -> str:
    return _first(data.get("target_symbol")) or _first(data.get("target_api"))


def _step_from_object(
    item: dict[str, Any],
    data: dict[str, Any],
) -> MutationStep | None:
    op = _op(item.get("op") or item.get("operation") or item.get("type"))
    if op not in ALLOWED_MUTATION_OPS:
        return None
    target_file = str(
        item.get("target_file")
        or item.get("file")
        or _global_target_file(data)
        or ""
    ).strip()
    target_symbol = str(
        item.get("target_symbol")
        or item.get("target_api")
        or item.get("target")
        or _global_target_symbol(data)
        or ""
    ).strip()
    before = str(
        item.get("before")
        or item.get("from")
        or item.get("original")
        or item.get("original_pattern")
        or ""
    ).strip()
    after = str(
        item.get("after")
        or item.get("to")
        or item.get("new_value")
        or item.get("new_code")
        or item.get("action")
        or ""
    ).strip()
    return MutationStep(
        op=op,
        target_file=target_file,
        target_symbol=target_symbol,
        seed_anchor=str(
            item.get("seed_anchor")
            or item.get("anchor")
            or item.get("slot")
            or before
            or ""
        ).strip(),
        before=before,
        after=after,
        rationale=str(item.get("rationale") or item.get("reason") or "").strip(),
        risk=_risk(item.get("risk") or data.get("risk")),
    )


def _step_from_string(
    value: str,
    data: dict[str, Any],
) -> MutationStep | None:
    op = _op(value)
    if op not in ALLOWED_MUTATION_OPS:
        return None
    return MutationStep(
        op=op,
        target_file=_global_target_file(data),
        target_symbol=_global_target_symbol(data),
        seed_anchor=_first(data.get("seed_anchor")),
        before=str(data.get("before") or "").strip(),
        after=str(data.get("after") or "").strip(),
        rationale=str(
            data.get("why_target_will_be_reached")
            or data.get("why_this_should_trigger")
            or ""
        ).strip(),
        risk=_risk(data.get("risk")),
    )


def _normalize_plan(
    instance_id: str,
    round_id: int,
    data: dict[str, Any],
) -> MutationPlan:
    raw_steps = data.get("steps")
    if raw_steps is None:
        raw_steps = data.get("mutation_ops")
    structural_errors: list[str] = []
    steps: list[MutationStep] = []
    if isinstance(raw_steps, list):
        for index, item in enumerate(raw_steps[:3]):
            step = (
                _step_from_object(item, data)
                if isinstance(item, dict)
                else _step_from_string(str(item), data)
            )
            if step is None:
                structural_errors.append(f"step[{index}] has an unsupported operation")
            else:
                steps.append(step)
    elif raw_steps not in {None, ""}:
        structural_errors.append("steps must be a JSON array")
    requested_status = str(data.get("status") or "PROPOSED").strip().upper()
    if requested_status == "ABSTAIN" or (not steps and not structural_errors):
        status = "ABSTAIN"
    else:
        status = "INVALID" if structural_errors else "PROPOSED"
    return MutationPlan(
        instance_id=instance_id,
        round_id=round_id,
        status=status,
        trigger_goal=str(
            data.get("trigger_goal") or data.get("mutation_goal") or ""
        ).strip(),
        steps=steps,
        preserve_from_seed=_strings(data.get("preserve_from_seed")),
        why_target_will_be_reached=str(
            data.get("why_target_will_be_reached")
            or data.get("why_this_should_trigger")
            or data.get("reason")
            or ""
        ).strip(),
        risk=max(
            (step.risk for step in steps),
            key={"low": 0, "medium": 1, "high": 2}.get,
            default=_risk(data.get("risk")),
        ),
        validation_errors=structural_errors,
    )


def _failed_plan(instance_id: str, round_id: int, error: str) -> MutationPlan:
    return MutationPlan(
        instance_id=instance_id,
        round_id=round_id,
        status="INVALID",
        validation_errors=[error],
        validation_evidence={"planner_failure": True},
    )


def _save_plan(plan: MutationPlan, output_dir: str) -> MutationPlan:
    safe_json_dump(
        plan.to_dict(),
        str(Path(output_dir) / f"mutation_round_{plan.round_id}_plan.json"),
    )
    return plan


def build_mutation_plan(
    instance_id: str,
    round_id: int,
    behavior: BehaviorEvidence,
    host: HostContext,
    protocol: ProtocolRecovery | None,
    llm_client: Any,
    output_dir: str,
    execution_feedback: str = "",
    verifier_feedback: dict[str, Any] | None = None,
    related_source: list[RetrievedCode] | None = None,
    related_test: RetrievedTest | None = None,
    buggy_repo: str = "",
) -> MutationPlan:
    """Build one plan and degrade to an audited invalid plan on any failure."""

    source = related_source or []
    seed_code = related_test.code_content if related_test else host.seed_test_code
    prompt = SEED_MUTATION_PLAN_USER_PROMPT.format(
        behavior_json=_prompt_json(
            behavior.to_dict(), MAX_PROMPT_BEHAVIOR_CHARS
        ),
        host_context_json=_prompt_json(
            host.to_dict(), MAX_PROMPT_HOST_CHARS
        ),
        protocol_json=_prompt_json(
            protocol.to_dict() if protocol else {}, MAX_PROMPT_PROTOCOL_CHARS
        ),
        source_context=_prompt_text(
            format_code_context(source), MAX_PROMPT_SOURCE_CHARS
        ),
        seed_test_code=_prompt_text(seed_code, MAX_PROMPT_SEED_CHARS),
        execution_feedback=_prompt_text(
            execution_feedback or "无", MAX_PROMPT_EXECUTION_CHARS
        ),
        verifier_feedback=_prompt_json(
            verifier_feedback or {}, MAX_PROMPT_VERIFIER_CHARS
        ),
    )
    prompt = render_evidence_prompt(prompt, behavior)
    prompt_path = Path(output_dir) / "prompts" / f"mutation_plan_round_{round_id}.txt"
    response_path = Path(output_dir) / "responses" / f"mutation_plan_round_{round_id}.txt"
    write_text(str(prompt_path), SEED_MUTATION_PLAN_SYSTEM_PROMPT + "\n\n" + prompt)
    try:
        response = llm_client.chat(SEED_MUTATION_PLAN_SYSTEM_PROMPT, prompt)
        write_text(str(response_path), response)
    except Exception as exc:  # noqa: BLE001
        return _save_plan(
            _failed_plan(instance_id, round_id, f"planner request failed: {exc}"),
            output_dir,
        )
    try:
        data = extract_json_object(response)
    except ValueError as first_error:
        retry_prompt = (
            prompt
            + "\n\n上一次 trigger plan 无法解析："
            + str(first_error)
            + "。请重新输出一个完整合法 JSON 对象，不要 Markdown、注释或解释。"
        )
        try:
            response = llm_client.chat(SEED_MUTATION_PLAN_SYSTEM_PROMPT, retry_prompt)
            write_text(
                str(
                    Path(output_dir)
                    / "responses"
                    / f"mutation_plan_round_{round_id}_json_retry.txt"
                ),
                response,
            )
            data = extract_json_object(response)
        except Exception as exc:  # noqa: BLE001
            return _save_plan(
                _failed_plan(
                    instance_id,
                    round_id,
                    f"planner response remained invalid after retry: {exc}",
                ),
                output_dir,
            )
    safe_json_dump(
        data,
        str(Path(output_dir) / f"mutation_round_{round_id}_raw.json"),
    )
    try:
        normalized = _normalize_plan(instance_id, round_id, data)
        validated = validate_mutation_plan(
            normalized,
            behavior,
            host,
            protocol,
            source,
            related_test,
            buggy_repo,
        )
    except Exception as exc:  # noqa: BLE001
        validated = _failed_plan(
            instance_id, round_id, f"plan validation failed safely: {exc}"
        )
    return _save_plan(validated, output_dir)

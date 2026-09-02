"""Stage 1: rewrite raw issues into structured behavior targets."""

from __future__ import annotations

import traceback
from pathlib import Path
import re
from typing import Any

from ..io.io_utils import format_code_context, format_test_context
from ..core.prompts import ISSUE_REWRITE_SYSTEM_PROMPT, ISSUE_REWRITE_USER_PROMPT
from ..core.schema import BehaviorTarget, InstanceContext
from ..core.utils import ensure_dir, extract_json_object, now_timestamp, safe_json_dump, write_text


REQUIRED_FIELDS = [
    "issue_summary",
    "trigger_condition",
    "error_symptom",
    "expected_behavior",
    "target_apis",
    "suspected_bug_locations",
    "related_test_seeds",
    "mutation_hints",
    "observation_points",
    "assertion_hints",
    "setup_hints",
    "uncertainties",
    "safety_constraints",
    "audit_warnings",
]


def _positive_issue_literals(issue_text: str) -> set[str]:
    markers = (
        " valid", "accepted", "accepts", "translates", "parses", "works",
        "succeeds", "合法", "正常", "接受", "解析为", "转换为",
    )
    values: set[str] = set()
    for segment in re.split(r"[\n.!?。！？]+", issue_text):
        low = segment.lower()
        if not any(marker in low for marker in markers):
            continue
        values.update(re.findall(r"['\"]([^'\"]{1,120})['\"]", segment))
        values.update(re.findall(r"\b\d{1,3}:\d{2}(?::\d{2})?\b", segment))
    return {value.strip() for value in values if value.strip()}


def apply_behavior_safety_constraints(
    issue_text: str, behavior: BehaviorTarget
) -> BehaviorTarget:
    """Add derived polarity guards while retaining the raw LLM target intact."""
    positive_literals = _positive_issue_literals(issue_text)
    if not positive_literals:
        return behavior
    mutation_text = json_dumps_for_text(behavior.mutation_hints)
    trigger_text = str(behavior.trigger_condition.get("text") or "")
    implicated = sorted(
        literal
        for literal in positive_literals
        if literal in mutation_text or literal in trigger_text
    )
    if not implicated:
        return behavior
    issue_and_expected = (
        issue_text + "\n" + str(behavior.expected_behavior.get("text") or "")
    ).lower()
    message_issue = any(
        marker in issue_and_expected
        for marker in (
            "error message", "validation message", "message format",
            "错误消息", "错误信息", "验证消息",
        )
    )
    constraint = {
        "rule": "positive_example_polarity",
        "severity": "hard",
        "protected_inputs": implicated,
        "instruction": (
            "Inputs explicitly described by the Issue as accepted, parsed, or "
            "successful must not be used as invalid inputs and must not be placed "
            "inside assertRaises/pytest.raises."
        ),
        "evidence": [
            f"Issue describes {literal!r} on a successful/accepted path."
            for literal in implicated
        ],
    }
    if message_issue:
        constraint["validation_message_instruction"] = (
            "For a validation-error-message defect, preserve a genuinely invalid "
            "input from the highest-ranked iCoRe seed and mutate only the expected "
            "fixed-side message fragment."
        )
    if constraint not in behavior.safety_constraints:
        behavior.safety_constraints.append(constraint)
    warning = (
        "Derived polarity audit: do not reinterpret successful Issue example(s) "
        + ", ".join(repr(value) for value in implicated)
        + " as invalid trigger inputs."
    )
    if warning not in behavior.audit_warnings:
        behavior.audit_warnings.append(warning)
    return behavior


def behavior_from_dict(instance_id: str, data: dict[str, Any]) -> BehaviorTarget:
    # New artifacts expose setup/trigger/oracle, but each section contains the
    # original P0 rich objects (including evidence/confidence/reason fields).
    # Flat P0 artifacts remain readable so both experiment arms can consume one
    # shared issue-rewrite cache without invoking the LLM again.
    if any(isinstance(data.get(key), dict) for key in ("setup", "trigger", "oracle")):
        setup = data.get("setup") if isinstance(data.get("setup"), dict) else {}
        trigger = data.get("trigger") if isinstance(data.get("trigger"), dict) else {}
        oracle = data.get("oracle") if isinstance(data.get("oracle"), dict) else {}
        source = {
            "issue_summary": data.get("issue_summary", ""),
            "setup_hints": setup.get("setup_hints", []),
            "related_test_seeds": setup.get("related_test_seeds", []),
            "trigger_condition": trigger.get("trigger_condition", {}),
            "error_symptom": trigger.get("error_symptom", {}),
            "target_apis": trigger.get("target_apis", []),
            "suspected_bug_locations": trigger.get("suspected_bug_locations", []),
            "mutation_hints": trigger.get("mutation_hints", []),
            "safety_constraints": trigger.get("safety_constraints", []),
            "expected_behavior": oracle.get("expected_behavior", {}),
            "observation_points": oracle.get("observation_points", []),
            "assertion_hints": oracle.get("assertion_hints", []),
            "uncertainties": data.get("uncertainties", []),
            "audit_warnings": trigger.get(
                "audit_warnings", data.get("audit_warnings", [])
            ),
        }
    else:
        source = data
    normalized = {k: source.get(k) for k in REQUIRED_FIELDS}
    normalized.setdefault("issue_summary", "")
    for key in ["target_apis", "suspected_bug_locations", "related_test_seeds", "mutation_hints", "observation_points", "assertion_hints", "setup_hints", "uncertainties", "safety_constraints", "audit_warnings"]:
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    for key in ["trigger_condition", "error_symptom", "expected_behavior"]:
        if not isinstance(normalized.get(key), dict):
            normalized[key] = {}
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else data
    schema_version = str(data.get("schema_version") or "behavior_target.lossless.v1")
    return BehaviorTarget(
        instance_id=instance_id,
        raw=raw,
        schema_version=schema_version,
        **normalized,
    )


def save_enhanced_issue_copy(behavior: BehaviorTarget, output_dir: str) -> None:
    enhanced = behavior.to_dict()
    safe_json_dump(enhanced, str(Path(output_dir) / "enhanced_issue.json"))
    lines = [
        f"instance_id: {behavior.instance_id}",
        "",
        f"issue_summary: {behavior.issue_summary}",
        "",
        "setup:",
        json_dumps_for_text(behavior.setup_view()),
        "",
        "trigger:",
        json_dumps_for_text(behavior.trigger_view()),
        "",
        "oracle:",
        json_dumps_for_text(behavior.oracle_view()),
        "",
        "uncertainties:",
        json_dumps_for_text(behavior.uncertainties),
        "",
    ]
    write_text(str(Path(output_dir) / "enhanced_issue.txt"), "\n".join(lines))


def json_dumps_for_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)


def rewrite_issue(
    context: InstanceContext,
    llm_client: Any,
    output_dir: str,
    code_max_chars: int = 18000,
    test_max_chars: int = 18000,
) -> BehaviorTarget:
    ensure_dir(output_dir)
    code_context = format_code_context(context.retrieved_code, code_max_chars)
    test_context = format_test_context(context.retrieved_tests, test_max_chars)
    user_prompt = ISSUE_REWRITE_USER_PROMPT.format(
        issue_text=context.issue_text,
        code_context=code_context,
        test_context=test_context,
    )
    prompt_path = str(Path(output_dir) / "prompt.txt")
    response_path = str(Path(output_dir) / "response.txt")
    write_text(prompt_path, ISSUE_REWRITE_SYSTEM_PROMPT + "\n\n" + user_prompt)
    meta = {"instance_id": context.instance_id, "started_at": now_timestamp(), "status": "RUNNING"}
    try:
        response = llm_client.chat(ISSUE_REWRITE_SYSTEM_PROMPT, user_prompt)
        write_text(response_path, response)
        try:
            data = extract_json_object(response)
        except ValueError as first_error:
            retry_prompt = (
                user_prompt
                + "\n\n上一次响应无法解析为完整 JSON："
                + str(first_error)
                + "。请重新输出单个完整合法 JSON 对象；不要省略字段，不要截断，"
                + "不要输出 Markdown 或解释。"
            )
            response = llm_client.chat(ISSUE_REWRITE_SYSTEM_PROMPT, retry_prompt)
            write_text(str(Path(output_dir) / "response_json_retry.txt"), response)
            data = extract_json_object(response)
        behavior = apply_behavior_safety_constraints(
            context.issue_text, behavior_from_dict(context.instance_id, data)
        )
        behavior.save_json(str(Path(output_dir) / "behavior_target.json"))
        save_enhanced_issue_copy(behavior, output_dir)
        meta.update({"status": "OK", "finished_at": now_timestamp()})
        safe_json_dump(meta, str(Path(output_dir) / "meta.json"))
        return behavior
    except Exception as exc:  # noqa: BLE001
        meta.update({"status": "ERROR", "finished_at": now_timestamp(), "error": str(exc), "traceback": traceback.format_exc()})
        safe_json_dump(meta, str(Path(output_dir) / "meta.json"))
        raise

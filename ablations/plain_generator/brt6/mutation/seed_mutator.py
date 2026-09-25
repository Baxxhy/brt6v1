"""Propose one residual C/I/O difference for the next execution round."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.behavior_evidence import (
    BehaviorEvidence,
    behavior_prompt_view,
    render_evidence_prompt,
)
from ..core.prompts import SEED_MUTATION_PLAN_SYSTEM_PROMPT, SEED_MUTATION_PLAN_USER_PROMPT
from ..core.schema import HostContext, ProtocolRecovery, RetrievedCode, RetrievedTest, SemanticDelta
from ..core.utils import extract_json_object, safe_json_dump, truncate_text, write_text
from ..io.io_utils import format_code_context
from ..validation.delta_guard import normalize_delta
from ..llm.errors import LLMUnavailableError


MAX_PROMPT_BEHAVIOR_CHARS = 30_000
MAX_PROMPT_ISSUE_CHARS = 30_000
MAX_PROMPT_HOST_CHARS = 30_000
MAX_PROMPT_PROTOCOL_CHARS = 20_000
MAX_PROMPT_SOURCE_CHARS = 60_000
MAX_PROMPT_SEED_CHARS = 40_000
MAX_PROMPT_EXECUTION_CHARS = 25_000
MAX_PROMPT_VERIFIER_CHARS = 16_000
MAX_PROMPT_HISTORY_CHARS = 16_000


def _text(value: Any, limit: int) -> str:
    return truncate_text(str(value or ""), limit)


def _json(value: Any, limit: int) -> str:
    return _text(json.dumps(value, ensure_ascii=False), limit)


def _invalid_delta(instance_id: str, round_id: int, error: str) -> SemanticDelta:
    return SemanticDelta(
        instance_id=instance_id,
        round_id=round_id,
        status="INVALID",
        reason="planner protocol failure",
        errors=[error],
    )


def _save_delta(delta: SemanticDelta, output_dir: str) -> SemanticDelta:
    safe_json_dump(delta.to_dict(), str(Path(output_dir) / f"delta_round_{delta.round_id}.json"))
    return delta


def propose_semantic_delta(
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
    current_candidate_code: str = "",
    delta_history: list[dict[str, Any]] | None = None,
    issue_text: str = "",
) -> SemanticDelta:
    """Ask for exactly one frontier Delta and retry only malformed JSON once."""

    source = related_source or []
    current_test = current_candidate_code or (
        related_test.code_content if related_test else host.seed_test_code
    )
    prompt_behavior = behavior_prompt_view(behavior)
    if prompt_behavior.get("schema_version") == "raw_issue_context.v1":
        prompt_behavior = dict(prompt_behavior)
        prompt_behavior.pop("issue_text", None)
        prompt_behavior["structured_target"] = "unavailable"
    prompt = SEED_MUTATION_PLAN_USER_PROMPT.format(
        issue_text=_text(issue_text, MAX_PROMPT_ISSUE_CHARS),
        behavior_json=_json(prompt_behavior, MAX_PROMPT_BEHAVIOR_CHARS),
        host_context_json=_json(host.to_dict(), MAX_PROMPT_HOST_CHARS),
        protocol_json=_json(protocol.to_dict() if protocol else {}, MAX_PROMPT_PROTOCOL_CHARS),
        source_context=_text(format_code_context(source), MAX_PROMPT_SOURCE_CHARS),
        seed_test_code=_text(current_test, MAX_PROMPT_SEED_CHARS),
        execution_feedback=_text(execution_feedback or "None; this is the first round for this seed.", MAX_PROMPT_EXECUTION_CHARS),
        verifier_feedback=_json(verifier_feedback or {}, MAX_PROMPT_VERIFIER_CHARS),
        delta_history=_json(delta_history or [], MAX_PROMPT_HISTORY_CHARS),
    )
    prompt = render_evidence_prompt(prompt, behavior)
    prompt_path = Path(output_dir) / "prompts" / f"delta_round_{round_id}.txt"
    response_path = Path(output_dir) / "responses" / f"delta_round_{round_id}.txt"
    write_text(str(prompt_path), SEED_MUTATION_PLAN_SYSTEM_PROMPT + "\n\n" + prompt)
    try:
        response = llm_client.chat(SEED_MUTATION_PLAN_SYSTEM_PROMPT, prompt)
        write_text(str(response_path), response)
        try:
            data = extract_json_object(response)
        except ValueError as exc:
            retry_prompt = (
                prompt
                + "\n\nPlease repair only the JSON structure without changing the semantic decision. Parse error: "
                + str(exc)
                + ". Return one complete JSON object only."
            )
            response = llm_client.chat(SEED_MUTATION_PLAN_SYSTEM_PROMPT, retry_prompt)
            write_text(
                str(Path(output_dir) / "responses" / f"delta_round_{round_id}_json_retry.txt"),
                response,
            )
            data = extract_json_object(response)
    except LLMUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        return _save_delta(
            _invalid_delta(instance_id, round_id, f"planner response invalid: {exc}"),
            output_dir,
        )
    safe_json_dump(data, str(Path(output_dir) / f"delta_round_{round_id}_raw.json"))
    return _save_delta(normalize_delta(instance_id, round_id, data), output_dir)

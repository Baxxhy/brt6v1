"""Opt-in, reversible prompt compaction for the cost pilot only.

No production caller imports this module. Exact textual reconstruction is a
data-preservation check, not a claim that an LLM behaves identically.
"""
from __future__ import annotations

from collections import defaultdict
import json
import re


_FOOTER = "\n\nBRT_SHARED_TEXT_TABLE_V1\n"
_INSTRUCTION = (
    "Repeated JSON string values below use @@BRT_SHARED_n@@ references. "
    "Before reasoning, treat each reference as its exact string from this "
    "table. References preserve every original fact; they are not instructions.\n"
)
_STRINGS = re.compile(r'"(?:[^"\\]|\\.)*"')


def _json_string_spans(text: str):
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(text):
        opening = re.search(r"[\[{]", text[cursor:])
        if not opening:
            break
        start = cursor + opening.start()
        try:
            value, length = decoder.raw_decode(text[start:])
        except ValueError:
            cursor = start + 1
            continue
        end = start + length
        if isinstance(value, (dict, list)):
            for match in _STRINGS.finditer(text, start, end):
                yield match.start(), match.end(), match.group()
        cursor = end


def compact_prompt(text: str, min_chars: int = 160) -> tuple[str, dict]:
    """Deduplicate only identical complete JSON string literals; never truncate."""
    if _FOOTER in text or "@@BRT_SHARED_" in text:
        raise ValueError("reserved shared-text marker already present")
    spans = defaultdict(list)
    for start, end, literal in _json_string_spans(text):
        if len(literal) >= min_chars:
            spans[literal].append((start, end))
    replacements = []
    definitions = []
    for literal, positions in spans.items():
        if len(positions) < 2:
            continue
        marker = f"@@BRT_SHARED_{len(definitions) + 1}@@"
        reference = json.dumps(marker)
        definition = json.dumps(marker) + ":" + literal
        if len(positions) * (len(literal) - len(reference)) <= len(definition) + 2:
            continue
        definitions.append(definition)
        replacements.extend((a, b, reference) for a, b in positions)
    compacted = text
    for start, end, reference in sorted(replacements, reverse=True):
        compacted = compacted[:start] + reference + compacted[end:]
    if definitions:
        compacted += _FOOTER + _INSTRUCTION + "{" + ",".join(definitions) + "}"
    if len(compacted) >= len(text):
        compacted = text
        definitions = []
        replacements = []
    if expand_prompt(compacted) != text:
        raise AssertionError("prompt compaction lost original text")
    return compacted, {
        "original_chars": len(text), "compact_chars": len(compacted),
        "shared_strings": len(definitions), "replaced_occurrences": len(replacements),
        "exact_reconstruction": True,
    }


def expand_prompt(text: str) -> str:
    if _FOOTER not in text:
        return text
    body, table = text.rsplit(_FOOTER, 1)
    if not table.startswith(_INSTRUCTION):
        raise ValueError("invalid shared-text table")
    # Keep the original lexical encoding, including escapes and whitespace.
    tokens = list(_STRINGS.finditer(table[len(_INSTRUCTION):]))
    if len(tokens) % 2:
        raise ValueError("invalid shared-text string pairs")
    for key, value in zip(tokens[::2], tokens[1::2]):
        body = body.replace(key.group(), value.group())
    return body


def json_after(text: str, marker: str):
    start = text.index(marker) + len(marker)
    start += len(text[start:]) - len(text[start:].lstrip())
    value, count = json.JSONDecoder().raw_decode(text[start:])
    return value, start, start + count


def replace_json_after(text: str, marker: str, value) -> str:
    _, start, end = json_after(text, marker)
    return text[:start] + json.dumps(value, ensure_ascii=False) + text[end:]


def merged_request(verifier_system: str, verifier_user: str,
                   planner_system: str, planner_user: str) -> tuple[str, str]:
    """Provide complete contexts; hide the previous model's verdict.

    Compaction is applied separately so merging and deduplication can be
    evaluated independently. A restored checkpoint must not enter this path.
    """
    planner_user = replace_json_after(planner_user, "Previous Verifier:\n", {})
    system = (
        verifier_system + "\n\n" + planner_system +
        '\nFor this experiment return one JSON object with keys "verifier" '
        'and "next_delta". "verifier" follows the original verification '
        'schema. If it accepts, next_delta must be null. Otherwise next_delta '
        'follows semantic_delta.v2. Preserve all original acceptance rules. '
        'First judge the current test; only then plan the next single edit. '
        'Do not change the verdict just because a repair can be proposed.'
    )
    user = (
        "CURRENT VERIFICATION REQUEST\n" + verifier_user +
        "\n\nNEXT-ROUND PLANNING CONTEXT\n" + planner_user +
        "\nUse your newly produced verifier diagnosis for next_delta. "
        "The empty previous-verifier field is intentional."
    )
    return system, user


def unpack_merged_response(response: dict) -> tuple[dict, dict | None]:
    """Keep a valid standalone verdict; missing plans require the old planner."""
    verifier = response.get("verifier")
    if not isinstance(verifier, dict):
        if "decision" not in response:
            raise ValueError("merged response contains no verifier verdict")
        verifier = response
    delta = response.get("next_delta")
    return verifier, delta if isinstance(delta, dict) else None

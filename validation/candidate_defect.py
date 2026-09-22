"""Preserve explicit negative semantic evidence; not an oracle correctness proof."""
from __future__ import annotations

from typing import Any


DEFECT_PROMPT = """
Also return candidate_defect (null if none). Distinguish a product failing its
requirement from a defect in the test itself. A missing product behavior is NOT
a candidate defect. Uncertainty or an unsupported expectation alone is not a
demonstrated defect. If supplied evidence identifies an actual test construction,
trigger, or oracle error, report:
{"kind":"setup|trigger|oracle", "subject":"test_artifact",
 "code_quote":"exact candidate text", "evidence_source":"issue|repository_context|execution_log",
 "evidence_quote":"exact supplied text", "explanation":"why this identifies a test defect rather than the target bug",
 "repair":"one concrete correction supported by that evidence"}.
Keep normal semantic facts unchanged. Do not propose a replacement expected
value from the buggy output, or infer a defect just because the test fails.
"""


def validated_candidate_defect(value: Any, code: str, sources: dict[str, str]) -> dict:
    """Validate references only; the semantic claim remains a model judgment."""
    if not isinstance(value, dict) or value.get("subject") != "test_artifact":
        return {}
    if value.get("kind") not in {"setup", "trigger", "oracle"}:
        return {}
    fields = ("kind", "subject", "code_quote", "evidence_source", "evidence_quote", "explanation", "repair")
    result = {k: value.get(k) for k in fields}
    if any(not isinstance(v, str) or not v.strip() for v in result.values()):
        return {}
    source = sources.get(result["evidence_source"], "")
    if result["code_quote"] not in code or result["evidence_quote"] not in source:
        return {}
    return result

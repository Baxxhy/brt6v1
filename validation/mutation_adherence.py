"""AST-aware checks that a generated candidate stayed within its trigger plan."""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import io
import re
import tokenize
from typing import Any

from ..core.schema import CandidateTest, MutationPlan, MutationStep, ProtocolRecovery


_ORACLE_CALLS = {
    "raises",
    "raisesregex",
    "warns",
    "warnsregex",
    "assertwarns",
    "assertwarnsregex",
    "assertlogs",
    "assertnlogs",
    "fail",
    "fnmatch_lines",
    "match_lines",
    "re_match_lines",
    "snapshot",
    "match",
}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _is_oracle_call(node: ast.Call) -> bool:
    leaf = _name(node.func).rsplit(".", 1)[-1].lower()
    return leaf.startswith("assert") or leaf in _ORACLE_CALLS


def _contains_explicit_failure(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            raised = _name(child.exc.func) if isinstance(child.exc, ast.Call) else _name(child.exc)
            if raised in {"AssertionError", "pytest.fail"}:
                return True
        if isinstance(child, ast.Call) and _name(child.func).lower() in {
            "pytest.fail",
            "unittest.fail",
            "self.fail",
        }:
            return True
    return False


def oracle_kinds(code: str) -> list[str]:
    """Classify explicit public observations without requiring a bare assert."""

    kinds: set[str] = set()
    marker = re.search(r"BRT_ORACLE_TYPE:\s*([A-Z_]+)", code)
    if marker:
        kinds.add(marker.group(1))
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return sorted(kinds)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            kinds.add("ASSERT_EXPRESSION")
        elif isinstance(node, ast.Call):
            name = _name(node.func).lower()
            leaf = name.rsplit(".", 1)[-1]
            if leaf in {"raises", "raisesregex"} or leaf.startswith("assertraises"):
                kinds.add("EXCEPTION")
            elif leaf in {"warns", "warnsregex"} or leaf.startswith("assertwarn"):
                kinds.add("WARNING")
            elif leaf in {"assertlogs", "assertnlogs"}:
                kinds.add("LOGGING")
            elif _is_oracle_call(node):
                kinds.add("FRAMEWORK_ASSERTION")
        elif isinstance(node, (ast.If, ast.Try)) and _contains_explicit_failure(node):
            kinds.add("EXPLICIT_FAILURE_GUARD")
    return sorted(kinds)


def oracle_contract_fragments(code: str) -> list[str]:
    """Return normalized AST fragments that define an Oracle contract."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ["syntax-error"]
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            values.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Call):
            if _is_oracle_call(node):
                values.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, (ast.If, ast.Try)) and _contains_explicit_failure(node):
            values.append(ast.dump(node, include_attributes=False))
    values.extend(f"oracle-kind:{kind}" for kind in oracle_kinds(code))
    return sorted(values)


def oracle_contract_is_strict_subset(before: str, after: str) -> bool:
    """Return true only when ``after`` removes Oracle clauses and retains one.

    Every retained assertion or framework matcher must be AST-identical to a
    clause in ``before``, including expected values and exception types.
    """

    before_fragments = Counter(oracle_contract_fragments(before))
    after_fragments = Counter(oracle_contract_fragments(after))
    semantic_fragments = [
        item for item in after_fragments if not item.startswith("oracle-kind:")
    ]
    return bool(
        semantic_fragments
        and after_fragments != before_fragments
        and all(
            count <= before_fragments.get(fragment, 0)
            for fragment, count in after_fragments.items()
        )
    )


def oracle_fingerprint(code: str) -> str:
    """Fingerprint assertions and non-assert Oracle protocols.

    Exception contexts, warning/logging contexts, unittest/pytest helper
    assertions, snapshot matchers, and explicit failure guards all carry test
    semantics even when no ``ast.Assert`` node exists.
    """

    return hashlib.sha256(
        "\n".join(oracle_contract_fragments(code)).encode()
    ).hexdigest()


def assertion_fingerprint(code: str) -> str:
    """Compatibility alias for older result readers."""

    return oracle_fingerprint(code)


def _fragment_patterns(fragment: str) -> list[str]:
    try:
        expression = ast.parse(fragment, mode="eval")
    except SyntaxError:
        try:
            module = ast.parse(fragment)
        except SyntaxError:
            return []
        return [ast.dump(node, include_attributes=False) for node in module.body]
    return [ast.dump(expression.body, include_attributes=False)]


def _token_stream(text: str) -> str:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return "".join(
            token.string
            for token in tokens
            if token.type
            not in {
                tokenize.COMMENT,
                tokenize.ENCODING,
                tokenize.ENDMARKER,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.NEWLINE,
                tokenize.NL,
            }
        )
    except (IndentationError, tokenize.TokenError):
        return _normalized(text)


def _fragment_present(fragment: str, candidate_code: str) -> bool:
    patterns = _fragment_patterns(fragment)
    try:
        tree = ast.parse(candidate_code)
    except SyntaxError:
        tree = None
    if patterns and tree is not None:
        candidate_patterns = {
            ast.dump(node, include_attributes=False) for node in ast.walk(tree)
        }
        if all(pattern in candidate_patterns for pattern in patterns):
            return True
    return bool(fragment.strip()) and _token_stream(fragment) in _token_stream(candidate_code)


def mutation_plan_from_candidate(candidate: CandidateTest) -> MutationPlan | None:
    """Reconstruct the validated plan carried by a derived candidate."""

    if candidate.mutation_plan_status != "VALID":
        return None
    raw_steps = (candidate.mutation_adherence or {}).get("planned_steps") or []
    steps: list[MutationStep] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        try:
            steps.append(
                MutationStep(
                    op=str(raw.get("op") or ""),
                    target_file=str(raw.get("target_file") or ""),
                    target_symbol=str(raw.get("target_symbol") or ""),
                    seed_anchor=str(raw.get("seed_anchor") or ""),
                    before=str(raw.get("before") or ""),
                    after=str(raw.get("after") or ""),
                    rationale=str(raw.get("rationale") or ""),
                    risk=str(raw.get("risk") or "low"),
                )
            )
        except (TypeError, ValueError):
            continue
    if not steps:
        return None
    return MutationPlan(
        instance_id=candidate.instance_id,
        round_id=candidate.round_id,
        status="VALID",
        steps=steps,
        risk=candidate.mutation_plan_risk or "low",
    )


def assess_mutation_adherence(
    candidate_code: str,
    plan: MutationPlan | None,
    protocol: ProtocolRecovery | None,
    oracle_baseline: str = "",
    assertion_baseline: str = "",
) -> dict[str, Any]:
    if plan is None or not plan.is_usable:
        return {"status": "NOT_APPLICABLE", "violations": []}
    violations: list[str] = []
    applied: list[dict[str, Any]] = []
    normalized_candidate = _normalized(candidate_code)
    for step in plan.steps:
        symbol = re.split(r"[.:]", step.target_symbol)[-1]
        after_matched = _fragment_present(step.after, candidate_code)
        symbol_matched = bool(
            symbol and re.search(rf"\b{re.escape(symbol)}\b", candidate_code)
        )
        matched = after_matched and symbol_matched
        applied.append(
            {
                "op": step.op,
                "matched": matched,
                "after_matched": after_matched,
                "target_symbol_matched": symbol_matched,
            }
        )
        if not matched:
            violations.append(
                f"planned step {step.op} was not observed at its target symbol"
            )
    baseline = oracle_baseline or assertion_baseline
    if baseline and oracle_fingerprint(baseline) != oracle_fingerprint(candidate_code):
        violations.append("non-oracle repair changed the generalized oracle contract")
    missing_protocol: list[str] = []
    if protocol is not None:
        # Record missing exact protocol fragments as audit evidence. Imports can
        # be equivalently regrouped, so these are not hard violations here.
        for item in protocol.decorators + protocol.pytest_marks:
            if item and _normalized(item) not in normalized_candidate:
                missing_protocol.append(item)
    matched_count = sum(bool(item["matched"]) for item in applied)
    if violations:
        status = "VIOLATED"
    elif matched_count == len(applied):
        status = "FULL"
    else:
        status = "PARTIAL"
    return {
        "status": status,
        "planned_steps": [step.to_dict() for step in plan.steps],
        "applied_steps": applied,
        "missing_protocol_fragments": missing_protocol,
        "oracle_kinds": oracle_kinds(candidate_code),
        "oracle_fingerprint": oracle_fingerprint(candidate_code),
        "assertion_fingerprint": oracle_fingerprint(candidate_code),
        "violations": violations,
    }

"""Minimal structural guards for one execution-guided semantic Delta."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.schema import SemanticDelta


ALLOWED_DIMENSIONS = {"CONTEXT", "INTERACTION", "OBSERVATION"}
_ORACLE_LEAVES = {
    "raises", "raisesregex", "warns", "warnsregex", "assertwarns",
    "assertwarnsregex", "assertlogs", "assertnlogs", "fail", "match",
    "snapshot", "fnmatch_lines", "match_lines", "re_match_lines",
}


@dataclass
class GuardResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_delta(instance_id: str, round_id: int, data: dict[str, Any]) -> SemanticDelta:
    """Accept only the paper-facing v2 contract; do no semantic proving."""

    action = str(data.get("action") or "").strip().upper()
    dimension = str(data.get("dimension") or "").strip().upper()
    delta = SemanticDelta(
        instance_id=instance_id,
        round_id=round_id,
        schema_version=str(data.get("schema_version") or "").strip(),
        action=action,
        dimension=dimension,
        seed_fact=str(data.get("seed_fact") or "").strip(),
        target_fact=str(data.get("target_fact") or "").strip(),
        change=str(data.get("change") or "").strip(),
        preserve=_strings(data.get("preserve")),
        avoid=_strings(data.get("avoid")),
        reason=str(data.get("reason") or "").strip(),
    )
    errors: list[str] = []
    if delta.schema_version != "semantic_delta.v2":
        errors.append("schema_version must be semantic_delta.v2")
    if action not in {"MUTATE", "KEEP"}:
        errors.append("action must be MUTATE or KEEP")
    if not delta.seed_fact:
        errors.append("seed_fact is required")
    if not delta.target_fact:
        errors.append("target_fact is required")
    if not delta.reason:
        errors.append("reason is required")
    if action == "MUTATE":
        if dimension not in ALLOWED_DIMENSIONS:
            errors.append("dimension must be CONTEXT, INTERACTION, or OBSERVATION")
        if not delta.change:
            errors.append("MUTATE requires one non-empty change")
    elif action == "KEEP" and (dimension or delta.change):
        errors.append("KEEP requires empty dimension and change")
    delta.errors = errors
    delta.status = "VALID" if not errors else "INVALID"
    return delta


def check_candidate(code: str, candidate_repo_path: str = "") -> GuardResult:
    """Protect executability and test-file scope without judging semantics."""

    errors: list[str] = []
    lowered = code.lower()
    forbidden_evidence = (
        "gold_patch", "gold_test", "fail_to_pass", "pass_to_pass",
    )
    leaked = [token for token in forbidden_evidence if token in lowered]
    if leaked:
        errors.append("candidate contains forbidden evaluation evidence: " + ", ".join(leaked))
    try:
        tree = ast.parse(code)
    except (SyntaxError, IndentationError) as exc:
        location = f"line {getattr(exc, 'lineno', '?')}"
        return GuardResult(False, [f"{type(exc).__name__} at {location}: {exc.msg}"])
    entries = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    if len(entries) != 1:
        errors.append(f"candidate must contain exactly one test entry; found {len(entries)}")
    if candidate_repo_path:
        path = Path(candidate_repo_path)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            errors.append("candidate path must be a repository-relative Python test file")
    return GuardResult(not errors, errors)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _is_oracle_call(node: ast.Call) -> bool:
    leaf = _call_name(node.func).rsplit(".", 1)[-1].lower()
    return leaf.startswith("assert") or leaf in _ORACLE_LEAVES


def _dimension_fragments(code: str) -> dict[str, list[str]] | None:
    """Extract coarse C/I/O evidence; this is telemetry, never a gate."""

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    context: list[str] = []
    interaction: list[str] = []
    observation: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            context.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Assert):
            observation.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Call):
            fragment = ast.dump(node, include_attributes=False)
            if _is_oracle_call(node):
                observation.append(fragment)
            else:
                interaction.append(fragment)
    return {
        "CONTEXT": sorted(context),
        "INTERACTION": sorted(interaction),
        "OBSERVATION": sorted(observation),
    }


def assess_delta_application(
    parent_code: str,
    candidate_code: str,
    delta: SemanticDelta | None,
) -> dict[str, Any]:
    """Record whether the requested Delta is visibly reflected in the AST.

    The result deliberately does not establish semantic correctness and must
    never reject, repair, or re-rank a candidate. It provides auditable
    evidence for the paper claim that a round was asked to change one frontier
    coordinate.
    """

    if delta is None:
        return {"status": "NO_DELTA"}
    requested = delta.dimension if delta.is_actionable else ""
    parent = _dimension_fragments(parent_code)
    candidate = _dimension_fragments(candidate_code)
    if parent is None or candidate is None:
        return {
            "status": "UNKNOWN",
            "requested_dimension": requested,
            "reason": "parent or candidate is not parseable",
        }
    observed = [
        dimension
        for dimension in sorted(ALLOWED_DIMENSIONS)
        if parent[dimension] != candidate[dimension]
    ]
    return {
        "status": "OBSERVED" if requested else "NOT_APPLICABLE",
        "requested_dimension": requested,
        "requested_change": delta.change,
        "requested_dimension_observed": bool(requested and requested in observed),
        "observed_dimensions": observed,
        "extra_dimensions": [item for item in observed if item != requested],
        "parent_fragment_counts": {
            key: len(value) for key, value in parent.items()
        },
        "candidate_fragment_counts": {
            key: len(value) for key, value in candidate.items()
        },
        "limitations": (
            "AST telemetry records coarse syntactic movement only; it does not "
            "prove that the requested semantic change is correct."
        ),
    }

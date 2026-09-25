"""Static Oracle-contract checks used by strict semantic verification."""

from __future__ import annotations

import ast

from ..core.behavior_evidence import BehaviorEvidence, expected_behavior_text
from .oracle_contract import oracle_kinds


_ORACLE_LITERAL_CALLS = {
    "raises",
    "raisesregex",
    "warns",
    "warnsregex",
    "assertwarns",
    "assertwarnsregex",
    "assertlogs",
    "assertnlogs",
    "fnmatch_lines",
    "match_lines",
    "re_match_lines",
    "snapshot",
    "match",
}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def oracle_contract_summary(
    behavior: BehaviorEvidence,
    code: str,
) -> dict[str, object]:
    """Describe whether a candidate has a falsifiable public observation.

    A valid Oracle need not contain a bare ``assert``. Framework exception,
    warning, logging, matcher, and explicit-failure protocols are explicit
    Oracles. A direct call is also sufficient for an Issue whose expected
    behavior is specifically no-exception/no-crash.
    """

    kinds = oracle_kinds(code)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "kinds": kinds,
            "falsifiable": False,
            "no_exception_contract": False,
            "reason": "candidate is not parseable",
        }
    explicit = False
    target_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            explicit = True
        elif isinstance(node, ast.Call):
            leaf = _name(node.func).rsplit(".", 1)[-1].lower()
            if (
                leaf.startswith("assert")
                or leaf in _ORACLE_LITERAL_CALLS
                or leaf == "fail"
            ):
                explicit = True
            elif leaf not in {
                "fixture",
                "mark",
                "parametrize",
                "patch",
                "mock",
            }:
                target_calls += 1
    if "EXPLICIT_FAILURE_GUARD" in kinds:
        explicit = True
    expected = expected_behavior_text(behavior).lower()
    no_exception = any(
        marker in expected
        for marker in (
            "不应抛",
            "不再抛",
            "不应该抛",
            "不报错",
            "正常执行",
            "正常工作",
            "不崩溃",
            "should not raise",
            "without raising",
            "without error",
            "must not raise",
            "should not crash",
            "without crashing",
        )
    ) and target_calls > 0
    falsifiable = explicit or no_exception
    reason = (
        "explicit framework/public observation"
        if explicit
        else "expected behavior is falsified by an unexpected exception"
        if no_exception
        else "no falsifiable Oracle protocol was found"
    )
    if no_exception and "NO_EXCEPTION" not in kinds:
        kinds = sorted(set(kinds) | {"NO_EXCEPTION"})
    return {
        "kinds": kinds,
        "falsifiable": falsifiable,
        "no_exception_contract": no_exception,
        "reason": reason,
    }

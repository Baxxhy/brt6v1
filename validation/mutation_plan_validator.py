"""Evidence validation for trigger-only mutation plans."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from ..core.behavior_evidence import BehaviorEvidence, is_behavior_target
from ..core.schema import (
    HostContext,
    MutationPlan,
    MutationStep,
    ProtocolRecovery,
    RetrievedCode,
    RetrievedTest,
)


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
_ORACLE_MARKERS = (
    "assert",
    "pytest.raises",
    "pytest.warns",
    "assertraises",
    "assertwarns",
    "assertlogs",
    "assertnologs",
    "warnings.catch_warnings",
    "warnings.simplefilter",
    "caplog",
    "snapshot",
    "matcher",
    "expected",
    "oracle",
    "断言",
    "异常断言",
    "警告断言",
    "日志断言",
    "期望值",
    "错误消息应",
)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _relative_file(value: str) -> str:
    value = str(value or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in Path(value).parts:
        return ""
    return value if value.endswith(".py") else ""


def _file_exists(
    relative: str,
    allowed_paths: set[str],
    buggy_repo: str,
) -> bool:
    if relative in allowed_paths:
        return True
    return bool(buggy_repo and (Path(buggy_repo) / relative).is_file())


def _file_text(
    relative: str,
    related_source: list[RetrievedCode],
    related_test: RetrievedTest | None,
    buggy_repo: str,
) -> str:
    chunks = [item.code_content for item in related_source if item.path == relative]
    if related_test is not None and related_test.file == relative:
        chunks.append(related_test.code_content)
    path = Path(buggy_repo) / relative if buggy_repo else None
    if path is not None and path.is_file():
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(chunks)


def _symbol_grounded(symbol: str, text: str) -> bool:
    """Require an executable symbol to exist in source/seed evidence.

    BehaviorTarget is a retrieval hint, not proof that a Python symbol exists.
    Keeping it out of this check prevents a hallucinated target API from
    validating itself merely because the same name appears in the LLM-produced
    behavior representation.
    """

    symbol = str(symbol or "").strip()
    if not symbol:
        return False
    leaf = re.split(r"[.:]", symbol)[-1].strip()
    if symbol in text or (leaf and re.search(rf"\b{re.escape(leaf)}\b", text)):
        return True
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
    return leaf in names


def _ast_fragment(fragment: str) -> list[str]:
    """Return exact AST patterns represented by an expression or statements."""

    try:
        expression = ast.parse(fragment, mode="eval")
    except SyntaxError:
        try:
            module = ast.parse(fragment)
        except SyntaxError:
            return []
        return [ast.dump(node, include_attributes=False) for node in module.body]
    return [ast.dump(expression.body, include_attributes=False)]


def _anchor_grounded(anchor: str, seed_code: str) -> bool:
    """Match a concrete seed fragment, including literals and call structure."""

    anchor = str(anchor or "").strip()
    if not anchor or not seed_code:
        return False
    if _normalized(anchor) in _normalized(seed_code):
        return True
    try:
        seed_tree = ast.parse(seed_code)
    except SyntaxError:
        return False
    patterns = _ast_fragment(anchor)
    if not patterns:
        return False
    seed_patterns = {
        ast.dump(node, include_attributes=False) for node in ast.walk(seed_tree)
    }
    return all(pattern in seed_patterns for pattern in patterns)


def _touches_oracle(step: MutationStep) -> bool:
    text = "\n".join(
        (step.target_symbol, step.seed_anchor, step.before, step.after)
    ).lower()
    return any(marker in text for marker in _ORACLE_MARKERS)


def _safety_conflict(step: MutationStep, behavior: BehaviorEvidence) -> bool:
    if not is_behavior_target(behavior):
        return False
    safety = json.dumps(behavior.safety_constraints, ensure_ascii=False).lower()
    if not safety:
        return False
    after = str(step.after or "").strip().lower()
    if not after or len(after) < 3 or after not in safety:
        return False
    return any(
        marker in safety
        for marker in ("不得", "不能", "禁止", "do not", "must not", "合法", "valid input")
    )


def _max_risk(steps: Iterable[MutationStep]) -> str:
    return max((step.risk for step in steps), key=lambda item: _RISK_ORDER[item], default="medium")


def validate_mutation_plan(
    plan: MutationPlan,
    behavior: BehaviorEvidence,
    host: HostContext,
    protocol: ProtocolRecovery | None,
    related_source: list[RetrievedCode] | None,
    related_test: RetrievedTest | None,
    buggy_repo: str = "",
) -> MutationPlan:
    """Return an audited plan; only ``VALID`` plans may reach generation."""

    del protocol  # The full protocol is supplied to generation and adherence checks.
    source = related_source or []
    if not plan.steps:
        errors = list(plan.validation_errors)
        errors.append("planner returned no safe trigger steps")
        return replace(
            plan,
            status="ABSTAIN" if plan.status != "INVALID" else "INVALID",
            validation_errors=list(dict.fromkeys(errors)),
            validation_evidence={"validated_steps": 0},
        )
    structural_errors = list(plan.validation_errors)
    allowed_paths = {item.path for item in source if item.path}
    if related_test is not None and related_test.file:
        allowed_paths.add(related_test.file)
    if host.host_file:
        allowed_paths.add(host.host_file)
    evidence: list[dict[str, object]] = []
    accepted_steps: list[MutationStep] = []
    rejected_steps: list[dict[str, object]] = []
    for index, step in enumerate(plan.steps):
        prefix = f"step[{index}]"
        step_errors: list[str] = []
        target_file = _relative_file(step.target_file)
        file_ok = bool(target_file) and _file_exists(
            target_file, allowed_paths, buggy_repo
        )
        if not file_ok:
            step_errors.append(
                f"{prefix}: target_file is not an evidenced Python file"
            )
        target_text = _file_text(target_file, source, related_test, buggy_repo)
        # The plan may target a production symbol or a helper already present in
        # the selected seed.  Both are executable evidence; BehaviorTarget text
        # deliberately remains excluded because it is an LLM-produced hint.
        symbol_evidence = "\n".join((target_text, host.seed_test_code))
        symbol_ok = file_ok and _symbol_grounded(
            step.target_symbol, symbol_evidence
        )
        if not symbol_ok:
            step_errors.append(
                f"{prefix}: target_symbol is not grounded in source evidence"
            )
        anchor_ok = _anchor_grounded(step.seed_anchor, host.seed_test_code)
        if not anchor_ok:
            step_errors.append(
                f"{prefix}: seed_anchor is not grounded in the selected seed"
            )
        before_ok = _anchor_grounded(step.before, host.seed_test_code)
        if not before_ok:
            step_errors.append(
                f"{prefix}: before is not grounded in the selected seed"
            )
        after_ok = bool(str(step.after or "").strip())
        if not after_ok:
            step_errors.append(f"{prefix}: after must describe a concrete edit")
        oracle_touched = _touches_oracle(step)
        if oracle_touched:
            step_errors.append(f"{prefix}: trigger plan attempts to edit the oracle")
        safety_conflict = _safety_conflict(step, behavior)
        if safety_conflict:
            step_errors.append(
                f"{prefix}: proposed trigger conflicts with safety constraints"
            )
        if step.risk == "high":
            step_errors.append(f"{prefix}: high-risk trigger plans are not executable")
        evidence.append(
            {
                "op": step.op,
                "target_file": target_file,
                "file_grounded": file_ok,
                "symbol_grounded": symbol_ok,
                "seed_anchor_grounded": anchor_ok,
                "before_grounded": before_ok,
                "after_concrete": after_ok,
                "oracle_touched": oracle_touched,
                "safety_conflict": safety_conflict,
                "accepted": not step_errors,
            }
        )
        if step_errors:
            rejected_steps.append(
                {
                    "index": index,
                    "step": step.to_dict(),
                    "errors": step_errors,
                }
            )
        else:
            accepted_steps.append(step)
    rejected_errors = [
        error
        for rejected in rejected_steps
        for error in rejected["errors"]
    ]
    valid = bool(accepted_steps) and not structural_errors
    risk = _max_risk(accepted_steps)
    return replace(
        plan,
        status="VALID" if valid else "INVALID",
        steps=accepted_steps if valid else plan.steps,
        risk=risk,
        validation_errors=(
            []
            if valid
            else list(dict.fromkeys(structural_errors + rejected_errors))
        ),
        validation_evidence={
            "proposed_steps": len(plan.steps),
            "validated_steps": len(accepted_steps),
            "rejected_steps": rejected_steps,
            "allowed_paths": sorted(allowed_paths),
            "steps": evidence,
        },
    )

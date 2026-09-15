"""Shared accessors for full and BehaviorTarget-ablation evidence.

The full branch returns the existing BehaviorTarget fields unchanged.  The
ablation branch keeps the raw Issue unstructured and never imports or calls
the IssueRewrite implementation.
"""

from __future__ import annotations

from typing import TypeAlias

from .schema import BehaviorTarget, RawIssueContext


BehaviorEvidence: TypeAlias = BehaviorTarget | RawIssueContext


def is_behavior_target(evidence: BehaviorEvidence) -> bool:
    return isinstance(evidence, BehaviorTarget)


def issue_evidence_text(evidence: BehaviorEvidence) -> str:
    if isinstance(evidence, RawIssueContext):
        return evidence.issue_text
    return "\n".join(
        value
        for value in (
            evidence.issue_summary,
            str(evidence.error_symptom.get("text") or ""),
            str(evidence.expected_behavior.get("text") or ""),
        )
        if value
    )


def expected_behavior_text(evidence: BehaviorEvidence) -> str:
    if isinstance(evidence, RawIssueContext):
        return evidence.issue_text
    return str(evidence.expected_behavior.get("text") or "")


def error_symptom_text(evidence: BehaviorEvidence) -> str:
    if isinstance(evidence, RawIssueContext):
        return evidence.issue_text
    return str(evidence.error_symptom.get("text") or "")


def target_apis(evidence: BehaviorEvidence) -> list[dict]:
    if isinstance(evidence, RawIssueContext):
        return []
    return evidence.target_apis


def suspected_bug_locations(evidence: BehaviorEvidence) -> list[dict]:
    if isinstance(evidence, RawIssueContext):
        return []
    return evidence.suspected_bug_locations


def behavior_target_payload(evidence: BehaviorEvidence) -> dict:
    return evidence.to_dict() if isinstance(evidence, BehaviorTarget) else {}


def behavior_prompt_view(evidence: BehaviorEvidence) -> dict:
    """Return downstream evidence without the lossless duplicate raw payload."""

    payload = evidence.to_dict()
    if isinstance(evidence, BehaviorTarget):
        payload.pop("raw", None)
    return payload


def raw_issue_payload(evidence: BehaviorEvidence) -> dict:
    return evidence.to_dict() if isinstance(evidence, RawIssueContext) else {}


def method_variant(evidence: BehaviorEvidence) -> str:
    return "full" if isinstance(evidence, BehaviorTarget) else "w/o Behavior Target"


def render_evidence_prompt(prompt: str, evidence: BehaviorEvidence) -> str:
    """Relabel only ablation prompts; full-method prompt text is untouched."""
    if isinstance(evidence, BehaviorTarget):
        return prompt
    rendered = prompt.replace(
        "BehaviorTarget.trigger.safety_constraints are hard constraints",
        "This ablation does not build a BehaviorTarget; constraints must come directly from the issue",
    )
    rendered = rendered.replace("Issue/BehaviorTarget", "Issue")
    rendered = rendered.replace("BehaviorTarget:", "Raw issue (unstructured):")
    rendered = rendered.replace("Behavior target:", "Raw issue (unstructured):")
    rendered = rendered.replace(
        "BehaviorTarget", "Structured behavior target unavailable in this ablation"
    )
    return (
        "Experiment condition: without BehaviorTarget.\n"
        "IssueRewrite was not run, so no Environment/Trigger/Assertion representation exists. "
        "Please use the raw issue and unchanged retrieved evidence below.\n\n"
        + rendered
    )

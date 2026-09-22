"""One evidence-cited alternative target, used only after primary exhaustion.

Citation existence and field scope are checked mechanically. Whether the cited
text supports the interpretation remains a model judgment, not a proof.
"""
from __future__ import annotations

import copy
import re

FIELDS = {
    "SETUP": {"setup_hints"},
    "TRIGGER": {"trigger_condition", "mutation_hints"},
    "INVOCATION": {"target_apis", "observation_points"},
    "ORACLE": {"expected_behavior", "assertion_hints"},
}

SYSTEM = """You recover ONE alternative reproduction target after the primary
target produced no accepted test. Use only the supplied issue, repository
evidence and buggy-side feedback. Failure alone does not prove the target wrong.
Abstain if the problem is only infrastructure, or there is no evidence-backed
alternative interpretation. Preserve the issue's explicit expected behavior.
Do not generate test code, infer a resolving patch or use fixed-side outcomes.
Return JSON only with: abstain (boolean), reason, changed_slots (list of SETUP,
TRIGGER, INVOCATION, ORACLE), evidence_bindings (list of {slot, source_ref, quote}),
and overrides (object). Source references must be exact keys from sources and
quotes must be exact substrings. Each changed slot requires a citation. ORACLE
changes require an issue citation and must clarify, never reverse, its requirement.
Allowed override fields: SETUP: setup_hints; TRIGGER: trigger_condition,
mutation_hints; INVOCATION: target_apis, observation_points; ORACLE:
expected_behavior, assertion_hints. Match the canonical field's JSON type. In particular expected_behavior and
trigger_condition are objects: {"text": "...", "evidence": ["..."],
"confidence": "medium"}; do not return plain strings for these fields.
Change only the smallest ambiguous part; leave all other fields untouched.
This is a bounded hypothesis proposal, not a claim of test correctness."""


def normalize_overrides(proposal: dict) -> dict:
    """Normalize flat and slot-grouped override JSON under the same guard."""
    slots = proposal.get("changed_slots")
    overrides = proposal.get("overrides")
    if not isinstance(slots, list) or not slots or not set(slots) <= set(FIELDS):
        raise ValueError("invalid changed_slots")
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError("missing overrides")

    flat: dict = {}
    for key, value in overrides.items():
        if key in FIELDS:
            if key not in slots or not isinstance(value, dict) or not value:
                raise ValueError("override outside declared slot")
            for field, field_value in value.items():
                if field not in FIELDS[key] or field in flat:
                    raise ValueError("override outside declared slot")
                flat[field] = field_value
        else:
            owners = [slot for slot in slots if key in FIELDS[slot]]
            if len(owners) != 1 or key in flat:
                raise ValueError("override outside declared slot")
            flat[key] = value
    return flat


def _evidence_text(value: str) -> str:
    """Canonicalize presentation-only Markdown when checking a quotation."""
    return " ".join(re.sub(r"`+", "", value).split())


def apply_alternative(canonical, proposal: dict, sources: dict[str, str]):
    if proposal.get("abstain") is True:
        return None
    if proposal.get("abstain") is not False:
        raise ValueError("abstain must be an explicit boolean")
    slots = proposal.get("changed_slots")
    overrides = normalize_overrides(proposal)
    allowed = set().union(*(FIELDS[s] for s in slots))
    if not set(overrides) <= allowed:
        raise ValueError("override outside declared slot")
    bindings = proposal.get("evidence_bindings", [])
    covered = set()
    for binding in bindings:
        slot, ref, quote = (binding.get(k) for k in ("slot", "source_ref", "quote"))
        if slot not in slots or ref not in sources or not isinstance(quote, str) or not quote.strip():
            raise ValueError("invalid evidence binding")
        if _evidence_text(quote) not in _evidence_text(sources[ref]):
            raise ValueError("evidence quote not found")
        if slot != "ORACLE" or ref == "issue":
            covered.add(slot)
    if set(slots) != covered:
        raise ValueError("each slot needs evidence; oracle needs issue evidence")
    result = copy.deepcopy(canonical)
    changed = False
    for key, value in overrides.items():
        original = getattr(canonical, key)
        # Models sometimes abbreviate a text-bearing evidence object as its
        # text. Normalize shape only; never invent a new expected behavior.
        if key in {"trigger_condition", "expected_behavior"} and isinstance(original, dict) and isinstance(value, str):
            owner = next(slot for slot in slots if key in FIELDS[slot])
            value = {"text": value, "evidence": [
                f"{b['source_ref']}: {b['quote']}" for b in bindings if b['slot'] == owner
            ], "confidence": "medium"}
        if type(value) is not type(original):
            raise ValueError("override type mismatch: " + key)
        if isinstance(original, dict) and (not value.get("text") or not isinstance(value["text"], str)):
            raise ValueError("behavior override needs text")
        changed |= value != original
        setattr(result, key, value)
    if not changed:
        raise ValueError("alternative does not change target")
    result.raw = {"bounded_alternative": proposal, "primary": canonical.to_dict()}
    return result

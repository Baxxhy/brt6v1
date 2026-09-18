"""One uncertainty-linked revision; quotation checks are not semantic proofs."""
from __future__ import annotations

import copy

SEMANTIC_STATUSES = {
    "UNRELATED_FAIL", "TRIGGER_UNRESOLVED", "ORACLE_UNRESOLVED",
    "PASS", "PASS_UNEXPECTED",
}
EDITABLE = {
    "trigger_condition", "setup_hints", "mutation_hints",
    "target_apis", "observation_points", "assertion_hints",
}

SYSTEM = """Please review a frozen reproduction target using only the supplied
issue, original repository excerpts and buggy-side feedback from three branches.
All three branches being rejected does NOT show that the target is wrong.
Return KEEP_TARGET if failures are caused by environment, collection, syntax,
invalid test construction or unrelated API misuse; do not revise a target merely
to accommodate a buggy output. Never infer the real fix or generate test code.

Consider REVISE_TARGET only if all three branches provide concrete semantic
feedback pointing to the SAME existing uncertainty, and a repository excerpt
supports a different interpretation. Cite an actual excerpt not the model's own
target or reasoning. Explain the link for each branch. Preserve every explicit
issue requirement and the entire expected_behavior. Revise ONE field only:
trigger_condition, setup_hints, mutation_hints, target_apis, observation_points,
or assertion_hints. For a list field, replace exactly one entry without changing
length or order. Keep the original JSON type and keys. Do not edit an entry
marked high confidence or sourced explicitly from issue/issue_report.

Return one JSON object, no Markdown:
{"action":"KEEP_TARGET", "reason":"..."}
or
{"action":"REVISE_TARGET", "reason":"why this interpretation needs revision",
 "uncertainty_index":0, "field":"trigger_condition", "before":{}, "after":{},
 "branch_evidence":[{"seed_id":"seed_0", "quote":"exact feedback substring",
 "explanation":"how this branch points to the uncertainty"},
 {"seed_id":"seed_1", "quote":"...", "explanation":"..."},
 {"seed_id":"seed_2", "quote":"...", "explanation":"..."}],
 "repository_evidence":{"source_ref":"exact supplied source key",
 "quote":"exact nonempty substring", "explanation":"why it supports the revision"}}
Use the original flat field values for before and after. Do not invent evidence.
"""


def eligibility(target, branches):
    if not target.uncertainties:
        return "NO_RECORDED_UNCERTAINTY"
    if set(branches) != {"seed_0", "seed_1", "seed_2"}:
        return "INCOMPLETE_PRIMARY_BRANCHES"
    if any(b["status"] not in SEMANTIC_STATUSES for b in branches.values()):
        return "BRANCH_NOT_SEMANTICALLY_COMPARABLE"
    return None


def apply_revision(target, proposal, sources, branches):
    if proposal.get("action") == "KEEP_TARGET":
        return None
    if proposal.get("action") != "REVISE_TARGET":
        raise ValueError("invalid action")
    blocked = eligibility(target, branches)
    if blocked:
        raise ValueError(blocked)
    index = proposal.get("uncertainty_index")
    if type(index) is not int or not 0 <= index < len(target.uncertainties):
        raise ValueError("revision must reference an existing uncertainty")
    field = proposal.get("field")
    if field not in EDITABLE:
        raise ValueError("fixed target field cannot be changed")
    before, after = proposal.get("before"), proposal.get("after")
    if before != getattr(target, field) or before == after:
        raise ValueError("before must match and revision must change it")
    if type(before) is not type(after):
        raise ValueError("field type changed")
    pairs = [(before, after)]
    if isinstance(before, list):
        if len(before) != len(after):
            raise ValueError("replace one list entry only")
        pairs = [(a, b) for a, b in zip(before, after) if a != b]
        if len(pairs) != 1:
            raise ValueError("replace one list entry only")
    a, b = pairs[0]
    if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
        raise ValueError("entry schema changed")
    if str(a.get("confidence", "")).lower() == "high" or str(a.get("source", "")).lower() in {"issue", "issue_report"}:
        raise ValueError("fixed entry cannot be changed")
    if any(type(b[k]) is not type(v) for k, v in a.items()):
        raise ValueError("entry value type changed")
    cited = set()
    for ev in proposal.get("branch_evidence", []):
        sid, quote = ev.get("seed_id"), ev.get("quote")
        if sid not in branches or sid in cited or not isinstance(quote, str) or not quote.strip() or quote not in branches[sid]["feedback"] or not ev.get("explanation"):
            raise ValueError("invalid branch evidence")
        cited.add(sid)
    if cited != set(branches):
        raise ValueError("three branches must support the same revision")
    ev = proposal.get("repository_evidence", {})
    ref, quote = ev.get("source_ref"), ev.get("quote")
    if ref not in sources or ref == "issue" or not isinstance(quote, str) or not quote.strip() or quote not in sources[ref] or not ev.get("explanation"):
        raise ValueError("missing exact repository evidence")
    result = copy.deepcopy(target)
    setattr(result, field, copy.deepcopy(after))
    # Avoid sending a stale duplicate of the original flat target downstream.
    result.raw = {"target_revision": copy.deepcopy(proposal)}
    return result

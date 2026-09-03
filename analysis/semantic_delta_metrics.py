#!/usr/bin/env python3
"""Mechanically summarize Semantic Delta artifacts without inferred success."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    generation = args.run_dir / "generation"
    summaries = [_load(path) for path in sorted(generation.glob("*/summary.json"))]
    summaries = [item for item in summaries if item.get("instance_id")]
    statuses = Counter(str(item.get("status") or "UNKNOWN") for item in summaries)
    transitions: Counter[str] = Counter()
    risks: Counter[str] = Counter()
    for trace_path in generation.glob("*/residual_trace.json"):
        trace = _load(trace_path)
        for item in trace.get("rounds") or []:
            transitions[str((item.get("transition") or {}).get("relation") or "UNKNOWN")] += 1
            evidence = (item.get("state") or {}).get("evidence") or {}
            risks[str(evidence.get("post_fix_failure_risk") or "unknown")] += 1
    payload = {
        "generated": len(summaries),
        "generation_status": dict(sorted(statuses.items())),
        "search_transitions": dict(sorted(transitions.items())),
        "post_fix_failure_risk": dict(sorted(risks.items())),
        "issue_aligned_generation": statuses.get("ISSUE_ALIGNED_FAIL", 0),
        "buggy_pass": statuses.get("BUGGY_PASS", 0) + statuses.get("PASS", 0),
        "note": "Only official F2P output may be reported as benchmark success.",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Replace selected formal-evaluation rows and recompute denominator-stable F2P."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source_results", required=True)
    parser.add_argument("--source_metrics", required=True)
    parser.add_argument("--override_results", required=True)
    parser.add_argument("--override_metrics", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--completion_path", required=True)
    parser.add_argument("--instance_id", action="append", required=True)
    args = parser.parse_args()

    source_results = load_json(Path(args.source_results))
    source_metrics = load_json(Path(args.source_metrics))
    override_results = load_json(Path(args.override_results))
    override_metrics = load_json(Path(args.override_metrics))
    ids = list(dict.fromkeys(args.instance_id))
    if len(source_results) != 276:
        raise ValueError(f"source denominator is {len(source_results)}, expected 276")
    if set(override_results) != set(ids):
        raise ValueError(
            f"override rows differ from requested IDs: rows={sorted(override_results)}, ids={sorted(ids)}"
        )

    combined = dict(source_results)
    report: dict[str, dict] = {}
    for instance_id in ids:
        old = source_results[instance_id]
        new = override_results[instance_id]
        combined[instance_id] = new
        report[instance_id] = {
            "old_status": old.get("status", "UNKNOWN"),
            "old_success": bool(old.get("success")),
            "new_status": new.get("status", "UNKNOWN"),
            "new_success": bool(new.get("success")),
            "buggy_returncode": (new.get("buggy_run") or {}).get("returncode"),
            "fixed_returncode": (new.get("fixed_run") or {}).get("returncode"),
            "env_error_category": new.get("env_error_category", ""),
            "template_env_name": new.get("template_env_name", ""),
            "runtime_env_name": new.get("env_name", ""),
        }

    statuses = Counter(
        str(result.get("status") or "UNKNOWN") for result in combined.values()
    )
    by_repo: dict[str, Counter] = defaultdict(Counter)
    for result in combined.values():
        by_repo[str(result.get("repo") or "UNKNOWN")][
            str(result.get("status") or "UNKNOWN")
        ] += 1
    success = statuses.get("F2P_SUCCESS", 0)
    environment_errors = {
        instance_id: str(result.get("env_error_category") or "")
        for instance_id, result in override_results.items()
        if result.get("env_error_category")
    }
    source_success = int(source_metrics.get("f2p_success") or 0)
    old_override_success = sum(
        source_results[instance_id].get("status") == "F2P_SUCCESS"
        for instance_id in ids
    )
    new_override_success = sum(
        override_results[instance_id].get("status") == "F2P_SUCCESS"
        for instance_id in ids
    )
    valid = bool(
        override_metrics.get("metrics_valid")
        and override_metrics.get("total_instances") == len(ids)
        and not environment_errors
    )
    metrics = {
        "total_instances": 276,
        "f2p_success": success,
        "f2p_fail": 276 - success,
        "f2p_at_1": success / 276,
        "f2p_at_1_percent": round(success / 276 * 100, 4),
        "metrics_valid": valid,
        "by_status": dict(sorted(statuses.items())),
        "by_repo": {
            repo: dict(sorted(counts.items()))
            for repo, counts in sorted(by_repo.items())
        },
        "by_env_error_category": {},
        "patch_cov_enabled": False,
        "result_type": "selected_instance_f2p_override_on_full276",
        "overridden_instance_count": len(ids),
        "denominator_policy": "all 276 dataset rows; missing generation counts as failure",
        "source_f2p_success": source_success,
        "source_f2p_at_1_percent": source_metrics.get("f2p_at_1_percent"),
        "source_override_success": old_override_success,
        "new_override_success": new_override_success,
        "f2p_success_delta": success - source_success,
        "overridden_instances": ids,
        "canary_environment_errors": environment_errors,
    }
    completion = {
        "finished_at": datetime.now().astimezone().isoformat(),
        "status": "complete" if valid else "invalid",
        "generated_tests": len(ids),
        "canary_metrics": override_metrics,
        "merged_metrics": metrics,
    }
    output_dir = Path(args.output_dir)
    write_json(output_dir / "merged_results.json", combined)
    write_json(output_dir / "metrics.json", metrics)
    write_json(output_dir / "override_report.json", report)
    write_json(Path(args.completion_path), completion)
    print(json.dumps(completion, ensure_ascii=False, indent=2))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())

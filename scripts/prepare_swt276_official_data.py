#!/usr/bin/env python3
"""Download and validate the official rows needed for SWT-276 evaluation.

The generated issue file remains patch-free.  This script writes the public
gold fields to a separate evaluation-only path consumed after predictions are
frozen.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


GOLD_FIELDS = ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS")


def load_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = list(payload.values()) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"unsupported dataset structure: {path}")
    return rows


def validate(issues: list[dict], official: list[dict]) -> None:
    issue_ids = [str(row.get("instance_id") or "") for row in issues]
    official_by_id = {str(row.get("instance_id") or ""): row for row in official}
    if len(issues) != 276 or len(set(issue_ids)) != 276:
        raise ValueError("generation dataset must contain 276 unique instances")
    if len(official) != 276 or set(issue_ids) != set(official_by_id):
        raise ValueError("official dataset does not match the SWT-276 instance set")
    for issue in issues:
        instance_id = issue["instance_id"]
        row = official_by_id[instance_id]
        for field in ("repo", "version", "base_commit"):
            if issue.get(field) != row.get(field):
                raise ValueError(f"{instance_id}: mismatched {field}")
        for field in GOLD_FIELDS:
            if field not in row or row[field] is None:
                raise ValueError(f"{instance_id}: missing official field {field}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--issues", default="data/issues/swt276_issues.json", type=Path
    )
    parser.add_argument(
        "--output", default="data/official/swt276_official_eval.json", type=Path
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    issues = load_rows(args.issues.resolve())
    output = args.output.resolve()
    if args.check:
        if not output.is_file():
            raise SystemExit(f"official dataset is missing: {output}")
        validate(issues, load_rows(output))
        print(f"official SWT-276 dataset ready: {output}")
        return 0

    from datasets import load_dataset

    public_rows = {
        row["instance_id"]: dict(row)
        for row in load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    }
    official: list[dict] = []
    for issue in issues:
        instance_id = issue["instance_id"]
        if instance_id not in public_rows:
            raise ValueError(f"public SWE-bench Lite is missing {instance_id}")
        source = public_rows[instance_id]
        row = dict(issue)
        for field in GOLD_FIELDS:
            row[field] = source[field]
        row["environment_setup_commit"] = (
            issue.get("environment_setup_commit")
            or source.get("environment_setup_commit")
            or issue["base_commit"]
        )
        official.append(row)

    validate(issues, official)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(official, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(official)} official rows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

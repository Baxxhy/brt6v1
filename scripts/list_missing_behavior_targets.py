"""List dataset instances without a complete BehaviorTarget artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from brt6.io.io_utils import load_issue_data


def valid_target(path: Path, instance_id: str) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("instance_id") == instance_id
        and isinstance(payload.get("trigger"), dict)
        and isinstance(payload.get("oracle"), dict)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances-path", required=True)
    parser.add_argument("--target-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--restrict-file")
    args = parser.parse_args()

    issues = load_issue_data(args.instances_path)
    instance_ids = list(issues)
    if args.restrict_file:
        requested = {
            line.strip()
            for line in Path(args.restrict_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        instance_ids = [instance_id for instance_id in instance_ids if instance_id in requested]
    root = Path(args.target_root)
    missing = [
        instance_id
        for instance_id in instance_ids
        if not valid_target(root / instance_id / "behavior_target.json", instance_id)
    ]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(f"{instance_id}\n" for instance_id in missing), encoding="utf-8"
    )
    report = {
        "instances": len(instance_ids),
        "complete": len(instance_ids) - len(missing),
        "missing_or_invalid": len(missing),
        "missing_instance_ids": missing,
        "validation": "parseable JSON with matching instance_id and structured trigger/oracle",
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"behavior_targets={report['complete']}/{report['instances']} "
        f"missing={report['missing_or_invalid']}",
        flush=True,
    )


if __name__ == "__main__":
    main()

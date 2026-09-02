#!/usr/bin/env python3
"""Summarize existing SWT template attempt logs without rerunning builds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .prewarm_swt_environments import attempt_diagnostics, write_json
except ImportError:  # Direct execution: python scripts/diagnose_*.py
    from prewarm_swt_environments import attempt_diagnostics, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify the latest failed SWT environment attempts."
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=PROJECT_ROOT / ".bootstrap/swt-template-environments",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def collect_failures(work_root: Path) -> list[dict[str, object]]:
    failures: list[dict[str, object]] = []
    if not work_root.is_dir():
        return failures
    for env_dir in sorted(path for path in work_root.iterdir() if path.is_dir()):
        attempts = sorted(
            env_dir.glob("attempt_*.json"),
            key=lambda path: int(path.stem.rsplit("_", 1)[-1]),
        )
        if not attempts:
            continue
        history: list[dict[str, object]] = []
        for attempt_path in attempts:
            try:
                attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(attempt, dict):
                history.append(attempt)
        if not history:
            continue
        latest_result = history[-1].get("result")
        latest_result = latest_result if isinstance(latest_result, dict) else {}
        try:
            latest_returncode = int(latest_result.get("returncode", 1))
        except (TypeError, ValueError):
            latest_returncode = 1
        if latest_returncode == 0:
            continue
        diagnostic_bundle = attempt_diagnostics(history)
        failures.append(
            {
                "env_name": env_dir.name,
                "attempt_file": str(attempts[-1].resolve()),
                **diagnostic_bundle,
            }
        )
    return failures


def main() -> int:
    args = parse_args()
    work_root = args.work_root.expanduser().resolve()
    output = (
        args.output.expanduser().resolve()
        if args.output
        else work_root / "failure_diagnostics.json"
    )
    failures = collect_failures(work_root)
    write_json(output, failures)
    print(f"work_root={work_root}")
    print(f"failed_count={len(failures)}")
    for item in failures:
        root_cause = item.get("root_cause") or {}
        print(
            f"{item['env_name']} "
            f"status={root_cause.get('status')} "
            f"rc={root_cause.get('returncode')} "
            f"category={root_cause.get('category')}"
        )
        if root_cause.get("error_line"):
            print(f"  {root_cause['error_line']}")
    print(f"diagnostics={output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Prepare or repair TDD-Bench local-Conda dependency templates.

The formal preflight is intentionally read-only.  This companion command
uses the production environment builder to repair only the template groups
reported as not ready, then leaves the preflight to verify the result again.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import json
import sys
import traceback
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from brt6.scripts.prewarm_swt_environments import (  # noqa: E402
    attempt_diagnostics,
    load_unique_templates,
    prepare_template,
    write_json,
)


def failed_environment_names(preflight: dict[str, Any]) -> set[str]:
    environments = preflight.get("environments")
    if not isinstance(environments, list):
        raise ValueError("preflight report has no environments list")
    return {
        str(record.get("canonical_env") or "")
        for record in environments
        if isinstance(record, dict)
        and not record.get("ready")
        and str(record.get("canonical_env") or "")
    }


def preflight_repair_blockers(preflight: dict[str, Any]) -> list[str]:
    """Return non-template failures that dependency repair cannot resolve."""

    blockers: list[str] = []
    if not preflight.get("framework_is_icore"):
        blockers.append("framework Python is not the icore environment")
    if not preflight.get("conda_available"):
        blockers.append("Conda is unavailable")
    if not preflight.get("lock_writable"):
        blockers.append("environment lock directory is not writable")
    missing_patch = preflight.get("missing_patch") or []
    if missing_patch:
        blockers.append(f"{len(missing_patch)} dataset rows have no golden patch")
    missing_setup = preflight.get("missing_environment_setup_commit") or []
    if missing_setup:
        blockers.append(
            f"{len(missing_setup)} dataset rows have no environment setup commit"
        )
    repositories = preflight.get("repositories") or []
    failed_repositories = [
        str(record.get("repo") or record.get("path") or "unknown")
        for record in repositories
        if isinstance(record, dict) and not record.get("ready")
    ]
    if failed_repositories:
        blockers.append(
            "repository cache is incomplete: " + ", ".join(failed_repositories)
        )
    return blockers


def select_templates_for_repair(
    templates: list[dict[str, str]],
    preflight: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if preflight is None:
        return list(templates)
    failed_names = failed_environment_names(preflight)
    available = {template["env_name"] for template in templates}
    unknown = sorted(failed_names - available)
    if unknown:
        raise ValueError(
            "preflight references template environments absent from the dataset: "
            + ", ".join(unknown)
        )
    return [
        template for template in templates if template["env_name"] in failed_names
    ]


def prepare_selected_templates(
    templates: list[dict[str, str]],
    work_root: Path,
    timeout: int,
    retries: int,
    workers: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any] | None] = [None] * len(templates)
    futures: dict[Future[dict[str, Any]], tuple[int, dict[str, str]]] = {}
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="brt5-tdd-template",
    ) as executor:
        for index, template in enumerate(templates):
            print(
                f"[{index + 1:02d}/{len(templates):02d}] "
                f"PREPARE {template['env_name']}",
                flush=True,
            )
            future = executor.submit(
                prepare_template,
                template,
                work_root,
                timeout,
                retries,
            )
            futures[future] = (index, template)

        completed = 0
        for future in as_completed(futures):
            index, template = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - worker safety net
                result = {
                    **template,
                    "status": "FAILED",
                    "attempts": [],
                    "result": {
                        "status": "EXCEPTION",
                        "returncode": 1,
                        "error": repr(exc),
                        "traceback": traceback.format_exc(),
                    },
                }
            results[index] = result
            completed += 1
            write_json(
                work_root / "repair_results.json",
                [item for item in results if item is not None],
            )
            print(
                f"[{completed:02d}/{len(templates):02d}] "
                f"{result['status']} {template['env_name']}",
                flush=True,
            )

    return [item for item in results if item is not None]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare TDD-Bench dependency templates using local Conda."
    )
    parser.add_argument("--instances_path", type=Path, required=True)
    parser.add_argument("--work_root", type=Path, required=True)
    parser.add_argument("--preflight_report", type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.timeout <= 0 or args.retries <= 0:
        parser.error("--timeout and --retries must be positive")
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be between 1 and 8")

    dataset = args.instances_path.expanduser().resolve()
    work_root = args.work_root.expanduser().resolve()
    preflight = None
    if args.preflight_report:
        report_path = args.preflight_report.expanduser().resolve()
        preflight = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(preflight, dict):
            parser.error("--preflight_report must contain a JSON object")
        blockers = preflight_repair_blockers(preflight)
        if blockers:
            parser.error(
                "preflight contains failures outside template dependencies: "
                + "; ".join(blockers)
            )

    templates = load_unique_templates(dataset)
    selected = select_templates_for_repair(templates, preflight)
    work_root.mkdir(parents=True, exist_ok=True)
    print(f"dataset={dataset}", flush=True)
    print(f"dataset_template_groups={len(templates)}", flush=True)
    print(f"selected_template_groups={len(selected)}", flush=True)
    print(f"workers={args.workers}", flush=True)

    results = prepare_selected_templates(
        selected,
        work_root,
        args.timeout,
        args.retries,
        args.workers,
    ) if selected else []
    ready = [result["env_name"] for result in results if result["status"] == "READY"]
    failed = [result["env_name"] for result in results if result["status"] != "READY"]
    failure_details = [
        {
            "env_name": result["env_name"],
            "instance_id": result["instance_id"],
            "repo": result["repo"],
            "version": result["version"],
            **attempt_diagnostics(result.get("attempts") or []),
        }
        for result in results
        if result["status"] != "READY"
    ]
    summary = {
        "dataset": str(dataset),
        "work_root": str(work_root),
        "dataset_template_groups": len(templates),
        "selected_template_groups": len(selected),
        "ready_count": len(ready),
        "failed_count": len(failed),
        "ready": ready,
        "failed": failed,
        "failure_details": failure_details,
    }
    write_json(work_root / "summary.json", summary)
    write_json(work_root / "failure_diagnostics.json", failure_details)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Export BRT tests and execute the official Docker harness safely."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt6.evaluation.official_benchmarks import (  # noqa: E402
    export_official_predictions,
    generation_completeness,
)
from brt6.evaluation.swtbench_runtime_compat import (  # noqa: E402
    _configure_container_reuse,
)


INCOMPLETE_GENERATION_EXIT = 3


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def _git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _safe_run_id(value: str) -> str:
    rendered = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("._")
    if not rendered:
        raise ValueError("run_id is empty after sanitization")
    return rendered


def _link_official_sources(workspace: Path, official_root: Path, dataset: str) -> None:
    names = ("src", "dataset") if dataset == "swt" else ("tddbench",)
    for name in names:
        destination = workspace / name
        source = official_root / name
        if destination.exists() or destination.is_symlink():
            continue
        destination.symlink_to(source, target_is_directory=True)


def _install_swt_runtime_shim(workspace: Path) -> Path:
    sitecustomize = workspace / "sitecustomize.py"
    sitecustomize.write_text(
        "from brt6.evaluation.swtbench_runtime_compat import install\n"
        "install()\n",
        encoding="utf-8",
    )
    return sitecustomize


def _report_path(
    workspace: Path, dataset: str, model_name: str, run_id: str
) -> Path:
    filename = f"{model_name.replace('/', '__')}.{run_id}.json"
    if dataset == "swt":
        return workspace / "evaluation_results" / filename
    return workspace / filename


def _metrics_from_report(
    report: dict[str, Any], dataset: str, official_commit: str
) -> dict[str, Any]:
    total = int(report.get("total_instances") or 0)
    resolved = int(report.get("resolved_instances") or 0)
    failed = max(0, total - resolved)
    metrics: dict[str, Any] = {
        "evaluator": "official_swtbench" if dataset == "swt" else "official_tddbench",
        "official_harness_commit": official_commit,
        "total_instances": total,
        "f2p_success": resolved,
        "f2p_fail": failed,
        "f2p_at_1_percent": (100.0 * resolved / total) if total else 0.0,
        "by_status": {
            "resolved": resolved,
            "unresolved_or_missing": failed,
            "errors": int(report.get("error_instances") or 0),
        },
        "official_report": report,
    }
    if dataset == "swt":
        metrics["patch_cov_at_1_percent"] = report.get("Mean coverage")
        metrics["patch_cov_delta_at_1_percent"] = report.get("Mean coverage delta")
        metrics["patch_cov_definition"] = "SWT-Bench official Docker harness"
    else:
        metrics["tdd_score"] = report.get("final_score")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=("swt", "tdd"), required=True)
    parser.add_argument("--outputs-dir", required=True)
    parser.add_argument("--dataset-file", required=True)
    parser.add_argument("--evaluation-dir", required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model-name", default="brt6-deepseek-v3")
    parser.add_argument("--compute-coverage", type=parse_bool, default=True)
    parser.add_argument("--official-python", default="")
    parser.add_argument("--swtbench-root", default=str(PACKAGE_ROOT / "swt-bench"))
    parser.add_argument(
        "--tddbench-root", default=str(PACKAGE_ROOT / "TDD-Bench-Verified")
    )
    args = parser.parse_args()

    run_id = _safe_run_id(args.run_id)
    evaluation_dir = Path(args.evaluation_dir).resolve()
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = evaluation_dir / "official_predictions.json"
    export_manifest = export_official_predictions(
        args.outputs_dir,
        args.dataset_file,
        predictions_path,
        model_name=args.model_name,
    )
    generation_gate = generation_completeness(export_manifest)
    manifest_path = evaluation_dir / "official_run_manifest.json"
    if not generation_gate["complete"]:
        now = datetime.now(timezone.utc).astimezone().isoformat()
        refusal = {
            "schema_version": 1,
            "status": "refused_incomplete_generation",
            "started_at": now,
            "finished_at": now,
            "returncode": INCOMPLETE_GENERATION_EXIT,
            "dataset": args.dataset,
            "run_id": run_id,
            "model_name": args.model_name,
            "predictions": export_manifest,
            "generation_gate": generation_gate,
            "reason": generation_gate["reason"],
            "official_harness_invoked": False,
        }
        manifest_path.write_text(
            json.dumps(refusal, indent=2) + "\n", encoding="utf-8"
        )
        print(generation_gate["reason"], file=sys.stderr)
        return INCOMPLETE_GENERATION_EXIT

    official_root = Path(
        args.swtbench_root if args.dataset == "swt" else args.tddbench_root
    ).resolve()
    required_entry = (
        official_root / "src" / "main.py"
        if args.dataset == "swt"
        else official_root / "tddbench" / "harness" / "run_evaluation.py"
    )
    if not required_entry.is_file():
        raise SystemExit(f"official harness is missing: {required_entry}")
    if shutil.which("docker") is None:
        raise SystemExit("Docker CLI is required by the official benchmark harness")
    docker_check = subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if docker_check.returncode != 0:
        raise SystemExit("Docker daemon is unavailable")

    official_python = Path(
        args.official_python
        or os.environ.get(
            "SWTBENCH_PYTHON" if args.dataset == "swt" else "TDDBENCH_PYTHON",
            sys.executable,
        )
    ).expanduser().resolve()
    if not official_python.is_file():
        raise SystemExit(f"official harness Python is missing: {official_python}")

    workspace = evaluation_dir / "official_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _link_official_sources(workspace, official_root, args.dataset)
    runtime_shim = _install_swt_runtime_shim(workspace) if args.dataset == "swt" else None
    if args.dataset == "swt":
        command = [
            str(official_python),
            "-m",
            "src.main",
            "--dataset_name",
            "princeton-nlp/SWE-bench_Lite",
            "--predictions_path",
            str(predictions_path),
            "--filter_swt",
            "--max_workers",
            str(args.max_workers),
            "--run_id",
            run_id,
            "--compute_coverage",
            str(args.compute_coverage).lower(),
            "--timeout",
            str(args.timeout),
        ]
    else:
        command = [
            str(official_python),
            "-m",
            "tddbench.harness.run_evaluation",
            "--dataset_name",
            str(Path(args.dataset_file).resolve()),
            "--predictions_path",
            str(predictions_path),
            "--patch_path",
            "gold",
            "--max_workers",
            str(args.max_workers),
            "--run_id",
            run_id,
            "--timeout",
            str(args.timeout),
        ]

    official_commit = _git_head(official_root)
    environment = os.environ.copy()
    if args.dataset == "swt":
        _configure_container_reuse(environment)
    python_paths = [str(workspace), str(PACKAGE_ROOT), str(official_root), str(official_root / "src")]
    if environment.get("PYTHONPATH"):
        python_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    command_text = " ".join(subprocess.list2cmdline([part]) for part in command)
    manifest = {
        "schema_version": 1,
        "status": "running",
        "started_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "dataset": args.dataset,
        "run_id": run_id,
        "model_name": args.model_name,
        "official_root": str(official_root),
        "official_commit": official_commit,
        "official_entrypoint": str(required_entry),
        "official_python": str(official_python),
        "predictions": export_manifest,
        "generation_gate": generation_gate,
        "official_harness_invoked": True,
        "runtime_compatibility": (
            {
                "mode": "official_harness_host_safety_shim",
                "sitecustomize": str(runtime_shim),
                "shim_source": str(PROJECT_ROOT / "evaluation" / "swtbench_runtime_compat.py"),
                "changes": [
                    "stage the exact base_commit from the local source cache instead of cloning GitHub inside Docker",
                    "treat an already-absent instance image as successful cleanup",
                    "make official exception stringification side-effect free",
                    "rotate host-side official harness logs at a bounded size",
                    "preserve the shared cached instance image after all six official evaluation states finish",
                    "reuse one readable persistent container per benchmark instance across all six states",
                    "serialize each instance with a lock stored under /root",
                    "remove runtime package installation from cached-image evaluation scripts",
                    "decode Docker test output as strict UTF-8 first and auditably escape only invalid bytes",
                    "patch both official run_evaluation module aliases loaded by src.main",
                ],
                "unchanged": [
                    "dataset", "test commands", "patches", "coverage", "grading", "Docker environment recipes", "official image keys"
                ],
            }
            if runtime_shim is not None
            else None
        ),
        "command": command,
        "cwd": str(workspace),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (evaluation_dir / "official_command.txt").write_text(
        command_text + "\n", encoding="utf-8"
    )

    log_path = evaluation_dir / "official_harness.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    report_path = _report_path(workspace, args.dataset, args.model_name, run_id)
    report: dict[str, Any] = {}
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        (evaluation_dir / "official_report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        metrics = _metrics_from_report(report, args.dataset, official_commit)
        (evaluation_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
        )
    manifest.update(
        {
            "status": "complete" if process.returncode == 0 and report else "failed",
            "finished_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "returncode": process.returncode,
            "official_report_path": str(report_path),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if process.returncode != 0:
        return process.returncode
    if not report:
        print(f"official harness did not create its report: {report_path}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

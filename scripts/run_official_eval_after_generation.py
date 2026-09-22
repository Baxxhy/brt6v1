#!/usr/bin/env python3
"""Export BRT tests and execute the official Docker harness safely."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
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
    _configure_official_environment,
)
from brt6.execution.executor import run_subprocess_tree, terminate_process_tree  # noqa: E402


F2P_ONLY_MARKER = "F2P_ONLY"
DEFAULT_LOCAL_SWT_DATASET = (
    PROJECT_ROOT / "data" / "official" / "swe_bench_lite_test.json"
)


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def _f2p_only_marker(evaluation_dir: Path) -> Path | None:
    """Return the run-scoped marker that disables coverage for this evaluation."""

    candidates = [evaluation_dir / F2P_ONLY_MARKER]
    if len(evaluation_dir.parents) >= 2:
        candidates.append(evaluation_dir.parents[1] / F2P_ONLY_MARKER)
    return next((path for path in candidates if path.is_file()), None)


def _git_head(path: Path) -> str:
    commit_marker = path / "OFFICIAL_COMMIT"
    if commit_marker.is_file():
        return commit_marker.read_text(encoding="utf-8").strip()
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


def _running_official_container_ids(workspace: Path) -> set[str]:
    """Return the currently running official evaluation container IDs."""

    listed = run_subprocess_tree(
        ["docker", "ps", "-q", "--filter", "name=exec.eval."],
        str(workspace),
        60,
    )
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "docker ps failed")
    return {line.strip() for line in listed.stdout.splitlines() if line.strip()}


def _stop_running_official_containers(
    workspace: Path,
    preexisting_container_ids: set[str],
) -> None:
    """Stop only official containers created by this harness invocation."""

    try:
        container_ids = sorted(
            _running_official_container_ids(workspace) - preexisting_container_ids
        )
        if container_ids:
            killed = run_subprocess_tree(
                ["docker", "kill", *container_ids],
                str(workspace),
                600,
            )
            if killed.returncode != 0:
                raise RuntimeError(killed.stderr.strip() or "docker kill failed")
    except Exception as error:  # cleanup failure must not hide the interrupt
        print(f"failed to stop interrupted official containers: {error}", file=sys.stderr)


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


def _summarize_runtime_audits(workspace: Path) -> dict[str, Any]:
    """Summarize image identities and pre-test infrastructure failures."""

    image_ids: dict[str, set[str]] = {}
    environment_failures: list[dict[str, str]] = []
    fingerprints = list(workspace.rglob("environment_fingerprint.json"))
    for path in fingerprints:
        payload = json.loads(path.read_text(encoding="utf-8"))
        configured = str(payload.get("configured_image") or "")
        image_id = str(payload.get("image_id") or "")
        if configured and image_id:
            image_ids.setdefault(configured, set()).add(image_id)
    audits = list(workspace.rglob("execution_audit.json"))
    for path in audits:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("environment_failure") is True:
            environment_failures.append(
                {
                    "path": str(path.relative_to(workspace)),
                    "reason": str(payload.get("environment_failure_reason") or "unknown"),
                }
            )
    return {
        "schema_version": 1,
        "evaluated_states": len(fingerprints),
        "audited_states": len(audits),
        "image_ids_by_tag": {
            key: sorted(values) for key, values in sorted(image_ids.items())
        },
        "image_identity_conflicts": {
            key: sorted(values)
            for key, values in sorted(image_ids.items())
            if len(values) > 1
        },
        "environment_failure_count": len(environment_failures),
        "environment_failures": environment_failures,
        "official_score_changed": False,
    }


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
    parser.add_argument(
        "--container-isolation",
        choices=("fresh", "reuse"),
        default="fresh",
        help="fresh is reproducible; reuse is an exploratory speed optimization",
    )
    parser.add_argument(
        "--swtbench-root",
        default=str(PROJECT_ROOT / "evaluation/vendor/swtbench"),
    )
    parser.add_argument(
        "--official-dataset-name",
        default=str(DEFAULT_LOCAL_SWT_DATASET),
    )
    parser.add_argument(
        "--instance-id",
        action="append",
        default=[],
        help="Evaluate only this instance; repeat for a targeted reevaluation.",
    )
    parser.add_argument(
        "--tddbench-root", default=str(PACKAGE_ROOT / "TDD-Bench-Verified")
    )
    args = parser.parse_args()

    run_id = _safe_run_id(args.run_id)
    evaluation_dir = Path(args.evaluation_dir).resolve()
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    f2p_only_marker = _f2p_only_marker(evaluation_dir)
    if f2p_only_marker is not None:
        args.compute_coverage = False
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
        print(
            generation_gate["reason"]
            + "; continuing with empty patches counted as F2P failures",
            file=sys.stderr,
        )

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
        official_dataset_name = str(Path(args.official_dataset_name).resolve())
        if not Path(official_dataset_name).is_file():
            raise SystemExit(
                f"local official SWT dataset is missing: {official_dataset_name}"
            )
        command = [
            str(official_python),
            "-m",
            "src.main",
            "--dataset_name",
            official_dataset_name,
            "--predictions_path",
            str(predictions_path),
            "--filter_swt",
        ]
        if args.instance_id:
            command.extend(["--instance_ids", *dict.fromkeys(args.instance_id)])
        command.extend(
            [
                "--max_workers",
                str(args.max_workers),
                "--run_id",
                run_id,
                "--compute_coverage",
                str(args.compute_coverage).lower(),
                "--timeout",
                str(args.timeout),
            ]
        )
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
        _configure_official_environment(
            environment, isolation=args.container_isolation
        )
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
        "official_dataset_name": (
            official_dataset_name if args.dataset == "swt" else args.dataset_file
        ),
        "predictions": export_manifest,
        "generation_gate": generation_gate,
        "generation_policy": "evaluate_available_count_missing_as_f2p_failure",
        "missing_generation_ids": generation_gate["missing_ids"],
        "requested_instance_ids": list(dict.fromkeys(args.instance_id)),
        "official_harness_invoked": True,
        "evaluation_scope": "f2p_only" if not args.compute_coverage else "f2p_and_coverage",
        "compute_coverage": args.compute_coverage,
        "container_isolation": args.container_isolation if args.dataset == "swt" else None,
        "f2p_only_marker": str(f2p_only_marker or ""),
        "runtime_compatibility": (
            {
                "mode": "official_harness_host_safety_shim",
                "sitecustomize": str(runtime_shim),
                "shim_source": str(PROJECT_ROOT / "evaluation" / "swtbench_runtime_compat.py"),
                "changes": [
                    "resolve official requirements metadata from the project-local cache",
                    "load the unchanged SWE-bench Lite rows from the project-local official cache",
                    "treat an already-absent instance image as successful cleanup",
                    "make official exception stringification side-effect free",
                    "rotate host-side official harness logs at a bounded size",
                    "preserve the shared cached instance image after all six official evaluation states finish",
                    (
                        "create a fresh container for every official evaluation state"
                        if args.container_isolation == "fresh"
                        else "reuse one official container per instance across all six states"
                    ),
                    "serialize each instance with a lock stored under /root",
                    "restore the benchmark checkout and remove ordinary untracked files before and after every state",
                    "verify the base commit and a clean tracked checkout before applying each state",
                    "reuse ignored build artifacts baked into the official image and skip project reinstall",
                    "force optional pip commands and dataset access offline",
                    "record the Docker image identity and audit pre-test environment failures for every state",
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
    process: subprocess.Popen | None = None
    previous_handlers: dict[int, Any] = {}
    try:
        preexisting_official_containers = _running_official_container_ids(workspace)
    except Exception as error:
        # Without a baseline, daemon-wide cleanup cannot establish ownership.
        # Continue the evaluation, but leave interrupt cleanup conservative.
        print(
            "failed to snapshot preexisting official containers; "
            f"interrupt cleanup will not kill daemon-wide containers: {error}",
            file=sys.stderr,
        )
        preexisting_official_containers = set()
        container_cleanup_safe = False
    else:
        container_cleanup_safe = True

    def request_shutdown(signum, _frame):
        raise KeyboardInterrupt(f"official evaluation interrupted by signal {signum}")

    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = process.wait()
            except BaseException:
                terminate_process_tree(process)
                if container_cleanup_safe:
                    _stop_running_official_containers(
                        workspace,
                        preexisting_official_containers,
                    )
                raise
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    report_path = _report_path(workspace, args.dataset, args.model_name, run_id)
    report: dict[str, Any] = {}
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        (evaluation_dir / "official_report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        metrics = _metrics_from_report(report, args.dataset, official_commit)
        if not args.compute_coverage:
            metrics.pop("patch_cov_at_1_percent", None)
            metrics.pop("patch_cov_delta_at_1_percent", None)
            metrics.pop("patch_cov_definition", None)
            metrics["evaluation_scope"] = "f2p_only"
        (evaluation_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
        )
    runtime_audit = _summarize_runtime_audits(workspace)
    (evaluation_dir / "runtime_audit_summary.json").write_text(
        json.dumps(runtime_audit, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest.update(
        {
            "status": "complete" if returncode == 0 and report else "failed",
            "finished_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "returncode": returncode,
            "official_report_path": str(report_path),
            "runtime_audit": runtime_audit,
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if returncode != 0:
        return returncode
    if not report:
        print(f"official harness did not create its report: {report_path}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

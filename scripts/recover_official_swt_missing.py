#!/usr/bin/env python3
"""Recover only missing SWT generation artifacts, then gate full evaluation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt6.llm.api_pool import configured_apis  # noqa: E402
from brt6.runtime.official_docker_runtime import docker_storage_preflight  # noqa: E402


EXPECTED_MISSING_COUNT = 22


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _load_rows(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else list(raw.values())
    if not rows or any(not str(row.get("instance_id") or "") for row in rows):
        raise ValueError(f"invalid dataset: {path}")
    return rows


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _run_logged(
    command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{_now()}] command={json.dumps(command)}\n")
        log.flush()
        process = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write(f"[{_now()}] returncode={process.returncode}\n")
    return int(process.returncode)


def _pipeline_command(
    args: argparse.Namespace,
    ids: list[str],
    workers: int,
    recovery_dir: Path,
) -> list[str]:
    return [
        args.framework_python,
        "-m",
        "brt6.pipeline.run",
        "--instances_path",
        str(args.instances_path),
        "--code_retrieval_path",
        str(args.code_retrieval),
        "--test_retrieval_path",
        str(args.test_retrieval),
        "--repo_root_base",
        str(args.repo_root),
        "--output_dir",
        str(recovery_dir),
        "--llm-provider",
        "deepseek",
        "--model",
        "deepseek-v3",
        "--instance_ids",
        ",".join(ids),
        "--max_workers",
        str(workers),
        "--max_feedback_rounds",
        "3",
        "--max_env_rounds",
        "2",
        "--max_brt_rounds",
        "3",
        "--validation_mode",
        "buggy_only",
        "--timeout",
        "1800",
        "--temperature",
        "0.1",
        "--max_tokens",
        "4096",
        "--enable_behavior_target",
        "true",
        "--enable_seed_mutation",
        "true",
        "--enable_specialized_feedback",
        "true",
        "--enable_environment_feedback",
        "true",
        "--enable_trigger_feedback",
        "true",
        "--enable_assertion_feedback",
        "true",
        "--behavior-target-cache",
        str(args.behavior_cache),
        "--dataset_mode",
        "swt",
        "--runtime_backend",
        "official_docker",
        "--official_harness_python",
        args.official_python,
        "--swtbench_root",
        str(args.swtbench_root),
        "--tddbench_root",
        str(args.tddbench_root),
        "--resume",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--instances-path",
        type=Path,
        default=PROJECT_ROOT / "data/issues/swt276_issues.json",
    )
    parser.add_argument(
        "--behavior-cache",
        type=Path,
        default=PROJECT_ROOT
        / "data/behavior_targets/swt/full_method_f2p_47_46_20260717",
    )
    parser.add_argument(
        "--code-retrieval",
        type=Path,
        default=PROJECT_ROOT
        / "retrieval_results/code/code_retrieval_results_gpt.json",
    )
    parser.add_argument(
        "--test-retrieval",
        type=Path,
        default=PROJECT_ROOT
        / "retrieval_results/test/icore/gpt/related_tests.json",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=PACKAGE_ROOT / "swe_repos"
    )
    parser.add_argument(
        "--framework-python",
        default="/root/conda/ENTER/envs/icore/bin/python",
    )
    parser.add_argument(
        "--official-python",
        default="/root/conda/ENTER/envs/brt6_swtbench/bin/python",
    )
    parser.add_argument(
        "--swtbench-root", type=Path, default=PACKAGE_ROOT / "swt-bench"
    )
    parser.add_argument(
        "--tddbench-root",
        type=Path,
        default=PACKAGE_ROOT / "TDD-Bench-Verified",
    )
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()

    args.source_run = args.source_run.resolve()
    args.run_dir = args.run_dir.resolve()
    args.instances_path = args.instances_path.resolve()
    source_generation = args.source_run / "generation"
    source_gate = json.loads(
        (args.source_run / "generation_gate.json").read_text(encoding="utf-8")
    )
    missing_ids = [str(value) for value in source_gate.get("missing_ids", [])]
    if len(missing_ids) != EXPECTED_MISSING_COUNT or len(set(missing_ids)) != len(
        missing_ids
    ):
        raise SystemExit(
            "source recovery scope drifted: expected 22 unique missing IDs, got "
            f"{len(missing_ids)}"
        )
    rows = _load_rows(args.instances_path)
    dataset_ids = [str(row["instance_id"]) for row in rows]
    if len(rows) != 276 or not set(missing_ids).issubset(dataset_ids):
        raise SystemExit("frozen SWT-276 dataset does not match the recovery gate")
    source_present = [
        instance_id
        for instance_id in dataset_ids
        if _nonempty(source_generation / instance_id / "final_test.py")
    ]
    if len(source_present) != 254 or set(source_present) & set(missing_ids):
        raise SystemExit("source generation is not the expected frozen 254/276 run")
    storage = docker_storage_preflight()
    if not storage["ok"]:
        raise SystemExit("Docker storage preflight failed: " + "; ".join(storage["errors"]))
    api_entries = len(configured_apis("deepseek"))
    if api_entries < 1:
        raise SystemExit("DeepSeek API pool is empty")
    for path in (
        args.behavior_cache,
        args.code_retrieval,
        args.test_retrieval,
        args.repo_root,
        args.swtbench_root / "src/main.py",
        Path(args.framework_python),
        Path(args.official_python),
    ):
        if not path.exists():
            raise SystemExit(f"required recovery path is missing: {path}")
    if args.preflight_only:
        print(
            json.dumps(
                {
                    "status": "PASSED",
                    "dataset_size": len(rows),
                    "source_generated_tests": len(source_present),
                    "missing_generation": len(missing_ids),
                    "deepseek_api_entries": api_entries,
                    "docker_storage": storage,
                },
                sort_keys=True,
            )
        )
        return 0

    args.run_dir.mkdir(parents=True, exist_ok=True)
    logs = args.run_dir / "logs"
    logs.mkdir(exist_ok=True)
    (args.run_dir / "launcher.pid").write_text(
        f"{os.getpid()}\n", encoding="utf-8"
    )
    (args.run_dir / "launcher.command.json").write_text(
        json.dumps([sys.executable, *sys.argv], indent=2) + "\n",
        encoding="utf-8",
    )
    (args.run_dir / "launcher.started_at").write_text(
        _now() + "\n", encoding="utf-8"
    )
    recovery_generation = args.run_dir / "recovered_generation"
    recovery_generation.mkdir(exist_ok=True)
    matplotlib_ids = [
        value for value in missing_ids if value.startswith("matplotlib__")
    ]
    other_ids = [value for value in missing_ids if value not in matplotlib_ids]
    groups = [
        {"name": "non_matplotlib", "ids": other_ids, "workers": 2},
        {"name": "matplotlib_serial", "ids": matplotlib_ids, "workers": 1},
    ]
    recovery_manifest = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": _now(),
        "launcher_pid": os.getpid(),
        "launcher_command": [sys.executable, *sys.argv],
        "source_run": str(args.source_run),
        "source_generated_tests": len(source_present),
        "missing_ids": missing_ids,
        "groups": groups,
        "dataset": str(args.instances_path),
        "model": "deepseek-v3",
        "provider": "deepseek",
        "api_entries": api_entries,
        "storage_preflight": storage,
        "formal_evaluation_requires": 276,
    }
    (args.run_dir / "recovery_manifest.json").write_text(
        json.dumps(recovery_manifest, indent=2) + "\n", encoding="utf-8"
    )

    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(PACKAGE_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    environment["BRT_REQUIRE_OFFICIAL_DOCKER"] = "1"
    environment["BRT_DOCKER_API_TIMEOUT_SECONDS"] = "300"
    environment["BRT_OFFICIAL_DOCKER_STARTUP_TIMEOUT"] = "14400"
    environment["BRT_OFFICIAL_BUILD_RETRIES"] = "3"
    group_results = []
    for group in groups:
        command = _pipeline_command(
            args,
            group["ids"],
            group["workers"],
            recovery_generation,
        )
        returncode = _run_logged(
            command,
            cwd=PACKAGE_ROOT,
            env=environment,
            log_path=logs / f"generation_{group['name']}.log",
        )
        recovered = [
            instance_id
            for instance_id in group["ids"]
            if _nonempty(recovery_generation / instance_id / "final_test.py")
        ]
        group_results.append(
            {
                **group,
                "returncode": returncode,
                "recovered": recovered,
                "missing": sorted(set(group["ids"]) - set(recovered)),
            }
        )
        summary = recovery_generation / "summary.json"
        if summary.is_file():
            (args.run_dir / f"generation_summary_{group['name']}.json").write_text(
                summary.read_text(encoding="utf-8"), encoding="utf-8"
            )

    merged = args.run_dir / "generation_merged_276"
    merged.mkdir(exist_ok=True)
    recovered_set = {
        instance_id
        for instance_id in missing_ids
        if _nonempty(recovery_generation / instance_id / "final_test.py")
    }
    linked: list[str] = []
    incomplete: list[str] = []
    for instance_id in dataset_ids:
        source = (
            recovery_generation / instance_id
            if instance_id in recovered_set
            else source_generation / instance_id
        )
        target = merged / instance_id
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            raise SystemExit(f"merge target is not a symlink: {target}")
        if _nonempty(source / "final_test.py"):
            target.symlink_to(source.resolve(), target_is_directory=True)
            linked.append(instance_id)
        else:
            incomplete.append(instance_id)
    generation_gate = {
        "checked_at": _now(),
        "source_generated_tests": len(source_present),
        "recovered_tests": len(recovered_set),
        "merged_tests": len(linked),
        "missing_ids": incomplete,
        "artifacts_complete": len(linked) == len(dataset_ids) == 276,
        "formal_evaluation_allowed": len(linked) == len(dataset_ids) == 276,
        "group_results": group_results,
    }
    (args.run_dir / "generation_gate.json").write_text(
        json.dumps(generation_gate, indent=2) + "\n", encoding="utf-8"
    )
    if not generation_gate["formal_evaluation_allowed"]:
        recovery_manifest.update(
            {
                "status": "INCOMPLETE_GENERATION",
                "finished_at": _now(),
                "generation_gate": generation_gate,
                "formal_evaluation_invoked": False,
            }
        )
        (args.run_dir / "recovery_manifest.json").write_text(
            json.dumps(recovery_manifest, indent=2) + "\n", encoding="utf-8"
        )
        return 3

    evaluation = args.run_dir / "evaluation/formal_f2p"
    evaluation_command = [
        args.framework_python,
        str(PROJECT_ROOT / "scripts/run_official_eval_after_generation.py"),
        "--dataset",
        "swt",
        "--outputs-dir",
        str(merged),
        "--dataset-file",
        str(args.instances_path),
        "--max-workers",
        "2",
        "--timeout",
        "1800",
        "--evaluation-dir",
        str(evaluation),
        "--run-id",
        args.run_dir.name,
        "--model-name",
        "brt6-deepseek-v3",
        "--compute-coverage",
        "true",
        "--official-python",
        args.official_python,
        "--swtbench-root",
        str(args.swtbench_root),
        "--tddbench-root",
        str(args.tddbench_root),
    ]
    evaluation_returncode = _run_logged(
        evaluation_command,
        cwd=PROJECT_ROOT,
        env=environment,
        log_path=logs / "formal_evaluation.log",
    )
    recovery_manifest.update(
        {
            "status": "COMPLETE" if evaluation_returncode == 0 else "EVALUATION_ERROR",
            "finished_at": _now(),
            "generation_gate": generation_gate,
            "formal_evaluation_invoked": True,
            "evaluation_returncode": evaluation_returncode,
        }
    )
    (args.run_dir / "recovery_manifest.json").write_text(
        json.dumps(recovery_manifest, indent=2) + "\n", encoding="utf-8"
    )
    return evaluation_returncode


if __name__ == "__main__":
    raise SystemExit(main())

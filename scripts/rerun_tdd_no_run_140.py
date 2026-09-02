#!/usr/bin/env python3
"""Regenerate protocol-invalid cases and rerun the audited 140 TDD no-run rows.

The source 449-row result remains immutable.  The recovery output copies only
small generation metadata/final tests, regenerates protocol-invalid rows plus
any audited row whose generation never reached an executable protocol, reuses
the 309 unaffected formal rows, and evaluates exactly the 140 audited
environment/protocol rows with the current evaluator.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT.parent
DEFAULT_SOURCE_RUN = (
    PROJECT_ROOT
    / "results/runs/p0_simple_llm_selector_tdd_cached_bt_20260721_194243"
)
DEFAULT_DATASET = PACKAGE_ROOT / "TDD-Bench-Verified/TDD_Bench.json"
DEFAULT_CODE_RETRIEVAL = (
    PROJECT_ROOT / "retrieval_results/code/code_retrieval_results_gpt.json"
)
DEFAULT_TEST_RETRIEVAL = (
    PROJECT_ROOT / "retrieval_results/test/icore/gpt/related_tests.json"
)
DEFAULT_REPO_ROOT = PACKAGE_ROOT / "swe_repos"


STATIC_CATEGORIES = {
    "matplotlib_generation_native_headers": {
        "matplotlib__matplotlib-13989",
        "matplotlib__matplotlib-14623",
    },
    "isolated_conda_clone_timeout": {
        "matplotlib__matplotlib-20676",
        "matplotlib__matplotlib-20826",
    },
    "declared_or_external_dependency": {
        "django__django-12193",
        "pylint-dev__pylint-4661",
        "matplotlib__matplotlib-21568",
        "sympy__sympy-12419",
    },
    "all_skipped_or_noop": {
        "sympy__sympy-16792",
        "sphinx-doc__sphinx-10614",
        "pydata__xarray-4966",
        "matplotlib__matplotlib-23299",
        "django__django-10973",
        "django__django-12039",
    },
    "nested_runner_protocol": {
        "pytest-dev__pytest-5840",
        "pytest-dev__pytest-7432",
        "pytest-dev__pytest-7982",
    },
    "generation_or_collection_protocol": {
        "matplotlib__matplotlib-25122",
        "django__django-7530",
        "matplotlib__matplotlib-23412",
        "matplotlib__matplotlib-24026",
        "django__django-12155",
    },
}

REGENERATE_CATEGORIES = {
    "matplotlib_generation_native_headers",
    "all_skipped_or_noop",
    "nested_runner_protocol",
    "generation_or_collection_protocol",
}
NON_EXECUTABLE_GENERATION_STATUSES = {
    "ENV_UNRESOLVED",
    "SETUP_ERROR",
    "ERROR",
}


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_object(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def audited_categories(results: dict[str, dict]) -> dict[str, set[str]]:
    categories: dict[str, set[str]] = {
        name: set(values) for name, values in STATIC_CATEGORIES.items()
    }
    categories["django_coverage_runner_path"] = {
        instance_id
        for instance_id, result in results.items()
        if "no module named 'test_sqlite'"
        in json.dumps(result, ensure_ascii=False).lower()
    }
    categories["sphinx_declared_dependencies"] = {
        instance_id
        for instance_id, result in results.items()
        if "no module named 'docutils'"
        in json.dumps(result, ensure_ascii=False).lower()
    }
    owners: dict[str, list[str]] = defaultdict(list)
    for category, ids in categories.items():
        for instance_id in ids:
            owners[instance_id].append(category)
    overlaps = {key: value for key, value in owners.items() if len(value) != 1}
    if overlaps:
        raise ValueError(f"recovery categories overlap: {overlaps}")
    missing = sorted(set(owners) - set(results))
    if missing:
        raise ValueError(f"recovery IDs absent from source results: {missing}")
    if len(owners) != 140:
        raise ValueError(
            f"audited no-run set changed: found {len(owners)}, expected 140"
        )
    expected_counts = {
        "django_coverage_runner_path": 83,
        "sphinx_declared_dependencies": 35,
    }
    for category, expected in expected_counts.items():
        if len(categories[category]) != expected:
            raise ValueError(
                f"{category} has {len(categories[category])}, expected {expected}"
            )
    return categories


def copy_generation_metadata(
    source_generation: Path,
    target_generation: Path,
    instance_ids: set[str],
) -> None:
    target_generation.mkdir(parents=True, exist_ok=True)
    for instance_id in sorted(instance_ids):
        source = source_generation / instance_id
        target = target_generation / instance_id
        if not source.is_dir():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.is_file() and item.name != ".running":
                shutil.copy2(item, target / item.name)


def generation_regeneration_ids(
    source_generation: Path,
    audited_ids: set[str],
    protocol_regenerate_ids: set[str],
) -> set[str]:
    """Include audited rows whose retained artifact has no runnable protocol."""

    regenerate = set(protocol_regenerate_ids)
    for instance_id in audited_ids:
        instance_dir = source_generation / instance_id
        summary_path = instance_dir / "summary.json"
        final_path = instance_dir / "final_test.py"
        if not summary_path.is_file() or not final_path.is_file():
            regenerate.add(instance_id)
            continue
        summary = load_object(summary_path)
        if str(summary.get("status") or "") in NON_EXECUTABLE_GENERATION_STATUSES:
            regenerate.add(instance_id)
            continue
        if not summary.get("command") or not summary.get("candidate_repo_path"):
            regenerate.add(instance_id)
    return regenerate


def non_executable_generation_ids(
    generation: Path, expected_ids: set[str]
) -> set[str]:
    """Return regenerated rows that still lack a formally replayable protocol.

    A generated test may legitimately end the buggy-side feedback loop with a
    setup or collection failure caused by the test itself.  Formal evaluation
    must replay and count that result instead of repeatedly regenerating it.
    Worktree/environment preparation failures still fail this gate because
    they do not carry a candidate path and runner command.
    """

    bad: set[str] = set()
    for instance_id in expected_ids:
        instance_dir = generation / instance_id
        summary_path = instance_dir / "summary.json"
        final_path = instance_dir / "final_test.py"
        if not summary_path.is_file() or not final_path.is_file():
            bad.add(instance_id)
            continue
        try:
            summary = load_object(summary_path)
        except (OSError, ValueError, json.JSONDecodeError):
            bad.add(instance_id)
            continue
        if not summary.get("command") or not summary.get("candidate_repo_path"):
            bad.add(instance_id)
    return bad


def prepare_resume_workers(
    source_evaluation: Path,
    target_evaluation: Path,
    rerun_ids: set[str],
) -> int:
    workers = sorted(source_evaluation.glob("worker_*/results.json"))
    if not workers:
        raise ValueError(f"source evaluation has no worker results: {source_evaluation}")
    retained: set[str] = set()
    for source in workers:
        values = load_object(source)
        filtered = {
            key: value for key, value in values.items() if key not in rerun_ids
        }
        retained.update(filtered)
        write_object(
            target_evaluation / source.parent.name / "results.json", filtered
        )
    if len(retained) != 309:
        raise ValueError(
            f"expected 309 retained formal rows, found {len(retained)}"
        )
    return len(workers)


def run_checked(command: list[str], *, cwd: Path, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PACKAGE_ROOT) + (
        ":" + environment["PYTHONPATH"]
        if environment.get("PYTHONPATH")
        else ""
    )
    environment["BRT_TDD_STRICT_LOCAL_CONDA"] = "1"
    with log_path.open("a", encoding="utf-8") as log:
        log.write("COMMAND " + " ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed rc={completed.returncode}; see {log_path}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, default=DEFAULT_SOURCE_RUN)
    parser.add_argument("--output-run", type=Path, default=None)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--code-retrieval", type=Path, default=DEFAULT_CODE_RETRIEVAL)
    parser.add_argument("--test-retrieval", type=Path, default=DEFAULT_TEST_RETRIEVAL)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--behavior-target-cache", type=Path, default=None)
    parser.add_argument("--generation-workers", type=int, default=6)
    parser.add_argument("--evaluation-workers", type=int, default=6)
    parser.add_argument("--generation-retries", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    source_run = args.source_run.resolve()
    output_run = (
        args.output_run.resolve()
        if args.output_run
        else PROJECT_ROOT
        / "results/runs"
        / ("tdd_no_run_140_recovery_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    )
    source_eval = source_run / "evaluation/formal_f2p"
    source_results = load_object(source_eval / "merged_results.json")
    categories = audited_categories(source_results)
    rerun_ids = set().union(*categories.values())
    protocol_regenerate_ids = set().union(
        *(categories[name] for name in REGENERATE_CATEGORIES)
    )
    regenerate_ids = generation_regeneration_ids(
        source_run / "generation", rerun_ids, protocol_regenerate_ids
    )
    if len(protocol_regenerate_ids) != 16 or len(regenerate_ids) != 20:
        raise ValueError(
            "regeneration audit changed: "
            f"protocol={len(protocol_regenerate_ids)} (expected 16), "
            f"total={len(regenerate_ids)} (expected 20)"
        )

    completion = load_object(source_run / "completion.json")
    cache = (
        args.behavior_target_cache.resolve()
        if args.behavior_target_cache
        else Path(completion["behavior_target_source"]["cache_path"]).resolve()
    )
    output_run.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_run": str(source_run),
        "output_run": str(output_run),
        "dataset": str(args.dataset.resolve()),
        "behavior_target_cache": str(cache),
        "audited_count": len(rerun_ids),
        "regenerate_count": len(regenerate_ids),
        "protocol_regenerate_count": len(protocol_regenerate_ids),
        "formal_reuse_count": 449 - len(rerun_ids),
        "categories": {
            name: sorted(values) for name, values in sorted(categories.items())
        },
        "regenerate_ids": sorted(regenerate_ids),
    }
    write_object(output_run / "recovery_manifest.json", manifest)

    generation = output_run / "generation"
    copy_generation_metadata(
        source_run / "generation", generation, rerun_ids - regenerate_ids
    )
    generation_command = [
        sys.executable,
        "-m",
        "brt6.pipeline.run",
        "--instances_path",
        str(args.dataset.resolve()),
        "--code_retrieval_path",
        str(args.code_retrieval.resolve()),
        "--test_retrieval_path",
        str(args.test_retrieval.resolve()),
        "--repo_root_base",
        str(args.repo_root.resolve()),
        "--output_dir",
        str(generation),
        "--behavior-target-cache",
        str(cache),
        "--instance_ids",
        ",".join(sorted(regenerate_ids)),
        "--dataset_mode",
        "tdd",
        "--runtime_backend",
        "local_conda",
        "--max_workers",
        str(args.generation_workers),
        "--max_feedback_rounds",
        "3",
        "--max_env_rounds",
        "2",
        "--max_brt_rounds",
        "3",
        "--validation_mode",
        "buggy_only",
        "--timeout",
        str(args.timeout),
        "--temperature",
        "0.1",
        "--max_tokens",
        "4096",
        "--resume",
    ]
    run_checked(
        generation_command,
        cwd=PACKAGE_ROOT,
        log_path=output_run / "logs/regeneration.log",
    )
    generation_attempts = [
        {
            "attempt": 0,
            "instance_ids": sorted(regenerate_ids),
            "log": str(output_run / "logs/regeneration.log"),
        }
    ]
    unresolved = non_executable_generation_ids(generation, regenerate_ids)
    for attempt in range(1, max(0, args.generation_retries) + 1):
        if not unresolved:
            break
        for instance_id in unresolved:
            shutil.rmtree(generation / instance_id, ignore_errors=True)
        retry_command = list(generation_command)
        retry_command[retry_command.index("--instance_ids") + 1] = ",".join(
            sorted(unresolved)
        )
        retry_log = output_run / f"logs/regeneration_retry_{attempt}.log"
        run_checked(retry_command, cwd=PACKAGE_ROOT, log_path=retry_log)
        generation_attempts.append(
            {
                "attempt": attempt,
                "instance_ids": sorted(unresolved),
                "log": str(retry_log),
            }
        )
        unresolved = non_executable_generation_ids(generation, regenerate_ids)
    manifest["generation_attempts"] = generation_attempts
    manifest["unresolved_after_generation"] = sorted(unresolved)
    write_object(output_run / "recovery_manifest.json", manifest)
    if unresolved:
        raise RuntimeError(
            "formal evaluation refused: regenerated rows remain non-executable: "
            + ", ".join(sorted(unresolved))
        )

    evaluation = output_run / "evaluation/formal_f2p"
    source_worker_count = prepare_resume_workers(
        source_eval, evaluation, rerun_ids
    )
    if source_worker_count != args.evaluation_workers:
        raise ValueError(
            "evaluation worker count must match the source partition: "
            f"source={source_worker_count}, requested={args.evaluation_workers}"
        )
    formal_command = [
        sys.executable,
        "-m",
        "brt6.evaluation.formal_eval",
        "--instances_path",
        str(args.dataset.resolve()),
        "--generated_dir",
        str(generation),
        "--repo_root_base",
        str(args.repo_root.resolve()),
        "--output_dir",
        str(evaluation),
        "--max_workers",
        str(args.evaluation_workers),
        "--timeout",
        str(args.timeout),
        "--eval_clone_root",
        str(evaluation / "eval_clones"),
        "--dataset_mode",
        "tdd",
        "--compute_patch_coverage",
        "true",
        "--resume",
    ]
    run_checked(
        formal_command,
        cwd=PACKAGE_ROOT,
        log_path=output_run / "logs/formal_eval.log",
    )

    metrics = load_object(evaluation / "metrics.json")
    recovered = load_object(evaluation / "merged_results.json")
    status_changes = {
        instance_id: {
            "before": source_results[instance_id].get("status"),
            "after": recovered[instance_id].get("status"),
            "env_error_category": recovered[instance_id].get(
                "env_error_category", ""
            ),
        }
        for instance_id in sorted(rerun_ids)
    }
    write_object(output_run / "status_changes.json", status_changes)
    write_object(
        output_run / "completion.json",
        {
            "finished_at": datetime.now().astimezone().isoformat(),
            "source_run": str(source_run),
            "rerun_instances": len(rerun_ids),
            "regenerated_instances": len(regenerate_ids),
            "formal_total_instances": metrics.get("total_instances"),
            "f2p_success": metrics.get("f2p_success"),
            "f2p_at_1_percent": metrics.get("f2p_at_1_percent"),
            "tdd_score": metrics.get("tdd_score"),
            "tdd_score_percent": metrics.get("tdd_score_percent"),
            "by_status": metrics.get("by_status"),
            "by_env_error_category": metrics.get("by_env_error_category"),
        },
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

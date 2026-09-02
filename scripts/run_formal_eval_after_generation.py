#!/usr/bin/env python3
"""Start true-patch direct evaluation only after generation is complete."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent


def parse_bool(value: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true/false")


def load_rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return [dict(value, instance_id=value.get("instance_id", key)) for key, value in data.items() if isinstance(value, dict)]


def select_rows(rows: list[dict], instance_ids: str) -> list[dict]:
    """Select an auditable pilot subset without creating a second dataset."""

    requested = [value.strip() for value in instance_ids.split(",") if value.strip()]
    if not requested:
        return rows
    if len(requested) != len(set(requested)):
        raise ValueError("--instance_ids contains duplicate instance IDs")
    by_id = {str(row.get("instance_id") or ""): row for row in rows}
    missing = [instance_id for instance_id in requested if instance_id not in by_id]
    if missing:
        raise ValueError(
            "--instance_ids contains IDs absent from the dataset: "
            + ", ".join(missing)
        )
    return [by_id[instance_id] for instance_id in requested]


def validate_runtime_contract(
    dataset_mode: str,
    runtime_backend: str,
    rows: list[dict],
) -> dict:
    contract = {
        "dataset_mode": dataset_mode,
        "runtime_backend": runtime_backend,
        "evaluation_module": "brt6.evaluation.formal_eval",
        "docker_harness_invoked": False,
        "upstream_tdd_harness_invoked": False,
        "dataset_instances": len(rows),
    }
    if dataset_mode != "tdd":
        return contract
    if runtime_backend != "local_conda":
        raise ValueError(
            "TDD evaluation only supports runtime_backend=local_conda; "
            "the upstream Docker harness is intentionally disabled"
        )
    missing_patch = [
        str(row.get("instance_id") or "")
        for row in rows
        if not str(row.get("patch") or "").strip()
    ]
    missing_setup_commit = [
        str(row.get("instance_id") or "")
        for row in rows
        if not str(row.get("environment_setup_commit") or "").strip()
    ]
    if missing_patch:
        raise ValueError(
            "TDD rows must carry their golden patch; refusing SWE-bench Lite "
            f"fallback for {len(missing_patch)} rows"
        )
    if missing_setup_commit:
        raise ValueError(
            "TDD rows must carry environment_setup_commit; missing for "
            f"{len(missing_setup_commit)} rows"
        )
    contract.update(
        {
            "gold_patch_rows": len(rows),
            "environment_setup_commit_rows": len(rows),
            "swebench_lite_fallback_allowed": False,
            "strict_runtime_scrub": True,
        }
    )
    return contract


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs_dir", required=True)
    parser.add_argument(
        "--dataset_file", default=str(PROJECT_ROOT / "data/issues/swt276_issues.json")
    )
    parser.add_argument("--patch_file", default="")
    parser.add_argument("--repo_root_base", default=str(WORKSPACE_ROOT / "swe_repos"))
    parser.add_argument("--max_workers", type=int, default=6)
    parser.add_argument("--eval_completed_only", type=parse_bool, default=False)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--evaluation_dir", default="")
    parser.add_argument("--eval_clone_root", default="")
    parser.add_argument("--eval_worktree_root", default="")
    parser.add_argument("--log_path", default="")
    parser.add_argument("--summary_path", default="")
    parser.add_argument(
        "--instance_ids",
        default="",
        help="Comma-separated pilot subset; denominator becomes this exact subset.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--compute_patch_coverage", type=parse_bool, default=True)
    parser.add_argument("--dataset_mode", choices=("swt", "tdd"), default="swt")
    parser.add_argument("--runtime_backend", default="local_conda")
    parser.add_argument(
        "--keep_eval_worktrees",
        action="store_true",
        help="Keep isolated evaluation clones so subsequent runs reuse the clean checkout.",
    )
    parser.add_argument(
        "--use_generated_worktrees",
        action="store_true",
        help=(
            "Opt in to evaluating inside generation/<instance>/worktree. "
            "The default creates isolated formal-eval local clones under --eval_clone_root."
        ),
    )
    args = parser.parse_args()
    if args.dataset_mode == "tdd" and args.eval_completed_only:
        parser.error(
            "TDD-Bench final score requires the complete dataset denominator; "
            "do not use --eval_completed_only true"
        )
    # The child evaluator runs from the workspace parent (``WORKSPACE_ROOT``).
    # Resolve
    # user-provided paths before that cwd change so commands launched from the
    # BRT6 checkout do not turn a valid generation directory into a false
    # preflight failure.
    outputs = Path(args.outputs_dir).resolve()
    try:
        rows = select_rows(load_rows(Path(args.dataset_file)), args.instance_ids)
    except ValueError as exc:
        parser.error(str(exc))
    completed = [row for row in rows if (outputs / str(row.get("instance_id")) / "final_test.py").is_file()]
    missing = [str(row.get("instance_id")) for row in rows if row not in completed]
    print(f"已生成 {len(completed)}/{len(rows)}，未完成 {len(missing)}")
    if missing:
        print("未完成实例:", ", ".join(missing[:50]))
    evaluation_rows = completed if args.eval_completed_only else rows
    if not evaluation_rows:
        return 1
    if args.patch_file:
        if len(evaluation_rows) != 1:
            print("--patch_file 只支持单 instance；批量请使用包含 patch 的 dataset 或 SWE-bench 数据加载。", file=sys.stderr)
            return 2
        evaluation_rows[0]["patch"] = Path(args.patch_file).read_text(encoding="utf-8")
    formal_dir = Path(args.evaluation_dir).resolve() if args.evaluation_dir else outputs / "formal_eval"
    formal_dir.mkdir(parents=True, exist_ok=True)
    try:
        runtime_contract = validate_runtime_contract(
            args.dataset_mode, args.runtime_backend, evaluation_rows
        )
    except ValueError as exc:
        print(f"runtime contract error: {exc}", file=sys.stderr)
        return 2
    (formal_dir / "runtime_contract.json").write_text(
        json.dumps(runtime_contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    eval_clone_root = (
        Path(args.eval_clone_root).resolve()
        if args.eval_clone_root
        else formal_dir / "eval_clones"
    )
    eval_worktree_root = (
        Path(args.eval_worktree_root).resolve()
        if args.eval_worktree_root
        else Path("")
    )
    log_path = (
        Path(args.log_path).resolve()
        if args.log_path
        else PROJECT_ROOT / "logs/formal_eval.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump(evaluation_rows, handle, ensure_ascii=False)
        filtered_path = handle.name
    command = [
        sys.executable, "-m", "brt6.evaluation.formal_eval",
        "--instances_path", filtered_path,
        "--generated_dir", str(outputs),
        "--repo_root_base", args.repo_root_base,
        "--output_dir", str(formal_dir),
        "--max_workers", str(args.max_workers),
        "--timeout", str(args.timeout),
        "--eval_clone_root", str(eval_clone_root),
        "--dataset_mode", args.dataset_mode,
    ]
    if args.eval_worktree_root:
        command.extend(["--eval_worktree_root", str(eval_worktree_root)])
    if args.use_generated_worktrees:
        command.append("--use_generated_worktrees")
    if args.compute_patch_coverage:
        command.extend(["--compute_patch_coverage", "true"])
    if args.keep_eval_worktrees:
        command.append("--keep_eval_worktrees")
    if (
        args.dataset_mode != "tdd"
        and not args.patch_file
        and not all(row.get("patch") for row in evaluation_rows)
    ):
        command.append("--use_swebench_lite")
    if args.resume:
        command.append("--resume")
    try:
        log_mode = "a" if args.resume else "w"
        child_env = os.environ.copy()
        if args.dataset_mode == "tdd":
            child_env["BRT_TDD_STRICT_LOCAL_CONDA"] = "1"
        with log_path.open(log_mode, encoding="utf-8") as log:
            proc = subprocess.Popen(
                command,
                cwd=WORKSPACE_ROOT,
                env=child_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            returncode = proc.wait()
    finally:
        os.unlink(filtered_path)
    metrics_path = formal_dir / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
    merged_path = formal_dir / "merged_results.json"
    merged = json.loads(merged_path.read_text(encoding="utf-8")) if merged_path.is_file() else {}
    normalized: dict[str, int] = {}
    for result in merged.values():
        raw = str(result.get("status") or "UNKNOWN")
        if raw == "F2P_SUCCESS":
            category = "F2P_SUCCESS"
        elif raw == "BUGGY_PASS":
            category = "BUGGY_PASS"
        elif raw == "FIXED_FAIL":
            category = "FIXED_FAIL"
        elif any(marker in raw for marker in ("SETUP", "COLLECT", "SYNTAX", "TIMEOUT")):
            category = "ENV_OR_COLLECT"
        else:
            category = raw
        normalized[category] = normalized.get(category, 0) + 1
    summary = {
        "dataset_mode": args.dataset_mode,
        "coverage_metric_family": metrics.get("coverage_metric_family", ""),
        "returncode": returncode,
        "completed": len(completed),
        "total_instances": len(rows),
        "generated_instances": len(completed),
        "missing_generation": len(missing),
        "missing": missing,
        "metrics": metrics,
        "patch_coverage": {
            "enabled": bool(metrics.get("patch_cov_enabled")),
            "applicable": metrics.get("patch_cov_applicable", 0),
            "success": metrics.get("patch_cov_success", 0),
            "definition": metrics.get("patch_cov_definition", ""),
            "algorithm": metrics.get("patch_cov_algorithm", ""),
            "patch_cov_at_1": metrics.get("patch_cov_at_1", 0),
            "patch_cov_at_1_percent": metrics.get("patch_cov_at_1_percent", 0),
            "patch_cov_delta_at_1": metrics.get("patch_cov_delta_at_1", 0),
            "patch_cov_delta_at_1_percent": metrics.get("patch_cov_delta_at_1_percent", 0),
            "delta_mean_change_coverage": metrics.get(
                "delta_mean_change_coverage"
            ),
            "delta_mean_change_coverage_percent": metrics.get(
                "delta_mean_change_coverage_percent"
            ),
            "delta_c_valid": metrics.get("delta_c_valid", False),
            "delta_c_invalid_reason": metrics.get("delta_c_invalid_reason", ""),
            "delta_c_numerator": metrics.get("delta_c_numerator", 0),
            "delta_c_denominator": metrics.get("delta_c_denominator", 0),
            "delta_c_eligible_ids": metrics.get("delta_c_eligible_ids", []),
            "delta_c_excluded_no_executable_ids": metrics.get(
                "delta_c_excluded_no_executable_ids", []
            ),
            "delta_c_excluded_gold_unavailable": metrics.get(
                "delta_c_excluded_gold_unavailable", {}
            ),
            "delta_c_zeroed_model_instances": metrics.get(
                "delta_c_zeroed_model_instances", {}
            ),
            "delta_c_invalid_instances": metrics.get(
                "delta_c_invalid_instances", {}
            ),
            "delta_c_definition": metrics.get("delta_c_definition", ""),
            "delta_c_denominator_policy": metrics.get(
                "delta_c_denominator_policy", ""
            ),
            "delta_c_coverage_instrumentation": metrics.get(
                "delta_c_coverage_instrumentation", ""
            ),
            "delta_c_coverage_instrumentation_sha256": metrics.get(
                "delta_c_coverage_instrumentation_sha256", ""
            ),
            "delta_c_prediction_scope": metrics.get(
                "delta_c_prediction_scope", ""
            ),
            "delta_c_gold_denominator_source": metrics.get(
                "delta_c_gold_denominator_source", ""
            ),
            "median": metrics.get("patch_cov_median", 0),
            "median_percent": metrics.get("patch_cov_median_percent", 0),
            "delta_median": metrics.get("patch_cov_delta_median", 0),
            "delta_median_percent": metrics.get("patch_cov_delta_median_percent", 0),
            "nonzero_instances": metrics.get("patch_cov_nonzero_instances", 0),
            "nonzero_rate": metrics.get("patch_cov_nonzero_rate", 0),
            "target_lines": metrics.get("patch_cov_target_lines", 0),
            "covered_lines": metrics.get("patch_cov_covered_lines", 0),
            "buggy_target_lines": metrics.get("patch_cov_buggy_target_lines", 0),
            "buggy_covered_lines": metrics.get("patch_cov_buggy_covered_lines", 0),
            "fixed_target_lines": metrics.get("patch_cov_fixed_target_lines", 0),
            "fixed_covered_lines": metrics.get("patch_cov_fixed_covered_lines", 0),
            "line_coverage": metrics.get("patch_line_coverage", 0),
            "line_coverage_percent": metrics.get("patch_line_coverage_percent", 0),
            "by_status": metrics.get("patch_cov_by_status", {}),
        },
        "tdd_coverage": {
            "enabled": bool(
                args.dataset_mode == "tdd" and metrics.get("coverage_enabled")
            ),
            "definition": metrics.get("tdd_definition", ""),
            "algorithm": metrics.get("tdd_algorithm", ""),
            "final_score": metrics.get("tdd_score"),
            "final_score_percent": metrics.get("tdd_score_percent"),
            "valid": metrics.get("tdd_score_valid", False),
            "numerator": metrics.get("tdd_score_numerator"),
            "denominator": metrics.get("tdd_score_denominator"),
            "resolved_instances": metrics.get("tdd_resolved_instances"),
            "raw_coverage_macro": metrics.get("tdd_raw_coverage_macro"),
            "total_changed": metrics.get("tdd_total_changed"),
            "total_missed": metrics.get("tdd_total_missed"),
            "zeroed_instances": metrics.get("tdd_zeroed_instances", {}),
            "by_status": metrics.get("tdd_coverage_by_status", {}),
        },
        "formal_categories": normalized,
        "log": str(log_path),
        "eval_clone_root": str(eval_clone_root),
        "eval_worktree_root": str(eval_worktree_root) if args.eval_worktree_root else "",
        "worktree_mode": (
            "generated_instance_worktree"
            if args.use_generated_worktrees
            else "isolated_eval_worktree"
            if args.eval_worktree_root
            else "isolated_eval_clone"
        ),
        "keep_eval_worktrees": args.keep_eval_worktrees,
        "runtime_contract": runtime_contract,
    }
    summary_path = Path(args.summary_path).resolve() if args.summary_path else outputs / "formal_eval_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())

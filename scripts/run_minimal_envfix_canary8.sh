#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_PARENT=$(cd "$PROJECT_ROOT/.." && pwd)
PYTHON_BIN=/root/conda/ENTER/envs/icore/bin/python
SOURCE_RUN=$PROJECT_ROOT/results/runs/p0_simple_llm_selector_full276_20260717
RUN_ID=${RUN_ID:-p0_minimal_envfix_canary8_$(date +%Y%m%d_%H%M%S)}
RUN_ROOT=${RUN_ROOT:-$PROJECT_ROOT/results/runs/$RUN_ID}
RAW_ROOT=$RUN_ROOT/generation_raw
GENERATION_VIEW=$RUN_ROOT/generation_canary8
EVALUATION_DIR=$RUN_ROOT/evaluation/formal_f2p_canary8
MERGED_DIR=$RUN_ROOT/evaluation/merged276
LOG_DIR=$RUN_ROOT/logs
TMP_ROOT=$RUN_ROOT/tmp

INSTANCES=$PROJECT_ROOT/data/issues/swt276_issues.json
GOLD_DATASET=$SOURCE_RUN/dataset/swt276_with_gold.json
SUBSET_DATASET=$RUN_ROOT/dataset/canary8_with_gold.json
CODE_RETRIEVAL=$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json
TEST_RETRIEVAL=$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json
REPO_ROOT=/root/Baxxhy/BugReproduce/swe_repos
BEHAVIOR_CACHE=$SOURCE_RUN/issue_rewrite

IDS=(
  astropy__astropy-12907
  astropy__astropy-14182
  astropy__astropy-14995
  astropy__astropy-6938
  pylint-dev__pylint-7114
  pylint-dev__pylint-7993
  pylint-dev__pylint-5859
  pylint-dev__pylint-7080
)

REGENERATE_IDS=(
  astropy__astropy-6938
  pylint-dev__pylint-5859
  pylint-dev__pylint-7080
)

mkdir -p "$RAW_ROOT" "$GENERATION_VIEW" "$EVALUATION_DIR" "$MERGED_DIR" "$LOG_DIR" "$TMP_ROOT" "$RUN_ROOT/dataset"
exec > >(tee -a "$LOG_DIR/pipeline.log") 2>&1

export PYTHONPATH=$PACKAGE_PARENT
export TMPDIR=$TMP_ROOT
export BRT4_BEHAVIOR_CACHE_DIR=$BEHAVIOR_CACHE
export PYTHONWARNINGS=ignore::SyntaxWarning

"$PYTHON_BIN" - "$RUN_ROOT" "$SOURCE_RUN" "$GOLD_DATASET" "$SUBSET_DATASET" "${IDS[@]}" <<'PY'
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

run_root = Path(sys.argv[1])
source_run = Path(sys.argv[2])
gold_path = Path(sys.argv[3])
subset_path = Path(sys.argv[4])
ids = sys.argv[5:]
rows = json.loads(gold_path.read_text(encoding="utf-8"))
by_id = {str(row.get("instance_id")): row for row in rows}
missing = [instance_id for instance_id in ids if instance_id not in by_id]
if missing:
    raise SystemExit(f"canary IDs absent from gold dataset: {missing}")
subset_path.write_text(
    json.dumps([by_id[instance_id] for instance_id in ids], ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
git_state = subprocess.run(
    ["git", "status", "--short"],
    cwd=str(source_run.parents[2]),
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
manifest = {
    "run_id": run_root.name,
    "started_at": datetime.now().astimezone().isoformat(),
    "status": "running",
    "source_run": str(source_run),
    "source_f2p": {"success": 131, "total": 276, "percent": 47.4638},
    "canary_instances": ids,
    "regenerated_instances": [
        "astropy__astropy-6938",
        "pylint-dev__pylint-5859",
        "pylint-dev__pylint-7080",
    ],
    "reused_generation_instances": [
        instance_id
        for instance_id in ids
        if instance_id
        not in {
            "astropy__astropy-6938",
            "pylint-dev__pylint-5859",
            "pylint-dev__pylint-7080",
        }
    ],
    "framework_python": "/root/conda/ENTER/envs/icore/bin/python",
    "target_runtime_policy": "configured iCoRe dependency template plus fresh per-instance clone",
    "behavior_cache": str(source_run / "issue_rewrite"),
    "formal_metric": "F2P@1 only",
    "patch_coverage": False,
    "formal_canary_denominator": 8,
    "reported_merged_denominator": 276,
    "merge_policy": "replace exactly the eight canary rows in the source 276-row formal result",
    "git_status_short": git_state.stdout.splitlines(),
}
(run_root / "run_manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

echo "__BRT_CANARY8_STAGE__ generation_start $(date --iso-8601=seconds)"
pids=()
for instance_id in "${REGENERATE_IDS[@]}"; do
  instance_root=$RAW_ROOT/$instance_id
  mkdir -p "$instance_root"
  (
    "$PYTHON_BIN" -m brt6.pipeline.run \
      --instances_path "$INSTANCES" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --repo_root_base "$REPO_ROOT" \
      --output_dir "$instance_root" \
      --instance_id "$instance_id" \
      --model deepseek-v3 \
      --max_workers 1 \
      --max_feedback_rounds 3 \
      --max_env_rounds 2 \
      --max_brt_rounds 3 \
      --validation_mode buggy_only \
      --timeout 1800 \
      --temperature 0.1 \
      --max_tokens 4096
  ) >"$LOG_DIR/generation_$instance_id.log" 2>&1 &
  pids+=("$!")
done

generation_process_failures=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[$index]}"; then
    echo "${REGENERATE_IDS[$index]} generation_process_failed"
    generation_process_failures=$((generation_process_failures + 1))
  fi
done

generated=0
for instance_id in "${REGENERATE_IDS[@]}"; do
  generated_instance=$RAW_ROOT/$instance_id/$instance_id
  if [[ -f "$generated_instance/final_test.py" ]]; then
    ln -sfn "$generated_instance" "$GENERATION_VIEW/$instance_id"
    generated=$((generated + 1))
    echo "__BRT_CANARY8_GENERATION__ $instance_id generated"
  else
    echo "__BRT_CANARY8_GENERATION__ $instance_id MISSING_GENERATION"
  fi
done

for instance_id in "${IDS[@]}"; do
  if [[ -L "$GENERATION_VIEW/$instance_id" ]]; then
    continue
  fi
  source_instance=$SOURCE_RUN/generation/$instance_id
  if [[ -f "$source_instance/final_test.py" ]]; then
    ln -sfn "$source_instance" "$GENERATION_VIEW/$instance_id"
  fi
done

available=0
for instance_id in "${IDS[@]}"; do
  if [[ -f "$GENERATION_VIEW/$instance_id/final_test.py" ]]; then
    available=$((available + 1))
  fi
done
echo "__BRT_CANARY8_STAGE__ generation_end regenerated=$generated/3 available=$available/8 process_failures=$generation_process_failures $(date --iso-8601=seconds)"

echo "__BRT_CANARY8_STAGE__ formal_f2p_start $(date --iso-8601=seconds)"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py" \
  --outputs_dir "$GENERATION_VIEW" \
  --dataset_file "$SUBSET_DATASET" \
  --repo_root_base "$REPO_ROOT" \
  --max_workers 4 \
  --timeout 1800 \
  --evaluation_dir "$EVALUATION_DIR" \
  --log_path "$LOG_DIR/formal_eval_canary8.log" \
  --summary_path "$RUN_ROOT/evaluation/formal_eval_canary8_summary.json" \
  --compute_patch_coverage false
evaluation_rc=$?
echo "__BRT_CANARY8_STAGE__ formal_f2p_end rc=$evaluation_rc $(date --iso-8601=seconds)"

"$PYTHON_BIN" - "$RUN_ROOT" "$SOURCE_RUN" "$EVALUATION_DIR" "$MERGED_DIR" "$evaluation_rc" "${IDS[@]}" <<'PY'
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

run_root = Path(sys.argv[1])
source_run = Path(sys.argv[2])
evaluation_dir = Path(sys.argv[3])
merged_dir = Path(sys.argv[4])
evaluation_rc = int(sys.argv[5])
ids = sys.argv[6:]

source_eval = source_run / "evaluation" / "formal_f2p"
source_results = json.loads((source_eval / "merged_results.json").read_text(encoding="utf-8"))
canary_path = evaluation_dir / "merged_results.json"
canary_results = json.loads(canary_path.read_text(encoding="utf-8")) if canary_path.is_file() else {}
if len(source_results) != 276:
    raise SystemExit(f"source result denominator is {len(source_results)}, expected 276")

combined = dict(source_results)
overrides = {}
for instance_id in ids:
    old = source_results.get(instance_id, {})
    new = canary_results.get(
        instance_id,
        {
            "instance_id": instance_id,
            "repo": old.get("repo", "UNKNOWN"),
            "status": "MISSING_GENERATION",
            "success": False,
            "error": "canary evaluation did not produce a row",
        },
    )
    combined[instance_id] = new
    overrides[instance_id] = {
        "old_status": old.get("status", "UNKNOWN"),
        "old_success": bool(old.get("success")),
        "new_status": new.get("status", "UNKNOWN"),
        "new_success": bool(new.get("success")),
        "env_name": new.get("env_name", ""),
        "template_env_name": new.get("template_env_name", ""),
        "env_error_category": new.get("env_error_category", ""),
    }

if len(combined) != 276:
    raise SystemExit(f"merged result denominator is {len(combined)}, expected 276")

status_counts = Counter(str(result.get("status") or "UNKNOWN") for result in combined.values())
success = sum(1 for result in combined.values() if result.get("status") == "F2P_SUCCESS")
by_repo = defaultdict(Counter)
for result in combined.values():
    by_repo[str(result.get("repo") or "UNKNOWN")][str(result.get("status") or "UNKNOWN")] += 1
canary_env_errors = {
    instance_id: str(result.get("env_error_category") or result.get("status") or "")
    for instance_id, result in canary_results.items()
    if result.get("env_error_category")
    or str(result.get("status") or "").startswith("ENV_")
    or str(result.get("status") or "") in {"INSTALL_FAILURE", "COMMAND_RESOLUTION_FAILURE", "SETUP_ERROR"}
}

metrics = {
    "total_instances": 276,
    "f2p_success": success,
    "f2p_fail": 276 - success,
    "f2p_at_1": success / 276,
    "f2p_at_1_percent": round(success / 276 * 100, 4),
    "by_status": dict(sorted(status_counts.items())),
    "by_repo": {repo: dict(sorted(counts.items())) for repo, counts in sorted(by_repo.items())},
    "patch_cov_enabled": False,
    "result_type": "eight_instance_override_on_full276",
    "source_run": str(source_run),
    "source_f2p_success": 131,
    "f2p_success_delta": success - 131,
    "overridden_instances": ids,
    "canary_rows_received": len(canary_results),
    "canary_environment_errors": canary_env_errors,
    "metrics_valid": evaluation_rc == 0 and len(canary_results) == 8 and not canary_env_errors,
    "denominator_policy": "all 276 dataset rows; missing generation counts as failure",
}
completion = {
    "finished_at": datetime.now().astimezone().isoformat(),
    "status": "complete" if metrics["metrics_valid"] else "complete_with_canary_errors",
    "evaluation_returncode": evaluation_rc,
    "canary_generated_tests": sum(
        (run_root / "generation_canary8" / instance_id / "final_test.py").is_file()
        for instance_id in ids
    ),
    "canary_evaluated_rows": len(canary_results),
    "formal_canary_statuses": dict(
        sorted(Counter(str(result.get("status") or "UNKNOWN") for result in canary_results.values()).items())
    ),
    "merged_metrics": metrics,
}

merged_dir.mkdir(parents=True, exist_ok=True)
(merged_dir / "merged_results.json").write_text(
    json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(merged_dir / "metrics.json").write_text(
    json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(merged_dir / "override_report.json").write_text(
    json.dumps(overrides, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(run_root / "completion.json").write_text(
    json.dumps(completion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(completion, ensure_ascii=False, indent=2))
PY

echo "__BRT_CANARY8_STAGE__ complete $(date --iso-8601=seconds)"
exit "$evaluation_rc"

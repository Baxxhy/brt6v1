#!/usr/bin/env bash

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_PARENT=$(cd "$PROJECT_ROOT/.." && pwd)
PYTHON_BIN=/root/conda/ENTER/envs/icore/bin/python
SOURCE_RUN=$PROJECT_ROOT/results/runs/p0_simple_llm_selector_full276_20260717
RECOVERY_RUN=${RECOVERY_RUN:-$PROJECT_ROOT/results/runs/p0_simple_llm_selector_env3_recovery_20260718}
RAW_ROOT=$RECOVERY_RUN/raw
RECOVERED_GENERATION=$RECOVERY_RUN/generation
MERGED_GENERATION=$RECOVERY_RUN/generation_merged276
EVALUATION_DIR=$RECOVERY_RUN/evaluation/formal_f2p_completed276
LOG_DIR=$RECOVERY_RUN/logs

INSTANCES=$PROJECT_ROOT/data/issues/swt276_issues.json
GOLD_DATASET=$SOURCE_RUN/dataset/swt276_with_gold.json
CODE_RETRIEVAL=$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json
TEST_RETRIEVAL=$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json
REPO_ROOT=${REPO_ROOT:-$PROJECT_ROOT/evaluation/vendor/swtbench_metadata}
BEHAVIOR_CACHE=$SOURCE_RUN/issue_rewrite

IDS=(
  astropy__astropy-6938
  pylint-dev__pylint-5859
  pylint-dev__pylint-7080
)

mkdir -p "$RAW_ROOT" "$RECOVERED_GENERATION" "$MERGED_GENERATION" "$EVALUATION_DIR" "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/recovery_pipeline.log") 2>&1

"$PYTHON_BIN" - "$RECOVERY_RUN" "$SOURCE_RUN" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

root = Path(sys.argv[1])
payload = {
    "run_id": "p0_simple_llm_selector_env3_recovery_20260718",
    "started_at": datetime.now().astimezone().isoformat(),
    "status": "running",
    "source_run": sys.argv[2],
    "source_f2p_success": 131,
    "dataset_size": 276,
    "recovery_instances": [
        "astropy__astropy-6938",
        "pylint-dev__pylint-5859",
        "pylint-dev__pylint-7080",
    ],
    "recovery_reason": "three MISSING_GENERATION rows with recoverable failed Conda lifecycle state",
    "method_change": "none",
    "environment_change": "exercise existing failed-environment cleanup and deterministic iCoRe recreation branch",
    "framework_python": "/root/conda/ENTER/envs/icore/bin/python",
    "behavior_cache": str(Path(sys.argv[2]) / "issue_rewrite"),
    "surrogate_patch_calls_expected": 0,
    "merged_generation_policy": "273 original instance directories plus three recovered instance directories via symlinks",
    "formal_metric": "F2P@1",
    "formal_denominator": 276,
    "patch_coverage": False,
    "launcher": "$PROJECT_ROOT/scripts/rerun_missing_env3_and_f2p.sh",
}
(root / "run_manifest.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

echo "__BRT_RECOVERY_STAGE__ generation_start $(date --iso-8601=seconds)"
pids=()
for instance_id in "${IDS[@]}"; do
  instance_root=$RAW_ROOT/$instance_id
  mkdir -p "$instance_root"
  (
    export PYTHONPATH=$PACKAGE_PARENT
    export BRT4_BEHAVIOR_CACHE_DIR=$BEHAVIOR_CACHE
    export PYTHONWARNINGS=ignore::SyntaxWarning
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
  ) >"$LOG_DIR/$instance_id.log" 2>&1 &
  pids+=("$!")
done

generation_rc=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[$index]}"; then
    echo "${IDS[$index]} process_failed"
    generation_rc=1
  fi
done

generated=0
for instance_id in "${IDS[@]}"; do
  recovered_instance=$RAW_ROOT/$instance_id/$instance_id
  if [[ -f "$recovered_instance/final_test.py" && -f "$recovered_instance/summary.json" ]]; then
    ln -sfn "$recovered_instance" "$RECOVERED_GENERATION/$instance_id"
    generated=$((generated + 1))
    echo "$instance_id recovered"
  else
    echo "$instance_id missing_final_test"
    generation_rc=1
  fi
done
echo "__BRT_RECOVERY_STAGE__ generation_end rc=$generation_rc generated=$generated/3 $(date --iso-8601=seconds)"

if [[ "$generation_rc" -ne 0 || "$generated" -ne 3 ]]; then
  "$PYTHON_BIN" - "$RECOVERY_RUN" "$generation_rc" "$generated" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

root = Path(sys.argv[1])
payload = {
    "finished_at": datetime.now().astimezone().isoformat(),
    "status": "generation_recovery_failed",
    "generation_returncode": int(sys.argv[2]),
    "recovered_tests": int(sys.argv[3]),
    "expected_recovered_tests": 3,
    "evaluation_started": False,
}
(root / "completion.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  exit 1
fi

for source_instance in "$SOURCE_RUN"/generation/*; do
  [[ -d "$source_instance" ]] || continue
  instance_id=$(basename "$source_instance")
  ln -sfn "$source_instance" "$MERGED_GENERATION/$instance_id"
done
for instance_id in "${IDS[@]}"; do
  ln -sfn "$RAW_ROOT/$instance_id/$instance_id" "$MERGED_GENERATION/$instance_id"
done

merged_count=$(find -L "$MERGED_GENERATION" -mindepth 2 -maxdepth 2 -name final_test.py -type f | wc -l)
echo "__BRT_RECOVERY_PROGRESS__ merged_tests=$merged_count/276"
if [[ "$merged_count" -ne 276 ]]; then
  echo "merged generation view is incomplete; formal evaluation will not start"
  exit 1
fi

echo "__BRT_RECOVERY_STAGE__ formal_f2p_start $(date --iso-8601=seconds)"
export PYTHONPATH=$PACKAGE_PARENT
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py" \
  --outputs_dir "$MERGED_GENERATION" \
  --dataset_file "$GOLD_DATASET" \
  --repo_root_base "$REPO_ROOT" \
  --max_workers 6 \
  --timeout 1800 \
  --evaluation_dir "$EVALUATION_DIR" \
  --log_path "$LOG_DIR/formal_eval.log" \
  --summary_path "$RECOVERY_RUN/evaluation/formal_eval_summary.json" \
  --compute_patch_coverage false
evaluation_rc=$?
echo "__BRT_RECOVERY_STAGE__ formal_f2p_end rc=$evaluation_rc $(date --iso-8601=seconds)"

"$PYTHON_BIN" - "$RECOVERY_RUN" "$generation_rc" "$evaluation_rc" "$merged_count" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

root = Path(sys.argv[1])
metrics_path = root / "evaluation" / "formal_f2p_completed276" / "metrics.json"
metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else {}
payload = {
    "finished_at": datetime.now().astimezone().isoformat(),
    "status": "complete" if int(sys.argv[3]) == 0 else "evaluation_failed",
    "generation_returncode": int(sys.argv[2]),
    "evaluation_returncode": int(sys.argv[3]),
    "recovered_tests": 3,
    "merged_tests": int(sys.argv[4]),
    "formal_total_instances": metrics.get("total_instances"),
    "f2p_success": metrics.get("f2p_success"),
    "f2p_fail": metrics.get("f2p_fail"),
    "f2p_at_1_percent": metrics.get("f2p_at_1_percent"),
    "by_status": metrics.get("by_status", {}),
    "denominator_valid": metrics.get("total_instances") == 276,
    "patch_cov_enabled": metrics.get("patch_cov_enabled"),
    "original_f2p_success": 131,
    "f2p_success_delta": (
        metrics.get("f2p_success") - 131
        if isinstance(metrics.get("f2p_success"), int)
        else None
    ),
}
(root / "completion.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

echo "__BRT_RECOVERY_STAGE__ complete $(date --iso-8601=seconds)"
exit "$evaluation_rc"

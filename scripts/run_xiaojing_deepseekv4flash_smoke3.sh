#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$ROOT/.." && pwd)
PYTHON=${PYTHON_BIN:-/root/miniconda3/envs/icore/bin/python}
SWT_PYTHON=${SWTBENCH_PYTHON:-/root/miniconda3/envs/swtbench/bin/python}
DATASET=$ROOT/data/issues/swt276_issues.json
OFFICIAL_DATASET=$ROOT/data/official/swt276_official_eval.json
CODE_RETRIEVAL=$ROOT/retrieval_results/code/code_retrieval_results_gpt.json
TEST_RETRIEVAL=$ROOT/retrieval_results/test/icore/gpt/related_tests.json
POOL=${BRT_API_POOL_FILE:-$ROOT/.secrets/api_pool.json}
RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_DIR=${RUN_DIR:-$ROOT/results/runs/xiaojing_deepseekv4flash_smoke3_${RUN_TIMESTAMP}}
IDS=$RUN_DIR/instance_ids.txt
DESIGN1=$RUN_DIR/design1
DESIGN2=$RUN_DIR/design2
SELECTION=$RUN_DIR/strict_icore
EVALUATION=$RUN_DIR/evaluation/official_f2p
OFFICIAL_SUBSET=$RUN_DIR/official_subset3.json

mkdir -p "$RUN_DIR"/{logs,tmp,cache} "$DESIGN1" "$DESIGN2" "$EVALUATION"
cat > "$IDS" <<'EOF'
astropy__astropy-12907
django__django-11049
sympy__sympy-20639
EOF

"$PYTHON" - "$OFFICIAL_DATASET" "$IDS" "$OFFICIAL_SUBSET" <<'PY'
import json
from pathlib import Path
import sys

source, ids_file, output = map(Path, sys.argv[1:])
rows = json.loads(source.read_text(encoding="utf-8"))
by_id = {row["instance_id"]: row for row in rows}
ids = [line.strip() for line in ids_file.read_text().splitlines() if line.strip()]
selected = [by_id[instance_id] for instance_id in ids]
output.write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
PY

export PYTHONPATH=$PACKAGE_ROOT
export BRT_API_POOL_FILE=$POOL
export BRT_ALLOWED_API_HOST=api.open.xiaojingai.com
export BRT_MODEL_ID=deepseek-v4-flash
export BRT_DISABLE_THINKING=1
export BRT_LLM_STREAM=1
export BRT3_LLM_REQUEST_TIMEOUT=600
export BRT3_LLM_MAX_ATTEMPTS=2
export BRT_LLM_TRUNCATION_MAX_TOKENS=8192
export BRT_CONDA_PROBE_TIMEOUT_SECONDS=${BRT_CONDA_PROBE_TIMEOUT_SECONDS:-120}
export BRT_REQUIRE_OFFICIAL_DOCKER=1
export BRT_ALLOW_DIRTY_WORKTREE=1
export BRT_OFFICIAL_DOCKER_STARTUP_TIMEOUT=7200
export DOCKER_HOST=${DOCKER_HOST:-unix:///run/mutate-docker.sock}
export TMPDIR=$RUN_DIR/tmp
export TEMP=$TMPDIR
export TMP=$TMPDIR
export XDG_CACHE_HOME=$RUN_DIR/cache

progress() {
  echo "[$(date '+%F %T')] $*" | tee -a "$RUN_DIR/progress.log"
}

cat > "$RUN_DIR/run_config.json" <<EOF
{
  "instances": 3,
  "model": "deepseek-v4-flash",
  "api_host": "api.open.xiaojingai.com",
  "reasoning": "disabled",
  "temperature": 0.1,
  "max_output_tokens": 4096,
  "truncation_retry_max_tokens": 8192,
  "workers": 3,
  "api_timeout_seconds": 1200,
  "docker_timeout_seconds": 7200,
  "fixed_side_used_before_frozen_selection": false
}
EOF

progress "stage 1/4: reproduction target recovery"
for attempt in 1 2 3; do
  progress "stage 1 pass $attempt/3"
  resume_args=()
  if [[ "$attempt" -gt 1 ]]; then
    resume_args+=(--resume)
  fi
  "$PYTHON" -u -m brt6.pipeline.run_issue_rewrite \
    --instances_path "$DATASET" \
    --code_retrieval_path "$CODE_RETRIEVAL" \
    --test_retrieval_path "$TEST_RETRIEVAL" \
    --output_dir "$DESIGN1" \
    --instance_ids_file "$IDS" \
    --model deepseek-v4-flash --llm-provider deepseek \
    --temperature 0.1 --max_tokens 4096 --max_workers 3 \
    "${resume_args[@]}" \
    >> "$RUN_DIR/logs/01_design1.log" 2>&1

  "$PYTHON" "$ROOT/scripts/list_missing_behavior_targets.py" \
    --instances-path "$DATASET" --target-root "$DESIGN1" \
    --output "$RUN_DIR/design1_missing.txt" --report "$RUN_DIR/design1_completion.json" \
    --restrict-file "$IDS"
  if [[ ! -s "$RUN_DIR/design1_missing.txt" ]]; then
    break
  fi
done
if [[ -s "$RUN_DIR/design1_missing.txt" ]]; then
  progress "stopped: Design 1 is incomplete"
  exit 2
fi

progress "stage 2/4: individual test adaptation"
BRT4_BEHAVIOR_CACHE_DIR="$DESIGN1" \
"$PYTHON" -u -m brt6.pipeline.run \
  --instances_path "$DATASET" \
  --code_retrieval_path "$CODE_RETRIEVAL" \
  --test_retrieval_path "$TEST_RETRIEVAL" \
  --repo_root_base "${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}" \
  --output_dir "$DESIGN2" --instance_ids_file "$IDS" \
  --model deepseek-v4-flash --llm-provider deepseek \
  --temperature 0.1 --max_tokens 4096 --max_workers 3 \
  --max_semantic_rounds 5 --timeout 7200 \
  --validation_mode buggy_only --alignment-verifier strict \
  --dataset_mode swt --runtime_backend official_docker \
  --official_harness_python "$SWT_PYTHON" \
  --swtbench_root "$ROOT/evaluation/vendor/swtbench" \
  --enable_behavior_target true --enable_seed_mutation true \
  --enable_specialized_feedback true --enable_environment_feedback true \
  --enable_trigger_feedback true --enable_assertion_feedback true \
  --enable_semantic_delta true \
  > "$RUN_DIR/logs/02_design2.log" 2>&1

progress "stage 3/4: frozen strict-verifier filtering and iCoRe ranking"
"$PYTHON" -u "$ROOT/scripts/run_generation_strict_icore.py" \
  --generation "$DESIGN2" --output "$SELECTION" \
  --issues "$DATASET" --instance-ids-file "$IDS" --workers 3 \
  > "$RUN_DIR/logs/03_selection.log" 2>&1

progress "stage 4/4: official F2P evaluation"
"$SWT_PYTHON" "$ROOT/scripts/run_official_eval_after_generation.py" \
  --dataset swt --outputs-dir "$SELECTION/generation" \
  --dataset-file "$OFFICIAL_SUBSET" \
  --official-dataset-name "$OFFICIAL_SUBSET" \
  --evaluation-dir "$EVALUATION" \
  --max-workers 3 --timeout 7200 --run-id "$(basename "$RUN_DIR")" \
  --model-name xiaojing-deepseek-v4-flash-smoke3 \
  --compute-coverage false --official-python "$SWT_PYTHON" \
  --swtbench-root "$ROOT/evaluation/vendor/swtbench" \
  > "$RUN_DIR/logs/04_official_eval.log" 2>&1

progress "complete"
echo "$RUN_DIR"

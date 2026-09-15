#!/usr/bin/env bash
set -euo pipefail

ROOT=/root/Baxxhy/BugReproduce/brt6
PACKAGE_ROOT=/root/Baxxhy/BugReproduce
PYTHON=/root/miniconda3/envs/icore/bin/python
SWT_PYTHON=/root/miniconda3/envs/swtbench/bin/python
GENERATION_DATASET=$ROOT/data/issues/swt276_issues.json
OFFICIAL_DATASET=$ROOT/data/official/swt276_official_eval.json
CODE_RETRIEVAL=$ROOT/retrieval_results/code/code_retrieval_results_gpt.json
TEST_RETRIEVAL=$ROOT/retrieval_results/test/icore/gpt/related_tests.json
POOL=${BRT_API_POOL_FILE:-$ROOT/.secrets/api_pool_xiaojing_deepseekv4flash.json}

RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_DIR=${RUN_DIR:-$ROOT/results/runs/trait_xiaojing_deepseekv4flash_full_${RUN_TIMESTAMP}}
RUN_ID=$(basename "$RUN_DIR")
DESIGN1=$RUN_DIR/design1_reproduction_target
DESIGN2=$RUN_DIR/design2_individual_adaptation
SELECTION=$RUN_DIR/strict_icore
EVALUATION=$RUN_DIR/evaluation/official_f2p
MISSING_TARGETS=$RUN_DIR/design1_missing.txt

mkdir -p "$RUN_DIR"/{logs,tmp,cache,evaluation} "$DESIGN1" "$DESIGN2" "$SELECTION"

ACTIVE_STAGE_PID=""

cleanup_stage() {
  local status=$?
  trap - EXIT INT TERM HUP
  if [[ -n "$ACTIVE_STAGE_PID" ]] && kill -0 "$ACTIVE_STAGE_PID" 2>/dev/null; then
    kill -TERM -- "-$ACTIVE_STAGE_PID" 2>/dev/null || true
    for _ in {1..50}; do
      kill -0 "$ACTIVE_STAGE_PID" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$ACTIVE_STAGE_PID" 2>/dev/null; then
      kill -KILL -- "-$ACTIVE_STAGE_PID" 2>/dev/null || true
    fi
    wait "$ACTIVE_STAGE_PID" 2>/dev/null || true
  fi
  exit "$status"
}

run_managed() {
  local status
  setsid "$@" &
  ACTIVE_STAGE_PID=$!
  set +e
  wait "$ACTIVE_STAGE_PID"
  status=$?
  set -e
  ACTIVE_STAGE_PID=""
  return "$status"
}

trap cleanup_stage EXIT INT TERM HUP

export PYTHONPATH=$PACKAGE_ROOT
export BRT_API_POOL_FILE=$POOL
export BRT_ALLOWED_API_HOST=api.open.xiaojingai.com
export BRT_MODEL_ID=deepseek-v4-flash
export BRT_DISABLE_THINKING=1
export BRT_LLM_STREAM=1
export BRT3_LLM_REQUEST_TIMEOUT=1200
export BRT3_LLM_MAX_ATTEMPTS=3
export BRT_LLM_WAIT_FOREVER=1
export BRT_LLM_TRUNCATION_MAX_TOKENS=8192
export BRT_CSU_MAX_INFLIGHT=20
export BRT_REQUIRE_OFFICIAL_DOCKER=1
export BRT_ALLOW_DIRTY_WORKTREE=1
export BRT_OFFICIAL_DOCKER_STARTUP_TIMEOUT=7200
export DOCKER_HOST=unix:///run/mutate-docker.sock
export TMPDIR=$RUN_DIR/tmp
export TEMP=$TMPDIR
export TMP=$TMPDIR
export XDG_CACHE_HOME=$RUN_DIR/cache

progress() {
  echo "[$(date '+%F %T')] $*" | tee -a "$RUN_DIR/progress.log"
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "required file is missing: $1" >&2
    exit 2
  fi
}

for required in \
  "$PYTHON" \
  "$SWT_PYTHON" \
  "$GENERATION_DATASET" \
  "$OFFICIAL_DATASET" \
  "$CODE_RETRIEVAL" \
  "$TEST_RETRIEVAL" \
  "$POOL"; do
  require_file "$required"
done

MAX_ZOMBIES=${BRT_MAX_ZOMBIES_BEFORE_RUN:-100}
ZOMBIE_COUNT=$(ps -eo stat= | awk '$1 ~ /^Z/ {count++} END {print count+0}')
if (( ZOMBIE_COUNT > MAX_ZOMBIES )); then
  echo "refusing to start: found $ZOMBIE_COUNT zombie processes (limit $MAX_ZOMBIES); restart the outer server/container first" >&2
  exit 2
fi

RUNNING_EVAL_CONTAINERS=$(docker ps -q --filter 'name=exec.eval.' | wc -l)
if (( RUNNING_EVAL_CONTAINERS > 0 )); then
  echo "refusing to start: found $RUNNING_EVAL_CONTAINERS running exec.eval containers from another or interrupted run" >&2
  exit 2
fi

cat > "$RUN_DIR/run_config.json" <<EOF
{
  "method": "TRAIT",
  "instances": 276,
  "design1": "single reproduction target recovery",
  "design2": "three independently adapted retrieved tests with semantic-delta feedback",
  "candidate_acceptance": "frozen strict semantic verifier verdict",
  "post_selection": "local deterministic iCoRe ranking",
  "additional_issue2test_judge": false,
  "model": "deepseek-v4-flash",
  "provider": "deepseek",
  "api_host": "api.open.xiaojingai.com",
  "reasoning": "disabled",
  "stream": true,
  "prompt_language": "English",
  "prompt_compaction": true,
  "temperature": 0.1,
  "initial_max_output_tokens": 4096,
  "truncation_retry_max_tokens": 8192,
  "semantic_rounds": 5,
  "workers": 20,
  "maximum_concurrent_model_requests": 20,
  "api_timeout_seconds": 1200,
  "docker_timeout_seconds": 7200,
  "compute_coverage": false,
  "fixed_side_used_during_generation_or_selection": false,
  "generation_dataset": "$GENERATION_DATASET",
  "official_evaluation_dataset": "$OFFICIAL_DATASET"
}
EOF

if [[ ! -f "$RUN_DIR/design1.done" ]]; then
  progress "stage 1/4: reproduction target recovery"
  for attempt in 1 2 3; do
    progress "stage 1 pass $attempt/3"
    run_managed "$PYTHON" -u -m brt6.pipeline.run_issue_rewrite \
      --instances_path "$GENERATION_DATASET" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --output_dir "$DESIGN1" \
      --model deepseek-v4-flash --llm-provider deepseek \
      --temperature 0.1 --max_tokens 4096 --max_workers 20 --resume \
      >> "$RUN_DIR/logs/01_design1.log" 2>&1

    "$PYTHON" "$ROOT/scripts/list_missing_behavior_targets.py" \
      --instances-path "$GENERATION_DATASET" \
      --target-root "$DESIGN1" \
      --output "$MISSING_TARGETS" \
      --report "$RUN_DIR/design1_completion.json"
    if [[ ! -s "$MISSING_TARGETS" ]]; then
      break
    fi
  done
  if [[ -s "$MISSING_TARGETS" ]]; then
    progress "stage 1 incomplete after three passes; see $MISSING_TARGETS"
    exit 2
  fi
  touch "$RUN_DIR/design1.done"
else
  progress "stage 1/4: already complete"
fi

if [[ ! -f "$RUN_DIR/design2.done" ]]; then
  progress "stage 2/4: individual test adaptation"
  run_managed env BRT4_BEHAVIOR_CACHE_DIR="$DESIGN1" \
  "$PYTHON" -u -m brt6.pipeline.run \
    --instances_path "$GENERATION_DATASET" \
    --code_retrieval_path "$CODE_RETRIEVAL" \
    --test_retrieval_path "$TEST_RETRIEVAL" \
    --repo_root_base /root/Baxxhy/BugReproduce/swe_repos \
    --output_dir "$DESIGN2" \
    --model deepseek-v4-flash --llm-provider deepseek \
    --temperature 0.1 --max_tokens 4096 --max_workers 20 \
    --max_semantic_rounds 5 --timeout 7200 --resume \
    --validation_mode buggy_only --alignment-verifier strict \
    --dataset_mode swt --runtime_backend official_docker \
    --official_harness_python "$SWT_PYTHON" \
    --swtbench_root "$ROOT/evaluation/vendor/swtbench" \
    --enable_behavior_target true --enable_seed_mutation true \
    --enable_specialized_feedback true --enable_environment_feedback true \
    --enable_trigger_feedback true --enable_assertion_feedback true \
    --enable_semantic_delta true \
    > "$RUN_DIR/logs/02_design2.log" 2>&1
  touch "$RUN_DIR/design2.done"
else
  progress "stage 2/4: already complete"
fi

if [[ ! -f "$RUN_DIR/selection.done" ]]; then
  progress "stage 3/4: strict-verifier filtering and iCoRe ranking"
  run_managed "$PYTHON" -u "$ROOT/scripts/run_generation_strict_icore.py" \
    --generation "$DESIGN2" \
    --output "$SELECTION" \
    --issues "$GENERATION_DATASET" \
    --workers 20 \
    > "$RUN_DIR/logs/03_strict_icore.log" 2>&1
  touch "$RUN_DIR/selection.done"
else
  progress "stage 3/4: already complete"
fi

if [[ ! -f "$RUN_DIR/evaluation.done" ]]; then
  progress "stage 4/4: official SWT-Docker F2P evaluation"
  mkdir -p "$EVALUATION"
  run_managed "$SWT_PYTHON" "$ROOT/scripts/run_official_eval_after_generation.py" \
    --dataset swt \
    --outputs-dir "$SELECTION/generation" \
    --dataset-file "$OFFICIAL_DATASET" \
    --official-dataset-name "$OFFICIAL_DATASET" \
    --evaluation-dir "$EVALUATION" \
    --max-workers 20 --timeout 7200 \
    --run-id "$RUN_ID" \
    --model-name trait-xiaojing-deepseek-v4-flash \
    --compute-coverage false \
    --official-python "$SWT_PYTHON" \
    --swtbench-root "$ROOT/evaluation/vendor/swtbench" \
    > "$RUN_DIR/logs/04_official_evaluation.log" 2>&1
  touch "$RUN_DIR/evaluation.done"
else
  progress "stage 4/4: already complete"
fi

touch "$RUN_DIR/completed.done"
progress "complete: $RUN_DIR"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$ROOT/.." && pwd)
PYTHON=${PYTHON_BIN:-/root/miniconda3/envs/icore/bin/python}
SWT_PYTHON=${SWTBENCH_PYTHON:-/root/miniconda3/envs/swtbench/bin/python}
GENERATION_DATASET=$ROOT/data/issues/swt276_issues.json
OFFICIAL_DATASET=$ROOT/data/official/swt276_official_eval.json
CODE_RETRIEVAL=$ROOT/retrieval_results/code/code_retrieval_results_gpt.json
TEST_RETRIEVAL=$ROOT/retrieval_results/test/icore/gpt/related_tests.json
POOL=${BRT_API_POOL_FILE:-$ROOT/.secrets/api_pool.json}

RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_DIR=${RUN_DIR:-$ROOT/results/runs/trait_wo_c1_full_${RUN_TIMESTAMP}}
RUN_ID=$(basename "$RUN_DIR")
DESIGN1=$RUN_DIR/unused_design1
DESIGN2=$RUN_DIR/design2_individual_adaptation
SELECTION=$RUN_DIR/strict_icore
EVALUATION=$RUN_DIR/evaluation/official_f2p
MISSING_TARGETS=$RUN_DIR/design1_missing.txt

mkdir -p "$RUN_DIR"/{logs,tmp,cache,evaluation} "$DESIGN2" "$SELECTION"
exec 9>"$RUN_DIR/launcher.lock"
flock -n 9 || { echo "This experiment is already running" >&2; exit 1; }

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
  "$PYTHON" "$ROOT/scripts/summarize_api_cost.py" --run-dir "$RUN_DIR" \
    >> "$RUN_DIR/logs/api_cost.log" 2>&1 || true
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
export BRT_COST_DIR=$RUN_DIR
unset BRT4_BEHAVIOR_CACHE_DIR BRT_DISABLE_DIRECT_FALLBACK
export BRT_API_POOL_FILE=$POOL
export BRT_ALLOWED_API_HOST=api.open.xiaojingai.com
export BRT_MODEL_ID=deepseek-v4-flash
export BRT_DISABLE_THINKING=1
export BRT_LLM_STREAM=1
export BRT3_LLM_REQUEST_TIMEOUT=600
export BRT3_LLM_MAX_ATTEMPTS=2
export BRT_RETRY_TRANSIENT_API=1
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

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "required file is missing: $1" >&2
    exit 2
  fi
}

json_stage_complete() {
  local path=$1
  local expected=$2
  local mode=$3
  "$PYTHON" - "$path" "$expected" "$mode" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = int(sys.argv[2])
mode = sys.argv[3]
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(value, dict):
    raise SystemExit(1)
if mode == "generation":
    ok = (
        int(value.get("total") or -1) == expected
        and int(value.get("error") or 0) == 0
        and int(value.get("paused_api") or 0) == 0
        and len(value.get("results") or []) == expected
    )
elif mode == "selection":
    ok = int(value.get("instances") or -1) == expected
elif mode == "evaluation":
    ok = int(value.get("total_instances") or -1) == expected
else:
    ok = False
raise SystemExit(0 if ok else 1)
PY
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

REQUESTED_CONFIG=$RUN_DIR/run_config.requested.json
cat > "$REQUESTED_CONFIG" <<EOF
{
  "method": "TRAIT",
  "algorithm_revision": "lossless_issue_target_fallback_v1",
  "instances": 276,
  "design1": "disabled; raw issue and identical repository context retained",
  "behavior_target": false,
  "reference_run": "trait_xiaojing_deepseekv4flash_full_20260918_051829",
  "design2": "three independently adapted retrieved tests with semantic-delta feedback",
  "fallback": "not applicable: direct adaptation is the primary ablation route",
  "candidate_acceptance": "frozen strict semantic verifier verdict",
  "post_selection": "strict accepted first; otherwise rank all available candidates after exhaustion",
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
  "api_timeout_seconds_per_endpoint": 600,
  "transient_api_retry": "enabled; logical request duration is not bounded by two attempts",
  "docker_timeout_seconds": 7200,
  "compute_coverage": false,
  "fixed_side_used_during_generation_or_selection": false,
  "generation_dataset": "$GENERATION_DATASET",
  "official_evaluation_dataset": "$OFFICIAL_DATASET"
}
EOF

if [[ -f "$RUN_DIR/run_config.json" ]]; then
  if ! cmp -s "$RUN_DIR/run_config.json" "$REQUESTED_CONFIG"; then
    echo "refusing to mix a new configuration into existing run directory: $RUN_DIR" >&2
    echo "requested configuration was saved to: $REQUESTED_CONFIG" >&2
    exit 2
  fi
  rm -f "$REQUESTED_CONFIG"
else
  mv "$REQUESTED_CONFIG" "$RUN_DIR/run_config.json"
fi

# C1 is intentionally absent; no recovery/cache/model call at this stage.
touch "$RUN_DIR/design1.done"
if [[ -f "$RUN_DIR/design2.done" ]] && \
   ! json_stage_complete "$DESIGN2/summary.json" 276 generation; then
  progress "stage 2 marker is stale; rebuilding incomplete adaptation outputs"
  rm -f "$RUN_DIR/design2.done" "$RUN_DIR/selection.done" \
    "$RUN_DIR/evaluation.done" "$RUN_DIR/completed.done"
fi

if [[ -f "$RUN_DIR/selection.done" ]] && \
   ! json_stage_complete "$SELECTION/completed.json" 276 selection; then
  progress "stage 3 marker is stale; rebuilding deterministic selection"
  rm -f "$RUN_DIR/selection.done" "$RUN_DIR/evaluation.done" \
    "$RUN_DIR/completed.done"
fi

if [[ -f "$RUN_DIR/evaluation.done" ]] && \
   ! json_stage_complete "$EVALUATION/metrics.json" 276 evaluation; then
  progress "stage 4 marker is stale; rerunning incomplete official evaluation"
  rm -f "$RUN_DIR/evaluation.done" "$RUN_DIR/completed.done"
fi

# A missing or invalid upstream marker makes every downstream artifact stale.
# This matters after an interrupted resume: a previously completed selection
# must never be reused after generation has produced new candidates.
if [[ ! -f "$RUN_DIR/design1.done" ]]; then
  rm -f "$RUN_DIR/design2.done" "$RUN_DIR/selection.done" \
    "$RUN_DIR/evaluation.done" "$RUN_DIR/completed.done"
fi
if [[ ! -f "$RUN_DIR/design2.done" ]]; then
  rm -f "$RUN_DIR/selection.done" "$RUN_DIR/evaluation.done" \
    "$RUN_DIR/completed.done"
fi
if [[ ! -f "$RUN_DIR/selection.done" ]]; then
  rm -f "$RUN_DIR/evaluation.done" "$RUN_DIR/completed.done"
fi

progress "stage 1 skipped: C1 disabled; original issue and repository context retained"

# Smoke coverage is chosen by dataset order, never by official outcomes.
# Completed pilot instances are reused by the identical full-run resume journal.
if [[ ! -f "$RUN_DIR/pilot.done" && ! -f "$RUN_DIR/design2.done" ]]; then
  "$PYTHON" - "$GENERATION_DATASET" "$RUN_DIR/pilot_ids.txt" <<'PYCODE'
import json, sys
from pathlib import Path
raw = json.loads(Path(sys.argv[1]).read_text())
rows = list(raw.values()) if isinstance(raw, dict) else raw
Path(sys.argv[2]).write_text("\n".join(str(r["instance_id"]) for r in rows[:3]) + "\n")
PYCODE
  progress "pilot: first three dataset instances; no F2P threshold"
  run_managed env -u BRT4_BEHAVIOR_CACHE_DIR "$PYTHON" -u -m brt6.pipeline.run \
    --instances_path "$GENERATION_DATASET" --instance_ids_file "$RUN_DIR/pilot_ids.txt" \
    --code_retrieval_path "$CODE_RETRIEVAL" --test_retrieval_path "$TEST_RETRIEVAL" \
    --repo_root_base "${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}" --output_dir "$DESIGN2" \
    --model deepseek-v4-flash --llm-provider deepseek \
    --temperature 0.1 --max_tokens 4096 --max_workers 20 \
    --max_semantic_rounds 5 --timeout 7200 --resume \
    --validation_mode buggy_only --alignment-verifier strict \
    --dataset_mode swt --runtime_backend official_docker \
    --official_harness_python "$SWT_PYTHON" --swtbench_root "$ROOT/evaluation/vendor/swtbench" \
    --enable_behavior_target false --enable_seed_mutation true \
    --enable_specialized_feedback true --enable_environment_feedback true \
    --enable_trigger_feedback true --enable_assertion_feedback true --enable_semantic_delta true \
    >> "$RUN_DIR/logs/02_design2.log" 2>&1
  "$PYTHON" - "$DESIGN2" "$RUN_DIR/pilot_ids.txt" <<'PYCODE'
import json, sys
from pathlib import Path
root=Path(sys.argv[1])
if (root/'api_paused.json').exists():
    raise SystemExit('pilot paused on API; resume after service recovers')
for iid in Path(sys.argv[2]).read_text().splitlines():
    d=json.loads((root/iid/'summary.json').read_text())
    assert d['behavior_target_enabled'] is False, iid
    assert d['ablation_id'] == 'wo_behavior_target', iid
    assert d['strict_verifier_enabled'] is True, iid
    assert d['status'] not in {'ERROR','PAUSED_API','MISSING'}, iid
    assert not (root/iid/'behavior_target.json').exists(), iid
print('pilot configuration verified; no success-rate gate')
PYCODE
  touch "$RUN_DIR/pilot.done"
fi

if [[ ! -f "$RUN_DIR/design2.done" ]]; then
  progress "stage 2/4: individual test adaptation"
  for attempt in 1 2 3; do
    progress "stage 2 pass $attempt/3"
    run_managed env -u BRT4_BEHAVIOR_CACHE_DIR \
    "$PYTHON" -u -m brt6.pipeline.run \
      --instances_path "$GENERATION_DATASET" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --repo_root_base "${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}" \
      --output_dir "$DESIGN2" \
      --model deepseek-v4-flash --llm-provider deepseek \
      --temperature 0.1 --max_tokens 4096 --max_workers 20 \
      --max_semantic_rounds 5 --timeout 7200 --resume \
      --validation_mode buggy_only --alignment-verifier strict \
      --dataset_mode swt --runtime_backend official_docker \
      --official_harness_python "$SWT_PYTHON" \
      --swtbench_root "$ROOT/evaluation/vendor/swtbench" \
      --enable_behavior_target false --enable_seed_mutation true \
      --enable_specialized_feedback true --enable_environment_feedback true \
      --enable_trigger_feedback true --enable_assertion_feedback true \
      --enable_semantic_delta true \
      >> "$RUN_DIR/logs/02_design2.log" 2>&1 || true
    if [[ -f "$DESIGN2/api_paused.json" ]]; then
      progress "stage 2 paused on API failure; progress saved. Restore service, then resume this run."
      exit 75
    fi
    if json_stage_complete "$DESIGN2/summary.json" 276 generation; then
      break
    fi
  done
  if ! json_stage_complete "$DESIGN2/summary.json" 276 generation; then
    progress "stage 2 incomplete after three passes; see $DESIGN2/summary.json"
    exit 2
  fi
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
    --model-name trait-wo-c1-deepseek-v4-flash \
    --compute-coverage false \
    --official-python "$SWT_PYTHON" \
    --swtbench-root "$ROOT/evaluation/vendor/swtbench" \
    > "$RUN_DIR/logs/04_official_evaluation.log" 2>&1
  touch "$RUN_DIR/evaluation.done"
else
  progress "stage 4/4: already complete"
fi

run_managed "$PYTHON" "$ROOT/scripts/compare_wo_c1_full.py" --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/05_comparison.log" 2>&1
touch "$RUN_DIR/completed.done"
progress "complete: $RUN_DIR"

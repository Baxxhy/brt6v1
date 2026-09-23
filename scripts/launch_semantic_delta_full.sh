#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
TMUX_SOCKET_NAME=brt6-runs

usage() {
  cat <<'EOF'
Usage: bash scripts/launch_semantic_delta_full.sh [options]

Launch the complete Semantic Delta pipeline in a detached tmux session.
The pipeline starts with IssueRewrite, generates the selected instances, and
runs the official evaluation for all dataset rows; missing tests count as failures.

Options:
  --dataset {swt|tdd}            Dataset (default: swt).
  --model {deepseek|gpt}         Model provider (default: deepseek).
  --model-id ID                  Concrete model ID (default: DeepSeek-V4-Flash
                                 for deepseek, gpt-5-mini for gpt).
  --max-rounds N                 Semantic Delta rounds per seed, 1-5 (default: 5).
  --issue-workers N              IssueRewrite workers (default: 6).
  --generation-workers N         Generation workers (default: 6).
  --evaluation-workers N         Official evaluation workers (default: 6).
  --generation-timeout N         Per-instance generation timeout in seconds (default: 3600).
  --worktree-timeout N           Git staging timeout in seconds (default: 1200).
  --evaluation-timeout N         Per-instance evaluation timeout in seconds (default: 3600).
  --instances PATH               Override the generation dataset.
  --gold-dataset PATH            Override the official evaluation dataset.
  --official-python PATH         Override the official harness Python.
  --run-name NAME                Result directory name under results/runs/.
  --session NAME                 tmux session name.
  --f2p-only                     Evaluate F2P only (default).
  --with-coverage                Also compute Patch Coverage.
  --allow-dirty                  Permit a development run from a dirty worktree.
  --allow-external-model-data    Confirm that Issue, retrieved code, and test
                                 fragments may be sent to the selected model.
  -h, --help                     Show this help.
EOF
}

DATASET=swt
MODEL_PROVIDER=deepseek
MODEL_ID=""
MAX_ROUNDS=5
ISSUE_WORKERS_VALUE=6
GENERATION_WORKERS_VALUE=6
EVALUATION_WORKERS_VALUE=6
GENERATION_TIMEOUT_VALUE=3600
WORKTREE_TIMEOUT_VALUE=1200
EVALUATION_TIMEOUT_VALUE=3600
INSTANCES_FILE=""
GOLD_FILE=""
OFFICIAL_PYTHON=""
RUN_NAME=""
SESSION_NAME=""
COMPUTE_COVERAGE=false
ALLOW_DIRTY=false
ALLOW_EXTERNAL_MODEL_DATA=false

require_value() {
  if [[ $# -lt 2 || -z "$2" ]]; then
    echo "$1 requires a value" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset|--model|--model-id|--max-rounds|--issue-workers|--generation-workers|--evaluation-workers|--generation-timeout|--worktree-timeout|--evaluation-timeout|--instances|--gold-dataset|--official-python|--run-name|--session)
      require_value "$1" "${2:-}"
      case "$1" in
        --dataset) DATASET=$2 ;;
        --model) MODEL_PROVIDER=$2 ;;
        --model-id) MODEL_ID=$2 ;;
        --max-rounds) MAX_ROUNDS=$2 ;;
        --issue-workers) ISSUE_WORKERS_VALUE=$2 ;;
        --generation-workers) GENERATION_WORKERS_VALUE=$2 ;;
        --evaluation-workers) EVALUATION_WORKERS_VALUE=$2 ;;
        --generation-timeout) GENERATION_TIMEOUT_VALUE=$2 ;;
        --worktree-timeout) WORKTREE_TIMEOUT_VALUE=$2 ;;
        --evaluation-timeout) EVALUATION_TIMEOUT_VALUE=$2 ;;
        --instances) INSTANCES_FILE=$2 ;;
        --gold-dataset) GOLD_FILE=$2 ;;
        --official-python) OFFICIAL_PYTHON=$2 ;;
        --run-name) RUN_NAME=$2 ;;
        --session) SESSION_NAME=$2 ;;
      esac
      shift 2
      ;;
    --f2p-only)
      COMPUTE_COVERAGE=false
      shift
      ;;
    --with-coverage)
      COMPUTE_COVERAGE=true
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=true
      shift
      ;;
    --allow-external-model-data)
      ALLOW_EXTERNAL_MODEL_DATA=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$DATASET" in
  swt)
    INSTANCES_FILE=${INSTANCES_FILE:-$PROJECT_ROOT/data/issues/swt276_issues.json}
    GOLD_FILE=${GOLD_FILE:-$PROJECT_ROOT/data/official/swe_bench_lite_test.json}
    if [[ -z "$OFFICIAL_PYTHON" ]]; then
      for candidate in \
        /root/miniconda3/envs/swtbench/bin/python \
        /root/miniconda3/envs/brt6_swtbench/bin/python; do
        if [[ -x "$candidate" ]]; then
          OFFICIAL_PYTHON=$candidate
          break
        fi
      done
    fi
    ;;
  tdd)
    INSTANCES_FILE=${INSTANCES_FILE:-$PACKAGE_ROOT/TDD-Bench-Verified/TDD_Bench.json}
    GOLD_FILE=${GOLD_FILE:-$INSTANCES_FILE}
    if [[ -z "$OFFICIAL_PYTHON" ]]; then
      for candidate in \
        /root/miniconda3/envs/tddbench/bin/python \
        /root/miniconda3/envs/brt6_tddbench/bin/python; do
        if [[ -x "$candidate" ]]; then
          OFFICIAL_PYTHON=$candidate
          break
        fi
      done
    fi
    ;;
  *)
    echo "--dataset must be swt or tdd" >&2
    exit 2
    ;;
esac

case "$MODEL_PROVIDER" in
  deepseek) MODEL_ID=${MODEL_ID:-DeepSeek-V4-Flash} ;;
  gpt) MODEL_ID=${MODEL_ID:-gpt-5-mini} ;;
  *)
    echo "--model must be deepseek or gpt" >&2
    exit 2
    ;;
esac

if [[ "$ALLOW_EXTERNAL_MODEL_DATA" != true ]]; then
  echo "refusing to send Issue, retrieved code, and test fragments without --allow-external-model-data" >&2
  exit 2
fi

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

for value in "$ISSUE_WORKERS_VALUE" "$GENERATION_WORKERS_VALUE" "$EVALUATION_WORKERS_VALUE" "$GENERATION_TIMEOUT_VALUE" "$WORKTREE_TIMEOUT_VALUE" "$EVALUATION_TIMEOUT_VALUE"; do
  if ! is_positive_integer "$value"; then
    echo "worker counts and timeouts must be positive integers" >&2
    exit 2
  fi
done
if [[ ! "$MAX_ROUNDS" =~ ^[1-5]$ ]]; then
  echo "--max-rounds must be an integer from 1 to 5" >&2
  exit 2
fi
if [[ ! -f "$INSTANCES_FILE" ]]; then
  echo "instances dataset is missing: $INSTANCES_FILE" >&2
  exit 2
fi
if [[ ! -f "$GOLD_FILE" ]]; then
  echo "official evaluation dataset is missing: $GOLD_FILE" >&2
  exit 2
fi
if [[ -z "$OFFICIAL_PYTHON" || ! -x "$OFFICIAL_PYTHON" ]]; then
  echo "official harness Python is unavailable: ${OFFICIAL_PYTHON:-not found}" >&2
  exit 2
fi

RUN_TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RUN_NAME=${RUN_NAME:-semantic_delta_${DATASET}_full_${RUN_TIMESTAMP}}
SESSION_NAME=${SESSION_NAME:-brt6_semantic_delta_${DATASET}_full_${RUN_TIMESTAMP}}
if [[ ! "$RUN_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "--run-name may contain only letters, digits, dot, underscore, and hyphen" >&2
  exit 2
fi
if [[ ! "$SESSION_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "--session may contain only letters, digits, dot, underscore, and hyphen" >&2
  exit 2
fi

RUN_DIR=$PROJECT_ROOT/results/runs/$RUN_NAME
LAUNCHER_LOG=$PROJECT_ROOT/results/runs/$RUN_NAME.launcher.log
if [[ -e "$RUN_DIR" || -e "$LAUNCHER_LOG" ]]; then
  echo "refusing to overwrite existing run artifacts for: $RUN_NAME" >&2
  exit 2
fi
if env SHELL=/bin/sh tmux -L "$TMUX_SOCKET_NAME" -f /dev/null has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "tmux session already exists: $SESSION_NAME" >&2
  exit 2
fi

mkdir -p "$PROJECT_ROOT/results/runs"

ENVIRONMENT=(
  env
  "RUN_DIR=$RUN_DIR"
  "INSTANCES_PATH=$INSTANCES_FILE"
  "GOLD_DATASET=$GOLD_FILE"
  "BRT_MODEL_ID=$MODEL_ID"
  # A transient shared-gateway error must not pause a full experiment after
  # merely trying each credential once. Cycle through the configured pool a
  # bounded number of times while preserving completed journal steps.
  "BRT_LLM_POOL_ROTATION_ATTEMPTS=${BRT_LLM_POOL_ROTATION_ATTEMPTS:-10}"
  "ISSUE_WORKERS=$ISSUE_WORKERS_VALUE"
  "GENERATION_WORKERS=$GENERATION_WORKERS_VALUE"
  "EVALUATION_WORKERS=$EVALUATION_WORKERS_VALUE"
  "MAX_SEMANTIC_ROUNDS=$MAX_ROUNDS"
  "GENERATION_TIMEOUT=$GENERATION_TIMEOUT_VALUE"
  "BRT_WORKTREE_TIMEOUT=$WORKTREE_TIMEOUT_VALUE"
  "FORMAL_EVAL_TIMEOUT=$EVALUATION_TIMEOUT_VALUE"
  "ALLOW_INCOMPLETE_FORMAL_EVAL=true"
  "COMPUTE_PATCH_COVERAGE=$COMPUTE_COVERAGE"
  "BRT_FINAL_SELECTION=${BRT_FINAL_SELECTION:-strict_icore}"
)
if [[ "$ALLOW_DIRTY" == true ]]; then
  ENVIRONMENT+=("BRT_ALLOW_DIRTY_WORKTREE=1")
fi
if [[ "$DATASET" == swt ]]; then
  ENVIRONMENT+=("SWTBENCH_PYTHON=$OFFICIAL_PYTHON")
else
  ENVIRONMENT+=("TDDBENCH_PYTHON=$OFFICIAL_PYTHON")
fi

COMMAND=(
  env -u BASH_ENV /bin/bash --noprofile --norc "$SCRIPT_DIR/run_p0_simple_llm_selector_full.sh"
  --dataset "$DATASET"
  --model "$MODEL_PROVIDER"
  --behavior-target on
  --mutation on
  --specialized-feedback on
  --environment-feedback on
  --trigger-feedback on
  --assertion-feedback on
  --semantic-delta on
)

printf -v escaped_root '%q' "$PROJECT_ROOT"
printf -v escaped_log '%q' "$LAUNCHER_LOG"
printf -v escaped_command '%q ' "${ENVIRONMENT[@]}" "${COMMAND[@]}"
env SHELL=/bin/sh tmux -L "$TMUX_SOCKET_NAME" -f /dev/null new-session -d -s "$SESSION_NAME" \
  "cd $escaped_root && exec $escaped_command > $escaped_log 2>&1"

printf 'session=%s\n' "$SESSION_NAME"
printf 'tmux_socket=%s\n' "$TMUX_SOCKET_NAME"
printf 'attach_command=tmux -L %s attach -t %s\n' "$TMUX_SOCKET_NAME" "$SESSION_NAME"
printf 'run_name=%s\n' "$RUN_NAME"
printf 'run_dir=%s\n' "$RUN_DIR"
printf 'launcher_log=%s\n' "$LAUNCHER_LOG"

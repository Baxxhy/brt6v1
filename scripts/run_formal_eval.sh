#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
cd "$PROJECT_ROOT"

RUN_DIR=${RUN_DIR:-"${1:-}"}
if [[ -z "$RUN_DIR" ]]; then
  echo "Usage: RUN_DIR=results/runs/<run_name> bash scripts/run_formal_eval.sh" >&2
  exit 2
fi
RUN_DIR=$(realpath -m "$RUN_DIR")
export DOCKER_HOST=${DOCKER_HOST:-unix:///run/mutate-docker.sock}
export TMPDIR=${TMPDIR:-"$RUN_DIR/tmp"}
export TEMP=${TEMP:-"$RUN_DIR/tmp"}
export TMP=${TMP:-"$RUN_DIR/tmp"}
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-"$RUN_DIR/cache"}
REPO_ROOT_BASE=${REPO_ROOT_BASE:-"$PACKAGE_ROOT/swe_repos"}
INSTANCES_PATH=${INSTANCES_PATH:-"$PROJECT_ROOT/data/issues/swt276_issues.json"}
WORKERS=${WORKERS:-6}
TIMEOUT=${TIMEOUT:-1800}

mkdir -p "$RUN_DIR/evaluation/formal" "$RUN_DIR/logs" "$TMPDIR" "$XDG_CACHE_HOME"
cmd=(
  python "$PROJECT_ROOT/scripts/run_formal_eval_after_generation.py"
  --outputs_dir "$RUN_DIR/generation"
  --evaluation_dir "$RUN_DIR/evaluation/formal"
  --summary_path "$RUN_DIR/evaluation/formal_eval_summary.json"
  --log_path "$RUN_DIR/logs/formal_eval.log"
  --dataset_file "$INSTANCES_PATH"
  --repo_root_base "$REPO_ROOT_BASE"
  --max_workers "$WORKERS"
  --timeout "$TIMEOUT"
  --eval_completed_only false
  --resume
)

printf '%q ' "${cmd[@]}" | tee "$RUN_DIR/logs/formal_eval.command.txt"
printf '\n' | tee -a "$RUN_DIR/logs/formal_eval.command.txt"
"${cmd[@]}" 2>&1 | tee "$RUN_DIR/logs/formal_eval_driver.log"
touch "$RUN_DIR/formal_eval.done"

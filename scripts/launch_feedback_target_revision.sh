#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SOURCE=${SOURCE_RUN:-$ROOT/results/runs/trait_xiaojing_deepseekv4flash_full_20260918_051829}
OUT=${RUN_DIR:-$ROOT/results/runs/target_revision18_20260919}
mkdir -p "$OUT/tmp"
exec 9>"$OUT/launcher.lock"
flock -n 9 || { echo 'Pilot already running'; exit 1; }
export PYTHONPATH="$(dirname "$ROOT")"
export BRT_API_POOL_FILE="$ROOT/.secrets/api_pool.json"
export BRT_ALLOWED_API_HOST=api.open.xiaojingai.com
export BRT_MODEL_ID=deepseek-v4-flash
export BRT_DISABLE_THINKING=1 BRT_LLM_STREAM=1 BRT_RETRY_TRANSIENT_API=1
export BRT3_LLM_REQUEST_TIMEOUT=600 BRT3_LLM_MAX_ATTEMPTS=2 BRT_LLM_TRUNCATION_MAX_TOKENS=8192
export BRT_COST_DIR="$OUT"
export BRT_REQUIRE_OFFICIAL_DOCKER=1 BRT_ALLOW_DIRTY_WORKTREE=1
export BRT_OFFICIAL_DOCKER_STARTUP_TIMEOUT=7200
export DOCKER_HOST=unix:///run/mutate-docker.sock
export TMPDIR="$OUT/tmp" TEMP="$OUT/tmp" TMP="$OUT/tmp"
cd "$ROOT"
exec /root/miniconda3/envs/icore/bin/python -u scripts/run_feedback_target_revision.py \
  --source-run "$SOURCE" --output "$OUT" "$@"

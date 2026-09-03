#!/usr/bin/env bash
set -euo pipefail

EVALUATION_DIR=${1:?Usage: bash scripts/watch_f2p_progress.sh EVALUATION_DIR [TOTAL]}
TOTAL=${2:-276}
HARNESS_LOG=$EVALUATION_DIR/official_harness.log
MANIFEST=$EVALUATION_DIR/official_run_manifest.json
METRICS=$EVALUATION_DIR/metrics.json

while true; do
  COMPLETED=0
  if [[ -f "$HARNESS_LOG" ]]; then
    LAST_PROGRESS=$(tr '\r' '\n' < "$HARNESS_LOG" \
      | grep -oE "[0-9]+/${TOTAL}" \
      | tail -n 1 || true)
    if [[ -n "$LAST_PROGRESS" ]]; then
      COMPLETED=${LAST_PROGRESS%%/*}
    fi
  fi
  REMAINING=$((TOTAL - COMPLETED))
  printf '\rOfficial F2P: %d/%d | remaining: %d   ' \
    "$COMPLETED" "$TOTAL" "$REMAINING"

  if [[ -f "$METRICS" ]]; then
    break
  fi
  STATUS=$(jq -r '.status // ""' "$MANIFEST" 2>/dev/null || true)
  if [[ -n "$STATUS" && "$STATUS" != "running" ]]; then
    printf '\nEvaluation stopped with status: %s\n' "$STATUS"
    exit 1
  fi
  sleep 5
done

SUCCESS=$(jq -r '.f2p_success' "$METRICS")
FAIL=$(jq -r '.f2p_fail' "$METRICS")
PERCENT=$(jq -r '.f2p_at_1_percent' "$METRICS")
printf '\rOfficial F2P: %d/%d | remaining: 0   \n' "$TOTAL" "$TOTAL"
printf 'Final F2P: %s/%s = %.2f%% | failed: %s\n' \
  "$SUCCESS" "$TOTAL" "$PERCENT" "$FAIL"

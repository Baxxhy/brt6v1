#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

BASELINE_RUN=${BASELINE_RUN:-$PROJECT_ROOT/results/runs/brt6_swt276_deepseekv4flash_full_20260902_164451}
BASELINE_REPORT=${BASELINE_REPORT:-$BASELINE_RUN/evaluation/formal_f2p_clean_20260903/official_report.json}
SOURCE_INSTANCES=${SOURCE_INSTANCES:-$PROJECT_ROOT/data/issues/swt276_issues.json}
SOURCE_GOLD=${SOURCE_GOLD:-$PROJECT_ROOT/data/official/swe_bench_lite_test.json}
RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/cio_first_round_f2f5_${RUN_TIMESTAMP}}

INSTANCE_IDS=(
  astropy__astropy-14182
  django__django-10914
  matplotlib__matplotlib-18869
  scikit-learn__scikit-learn-10297
  sympy__sympy-21171
)

for REQUIRED_FILE in "$BASELINE_REPORT" "$SOURCE_INSTANCES" "$SOURCE_GOLD"; do
  if [[ ! -f "$REQUIRED_FILE" ]]; then
    echo "required input is missing: $REQUIRED_FILE" >&2
    exit 2
  fi
done

if [[ -e "$RUN_DIR" ]]; then
  echo "refusing to overwrite existing run directory: $RUN_DIR" >&2
  exit 2
fi

mkdir -p "$RUN_DIR/logs"
SELECTED_INSTANCES=$RUN_DIR/selected_instances.json
SELECTED_GOLD=$RUN_DIR/selected_official_dataset.json

python3 -c '
import json
import sys
from pathlib import Path

report_path, instances_path, gold_path, selected_instances, selected_gold, *ids = sys.argv[1:]
report = json.loads(Path(report_path).read_text(encoding="utf-8"))
unresolved = set(report.get("unresolved_ids") or [])
missing_failures = [instance_id for instance_id in ids if instance_id not in unresolved]
if missing_failures:
    raise SystemExit(f"selected IDs are not first-round unresolved: {missing_failures}")

def select(source_path):
    payload = json.loads(Path(source_path).read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else list(payload.values())
    by_id = {str(row.get("instance_id") or ""): row for row in rows if isinstance(row, dict)}
    missing = [instance_id for instance_id in ids if instance_id not in by_id]
    if missing:
        raise SystemExit(f"selected IDs are missing from {source_path}: {missing}")
    return [by_id[instance_id] for instance_id in ids]

Path(selected_instances).write_text(
    json.dumps(select(instances_path), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
Path(selected_gold).write_text(
    json.dumps(select(gold_path), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
' "$BASELINE_REPORT" "$SOURCE_INSTANCES" "$SOURCE_GOLD" \
  "$SELECTED_INSTANCES" "$SELECTED_GOLD" "${INSTANCE_IDS[@]}"

printf '%s\n' "${INSTANCE_IDS[@]}" >"$RUN_DIR/selected_ids.txt"

export RUN_DIR
export INSTANCES_PATH=$SELECTED_INSTANCES
export GOLD_DATASET=$SELECTED_GOLD
export BRT_MODEL_ID=${BRT_MODEL_ID:-DeepSeek-V4-Flash}
export BRT_ALLOW_DIRTY_WORKTREE=1
if [[ -z "${SWTBENCH_PYTHON:-}" && -x /root/miniconda3/envs/swtbench/bin/python ]]; then
  export SWTBENCH_PYTHON=/root/miniconda3/envs/swtbench/bin/python
fi
export ISSUE_WORKERS=${ISSUE_WORKERS:-5}
export GENERATION_WORKERS=${GENERATION_WORKERS:-5}
export EVALUATION_WORKERS=${EVALUATION_WORKERS:-5}
export COMPUTE_PATCH_COVERAGE=false

exec bash "$PROJECT_ROOT/scripts/run_semantic_delta_swt_full.sh" --model deepseek

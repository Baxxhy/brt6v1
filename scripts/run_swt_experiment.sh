#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
RUNTIME_ENV_FILE=$PROJECT_ROOT/.bootstrap/use_fresh_swt_server.sh

if [[ "$PROJECT_ROOT" == "/" ]]; then
  echo "Refusing to launch from an unsafe project root." >&2
  exit 2
fi
if [[ ! -f "$RUNTIME_ENV_FILE" ]]; then
  echo "Fresh-server setup is incomplete: $RUNTIME_ENV_FILE is missing." >&2
  echo "Run: bash $PROJECT_ROOT/scripts/bootstrap_fresh_swt_server.sh" >&2
  exit 2
fi
if [[ $# -eq 0 ]]; then
  cat >&2 <<'EOF'
Usage: bash scripts/run_swt_experiment.sh [SWT launcher options]

Frozen full method:
  bash scripts/run_swt_experiment.sh \
    --behavior-target on \
    --behavior-target-cache data/behavior_targets/swt/full_method_f2p_47_46_20260717
EOF
  exit 2
fi
for argument in "$@"; do
  if [[ "$argument" == --dataset || "$argument" == --dataset=* ]]; then
    echo "This wrapper fixes --dataset swt; do not pass --dataset again." >&2
    exit 2
  fi
done

# shellcheck disable=SC1090
source "$RUNTIME_ENV_FILE"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Configured controller Python is missing: $PYTHON_BIN" >&2
  echo "Rerun: bash $PROJECT_ROOT/scripts/bootstrap_fresh_swt_server.sh" >&2
  exit 2
fi
TEMPLATE_SUMMARY=$PROJECT_ROOT/.bootstrap/swt-template-environments/summary.json
REPOSITORY_MANIFEST=$PROJECT_ROOT/.bootstrap/repositories.check.json
CACHE_VALIDATION=$PROJECT_ROOT/.bootstrap/behavior_target_cache_validation.json
"$PYTHON_BIN" - "$TEMPLATE_SUMMARY" "$REPOSITORY_MANIFEST" "$CACHE_VALIDATION" <<'PY'
import json
import sys
from pathlib import Path

template_path, repository_path, cache_path = map(Path, sys.argv[1:])
for path in (template_path, repository_path, cache_path):
    if not path.is_file():
        raise SystemExit(
            f"fresh-server readiness artifact is missing: {path}; "
            "rerun scripts/bootstrap_fresh_swt_server.sh"
        )
templates = json.loads(template_path.read_text(encoding="utf-8"))
if (
    templates.get("total") != 52
    or templates.get("ready_count") != 52
    or templates.get("failed_count") != 0
):
    raise SystemExit(
        "SWT template prewarm is incomplete; rerun the bootstrap and inspect "
        ".bootstrap/swt-template-environments/failure_diagnostics.json"
    )
repositories = json.loads(repository_path.read_text(encoding="utf-8"))
if not repositories.get("ready") or len(repositories.get("repositories") or []) != 12:
    raise SystemExit("the 12-repository SWT cache is not ready; rerun the bootstrap")
cache = json.loads(cache_path.read_text(encoding="utf-8"))
if cache.get("dataset_mode") != "swt" or cache.get("instance_count") != 276:
    raise SystemExit("the frozen SWT-276 BehaviorTarget cache validation is invalid")
print("fresh_server_readiness_gate=passed")
PY

cd "$PROJECT_ROOT"
exec bash "$PROJECT_ROOT/scripts/run_p0_simple_llm_selector_full.sh" --dataset swt "$@"

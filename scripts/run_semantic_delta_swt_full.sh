#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# The full Semantic Delta treatment is F2P-only. All Docker/runtime behavior,
# model selection, API-pool reuse, and restart semantics remain in the official
# launcher instead of being duplicated here.
export COMPUTE_PATCH_COVERAGE=false
exec bash "$SCRIPT_DIR/run_p0_simple_llm_selector_full.sh" \
  --dataset swt \
  --mutation on \
  --specialized-feedback on \
  --environment-feedback on \
  --trigger-feedback on \
  --assertion-feedback on \
  --semantic-delta on \
  "$@"

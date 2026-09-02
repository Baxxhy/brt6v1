#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)

export DOCKER_HOST=${DOCKER_HOST:-unix:///run/mutate-docker.sock}

SWTBENCH_ROOT=${SWTBENCH_ROOT:-$PACKAGE_ROOT/swt-bench}
TDD_BENCH_ROOT=${TDD_BENCH_ROOT:-$PACKAGE_ROOT/TDD-Bench-Verified}
BEHAVIOR_CACHE=${BEHAVIOR_CACHE:-$PROJECT_ROOT/data/behavior_targets/swt/full_method_f2p_47_46_20260717}

[[ -f "$SWTBENCH_ROOT/src/main.py" ]] || {
  echo "Official SWTBench checkout is missing: $SWTBENCH_ROOT" >&2
  exit 2
}
[[ -f "$TDD_BENCH_ROOT/tddbench/harness/run_evaluation.py" ]] || {
  echo "Official TDDBench checkout is missing: $TDD_BENCH_ROOT" >&2
  exit 2
}
docker info >/dev/null 2>&1 || {
  echo "Docker daemon is unavailable; official SWTBench cannot run." >&2
  exit 2
}

export SWTBENCH_ROOT TDD_BENCH_ROOT
export BRT_ALLOW_DIRTY_WORKTREE=${BRT_ALLOW_DIRTY_WORKTREE:-1}
export DEEPSEEK_MODEL=deepseek-v3
export OFFICIAL_BENCHMARK_EVALUATION=1
export RUNTIME_BACKEND=official_docker

CONDA_EXE=${CONDA_EXE:-/root/conda/ENTER/bin/conda}
if [[ ! -x "$CONDA_EXE" ]]; then
  CONDA_EXE=$(command -v conda || true)
fi
[[ -n "$CONDA_EXE" && -x "$CONDA_EXE" ]] || {
  echo "Conda is unavailable." >&2
  exit 2
}
CONDA_BASE=$($CONDA_EXE info --base)
export SWTBENCH_PYTHON=${SWTBENCH_PYTHON:-$CONDA_BASE/envs/brt6_swtbench/bin/python}
[[ -x "$SWTBENCH_PYTHON" ]] || {
  echo "Official SWTBench environment is missing; run scripts/bootstrap_official_benchmarks.sh" >&2
  exit 2
}
export OFFICIAL_HARNESS_PYTHON=$SWTBENCH_PYTHON
RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
export RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/official_docker_swt_deepseekv3_full_$RUN_TIMESTAMP}

if [[ "${1:-}" == "--preflight-only" ]]; then
  PYTHON_BIN=${PYTHON_BIN:-$CONDA_BASE/envs/icore/bin/python}
  PYTHONPATH="$PACKAGE_ROOT" "$PYTHON_BIN" - <<'PY'
from brt6.llm.api_pool import configured_apis
count = len(configured_apis("deepseek"))
if count < 1:
    raise SystemExit("DeepSeek API pool is empty")
print(f"deepseek_api_entries={count}")
PY
  PYTHONPATH="$SWTBENCH_ROOT/src:$SWTBENCH_ROOT" \
    "$SWTBENCH_PYTHON" -m src.main --help >/dev/null
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/validate_behavior_target_cache.py" \
    --cache-dir "$BEHAVIOR_CACHE" \
    --instances-path "$PROJECT_ROOT/data/issues/swt276_issues.json" \
    --dataset-mode swt \
    --code-retrieval-path "$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json" \
    --test-retrieval-path "$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json" \
    >/dev/null
  echo "official_swt_full_preflight=passed"
  exit 0
fi

cd "$PROJECT_ROOT"
exec bash "$PROJECT_ROOT/scripts/run_p0_simple_llm_selector_full.sh" \
  --dataset swt \
  --model deepseek \
  --behavior-target on \
  --behavior-target-cache "$BEHAVIOR_CACHE"

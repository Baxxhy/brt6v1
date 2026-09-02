#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PACKAGE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
SWTBENCH_ROOT=${SWTBENCH_ROOT:-$PACKAGE_ROOT/swt-bench}
TDD_BENCH_ROOT=${TDD_BENCH_ROOT:-$PACKAGE_ROOT/TDD-Bench-Verified}

CONDA_EXE=${CONDA_EXE:-/root/conda/ENTER/bin/conda}
if [[ ! -x "$CONDA_EXE" ]]; then
  CONDA_EXE=$(command -v conda || true)
fi
[[ -n "$CONDA_EXE" && -x "$CONDA_EXE" ]] || {
  echo "Conda is unavailable." >&2
  exit 2
}
[[ -f "$SWTBENCH_ROOT/pyproject.toml" ]] || {
  echo "Official SWTBench checkout is missing: $SWTBENCH_ROOT" >&2
  exit 2
}
[[ -f "$TDD_BENCH_ROOT/setup.py" ]] || {
  echo "Official TDDBench checkout is missing: $TDD_BENCH_ROOT" >&2
  exit 2
}

if ! "$CONDA_EXE" run -n brt6_swtbench python -c 'import sys' >/dev/null 2>&1; then
  "$CONDA_EXE" create -n brt6_swtbench -y python=3.12 pip
fi
"$CONDA_EXE" run -n brt6_swtbench python -m pip install -e "$SWTBENCH_ROOT"

if ! "$CONDA_EXE" run -n brt6_tddbench python -c 'import sys' >/dev/null 2>&1; then
  "$CONDA_EXE" create -n brt6_tddbench -y python=3.12 pip
fi
# The official setup declares an unbounded datasets dependency that now
# conflicts with cldk==1.0.6's pyarrow==20 pin. datasets 3.6 is compatible
# with the official harness API and the declared CLDK/PyArrow versions.
"$CONDA_EXE" run -n brt6_tddbench python -m pip install \
  beautifulsoup4 'datasets==3.6.0' docker ghapi python-dotenv requests \
  unidiff tqdm pytest 'cldk==1.0.6'
"$CONDA_EXE" run -n brt6_tddbench python -m pip install --no-deps -e "$TDD_BENCH_ROOT"

(
  cd "$SWTBENCH_ROOT"
  PYTHONPATH="$SWTBENCH_ROOT/src:$SWTBENCH_ROOT" \
    "$CONDA_EXE" run -n brt6_swtbench python -m src.main --help >/dev/null
)
(
  cd "$TDD_BENCH_ROOT"
  PYTHONPATH="$TDD_BENCH_ROOT" \
    "$CONDA_EXE" run -n brt6_tddbench \
    python -m tddbench.harness.run_evaluation --help >/dev/null
)
echo "official_benchmark_environments=ready"

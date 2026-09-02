#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
WORKSPACE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)

DATASET_MODE=swt
INSTALL_SYSTEM_PACKAGES=true
CLONE_REPOSITORIES=true
NON_INTERACTIVE=false
CONDA_ROOT=${CONDA_ROOT:-$HOME/miniforge3}
PREPARE_TEMPLATE_ENVIRONMENTS=true
TEMPLATE_WORKERS=${BRT_TEMPLATE_WORKERS:-4}

usage() {
  cat <<'EOF'
Usage: bash scripts/bootstrap_machine.sh [options]

Options:
  --dataset {swt|tdd|all}       Inputs and repositories to prepare (default: swt).
  --conda-root PATH             Miniforge installation path (default: ~/miniforge3).
  --skip-system-packages        Do not use apt-get for build dependencies.
  --skip-repositories           Do not clone/verify benchmark repositories.
  --skip-template-environments Skip generation-time TDD dependency templates.
  --template-workers N         Concurrent TDD template repairs, 1-8 (default: 4).
  --non-interactive             Do not prompt for API keys.
  -h, --help                    Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset) DATASET_MODE=${2:?missing dataset}; shift 2 ;;
    --dataset=*) DATASET_MODE=${1#*=}; shift ;;
    --conda-root) CONDA_ROOT=${2:?missing conda root}; shift 2 ;;
    --conda-root=*) CONDA_ROOT=${1#*=}; shift ;;
    --skip-system-packages) INSTALL_SYSTEM_PACKAGES=false; shift ;;
    --skip-repositories) CLONE_REPOSITORIES=false; shift ;;
    --skip-template-environments) PREPARE_TEMPLATE_ENVIRONMENTS=false; shift ;;
    --template-workers) TEMPLATE_WORKERS=${2:?missing worker count}; shift 2 ;;
    --template-workers=*) TEMPLATE_WORKERS=${1#*=}; shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$DATASET_MODE" in swt|tdd|all) ;; *) echo "dataset must be swt, tdd, or all" >&2; exit 2 ;; esac
if [[ ! "$TEMPLATE_WORKERS" =~ ^[0-9]+$ ]] ||
   (( TEMPLATE_WORKERS < 1 || TEMPLATE_WORKERS > 8 )); then
  echo "--template-workers must be an integer between 1 and 8" >&2
  exit 2
fi

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This reproducibility bootstrap currently supports Linux only." >&2
  exit 2
fi

if [[ "$INSTALL_SYSTEM_PACKAGES" == "true" ]]; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Automatic system setup requires apt-get; install equivalent build tools or use --skip-system-packages." >&2
    exit 2
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    ROOT_COMMAND=()
  elif command -v sudo >/dev/null 2>&1; then
    ROOT_COMMAND=(sudo)
  else
    echo "apt-get is available but root/sudo is not; use --skip-system-packages after installing build tools." >&2
    exit 2
  fi
  "${ROOT_COMMAND[@]}" apt-get update
  "${ROOT_COMMAND[@]}" apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates cm-super-minimal curl dvipng gdal-bin \
    git gfortran libffi-dev libgdal-dev libgeos-dev \
    libfreetype6-dev libjpeg-dev liblapack-dev libopenblas-dev libpng-dev \
    libssl-dev libxml2-dev libxslt1-dev pkg-config rsync texlive-latex-base \
    texlive-fonts-recommended texlive-latex-extra
fi

CONDA_EXE=${CONDA_EXE:-}
if [[ -z "$CONDA_EXE" ]] && command -v conda >/dev/null 2>&1; then
  CONDA_EXE=$(command -v conda)
fi
if [[ -z "$CONDA_EXE" ]] && [[ -x "$CONDA_ROOT/bin/conda" ]]; then
  CONDA_EXE="$CONDA_ROOT/bin/conda"
fi
if [[ -z "$CONDA_EXE" ]]; then
  ARCH=$(uname -m)
  case "$ARCH" in x86_64|aarch64|ppc64le) ;; *) echo "unsupported Linux architecture: $ARCH" >&2; exit 2 ;; esac
  INSTALLER="Miniforge3-Linux-${ARCH}.sh"
  URL="https://github.com/conda-forge/miniforge/releases/latest/download/${INSTALLER}"
  TMP_DIR=$(mktemp -d)
  trap 'rm -rf "$TMP_DIR"' EXIT
  curl -fsSLo "$TMP_DIR/$INSTALLER" "$URL"
  curl -fsSLo "$TMP_DIR/$INSTALLER.sha256" "$URL.sha256"
  (cd "$TMP_DIR" && sha256sum -c "$INSTALLER.sha256")
  bash "$TMP_DIR/$INSTALLER" -b -p "$CONDA_ROOT"
  CONDA_EXE="$CONDA_ROOT/bin/conda"
fi

CONDA_BASE=$("$CONDA_EXE" info --base)
CONDA_SH="$CONDA_BASE/etc/profile.d/conda.sh"
if [[ ! -f "$CONDA_SH" ]]; then
  echo "Conda initialization script not found: $CONDA_SH" >&2
  exit 2
fi
export CONDA_EXE
export BRT3_CONDA_SH="$CONDA_SH"
export PATH="$(dirname "$CONDA_EXE"):$PATH"

if "$CONDA_EXE" run -n icore python -c 'import sys' >/dev/null 2>&1; then
  "$CONDA_EXE" install -n icore -y python=3.12 pip
else
  "$CONDA_EXE" create -n icore -y python=3.12 pip
fi
PYTHON_BIN="$CONDA_BASE/envs/icore/bin/python"
"$PYTHON_BIN" -m pip install pip==26.1.2 setuptools==82.0.1 wheel==0.47.0
"$PYTHON_BIN" -m pip install -r "$PROJECT_ROOT/requirements.txt"

if [[ "$DATASET_MODE" == "swt" || "$DATASET_MODE" == "all" ]]; then
  [[ -f "$WORKSPACE_ROOT/swt-bench/src/main.py" ]] || {
    echo "missing official SWTBench checkout: $WORKSPACE_ROOT/swt-bench" >&2
    exit 4
  }
fi

bash "$PROJECT_ROOT/scripts/bootstrap_official_benchmarks.sh"

export PYTHONPATH="$WORKSPACE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
API_COUNT=$("$PYTHON_BIN" - <<'PY'
from brt6.llm.api_pool import configured_apis
print(len(configured_apis()))
PY
)
if [[ "$API_COUNT" -eq 0 ]]; then
  if [[ "$NON_INTERACTIVE" == "true" ]] || [[ ! -t 0 ]]; then
    echo "No API keys configured. Run: $PYTHON_BIN $PROJECT_ROOT/scripts/configure_api_keys.py" >&2
    exit 3
  fi
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/configure_api_keys.py"
fi

SWT_DATASET="$PROJECT_ROOT/data/issues/swt276_issues.json"
TDD_ROOT="$WORKSPACE_ROOT/TDD-Bench-Verified"
TDD_DATASET="$TDD_ROOT/TDD_Bench.json"
DATASET_ARGS=()
if [[ "$DATASET_MODE" == "swt" || "$DATASET_MODE" == "all" ]]; then
  [[ -f "$SWT_DATASET" ]] || { echo "missing SWT dataset: $SWT_DATASET" >&2; exit 4; }
  DATASET_ARGS+=(--dataset "$SWT_DATASET")
fi
if [[ "$DATASET_MODE" == "tdd" || "$DATASET_MODE" == "all" ]]; then
  if [[ ! -d "$TDD_ROOT/.git" ]]; then
    git clone https://github.com/IBM/TDD-Bench-Verified.git "$TDD_ROOT"
  else
    git -C "$TDD_ROOT" fetch --tags --prune origin
  fi
  [[ -f "$TDD_DATASET" ]] || { echo "missing TDD dataset after clone: $TDD_DATASET" >&2; exit 4; }
  DATASET_ARGS+=(--dataset "$TDD_DATASET")
fi

REPO_ROOT=${REPO_ROOT:-$WORKSPACE_ROOT/swe_repos}
mkdir -p "$PROJECT_ROOT/.bootstrap"
if [[ "$CLONE_REPOSITORIES" == "true" ]]; then
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/bootstrap_repositories.py" \
    "${DATASET_ARGS[@]}" \
    --repo-root "$REPO_ROOT" \
    --manifest "$PROJECT_ROOT/.bootstrap/repositories.json"
fi

if [[ "$PREPARE_TEMPLATE_ENVIRONMENTS" == "true" ]] &&
   [[ "$DATASET_MODE" == "tdd" || "$DATASET_MODE" == "all" ]]; then
  TDD_TEMPLATE_ROOT=$PROJECT_ROOT/.bootstrap/tdd-template-environments
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/prepare_tdd_template_environments.py" \
    --instances_path "$TDD_DATASET" \
    --work_root "$TDD_TEMPLATE_ROOT" \
    --timeout 3600 \
    --retries 3 \
    --workers "$TEMPLATE_WORKERS"
  BRT_TDD_STRICT_LOCAL_CONDA=1 "$PYTHON_BIN" \
    "$PROJECT_ROOT/scripts/preflight_tdd_local_conda.py" \
    --instances_path "$TDD_DATASET" \
    --repo_root_base "$REPO_ROOT" \
    --output_path "$PROJECT_ROOT/.bootstrap/tdd_local_conda_preflight.json"
fi

cat <<EOF

Bootstrap complete.
Framework Python: $PYTHON_BIN
Conda: $CONDA_EXE
Repository cache: $REPO_ROOT

Run the experiment from $PROJECT_ROOT:
  bash scripts/run_p0_simple_llm_selector_full.sh --dataset swt --behavior-target on
  bash scripts/run_p0_simple_llm_selector_full.sh --dataset swt --behavior-target off
  bash scripts/run_p0_simple_llm_selector_full.sh --dataset tdd
EOF

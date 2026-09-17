#!/usr/bin/env bash
set -Eeuo pipefail

# Prepare a fresh SWT experiment server that already has Conda installed.
# Resolve every managed location from this checkout so the same command works
# on workstations and clusters without editing machine-specific absolute paths.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
WORKSPACE_ROOT=$(cd "$PROJECT_ROOT/.." && pwd)
REPO_ROOT=$WORKSPACE_ROOT/swe_repos
BOOTSTRAP_ROOT=$PROJECT_ROOT/.bootstrap
CONDA_STORAGE_ROOT=$WORKSPACE_ROOT/.brt5-conda
RUNTIME_ENV_FILE=$BOOTSTRAP_ROOT/use_fresh_swt_server.sh
CONTROLLER_ENV=icore
OFFICIAL_SWT_ENV=swtbench
ENV_PREFIX=brt5_
INSTALL_SYSTEM_PACKAGES=true
PREWARM=true
PREWARM_WORKERS=${BRT_PREWARM_WORKERS:-4}
RUN_TESTS=true
NON_INTERACTIVE=false
REQUESTED_CONDA_EXE=${CONDA_EXE:-}
REQUESTED_CONTROLLER_PYTHON=

usage() {
  cat <<'EOF'
Usage: bash scripts/bootstrap_fresh_swt_server.sh [options]

Prepare this fresh Linux server for the 276-instance SWT experiment. Conda
must already be installed; this script never downloads or installs another
Conda distribution.

Options:
  --conda-exe PATH          Explicit path to the installed conda executable.
  --controller-python PATH  Reuse an already prepared Python 3.10+ controller;
                            skip creating/installing the icore controller.
  --skip-system-packages    Skip apt-get (only after installing prerequisites).
  --skip-prewarm            Skip the 52 SWT template environments (not run-ready).
  --prewarm-workers N       Concurrent SWT environment builds, 1-8 (default: 4).
  --skip-tests              Skip the local regression test suite.
  --non-interactive         Do not prompt for API keys; require a configured key.
  -h, --help                Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --conda-exe) REQUESTED_CONDA_EXE=${2:?missing path}; shift 2 ;;
    --conda-exe=*) REQUESTED_CONDA_EXE=${1#*=}; shift ;;
    --controller-python) REQUESTED_CONTROLLER_PYTHON=${2:?missing path}; shift 2 ;;
    --controller-python=*) REQUESTED_CONTROLLER_PYTHON=${1#*=}; shift ;;
    --skip-system-packages) INSTALL_SYSTEM_PACKAGES=false; shift ;;
    --skip-prewarm) PREWARM=false; shift ;;
    --prewarm-workers) PREWARM_WORKERS=${2:?missing worker count}; shift 2 ;;
    --prewarm-workers=*) PREWARM_WORKERS=${1#*=}; shift ;;
    --skip-tests) RUN_TESTS=false; shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! "$PREWARM_WORKERS" =~ ^[0-9]+$ ]] ||
   (( PREWARM_WORKERS < 1 || PREWARM_WORKERS > 8 )); then
  echo "--prewarm-workers must be an integer between 1 and 8" >&2
  exit 2
fi

if [[ "$PROJECT_ROOT" == "/" || "$WORKSPACE_ROOT" == "/" ]]; then
  echo "Refusing to bootstrap from an unsafe project/workspace root." >&2
  echo "Project:   $PROJECT_ROOT" >&2
  echo "Workspace: $WORKSPACE_ROOT" >&2
  exit 2
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "This bootstrap supports Linux x86_64 only." >&2
  exit 2
fi

required_project_files=(
  requirements.txt
  requirements-reproduce.txt
  data/issues/swt276_issues.json
  retrieval_results/code/code_retrieval_results_gpt.json
  retrieval_results/test/icore/gpt/related_tests.json
  evaluation/vendor/swtbench/src/main.py
  scripts/bootstrap_repositories.py
  scripts/prewarm_swt_environments.py
  scripts/validate_behavior_target_cache.py
  scripts/run_p0_simple_llm_selector_full.sh
)
for relative_path in "${required_project_files[@]}"; do
  if [[ ! -f "$PROJECT_ROOT/$relative_path" ]]; then
    echo "Required project file is missing: $PROJECT_ROOT/$relative_path" >&2
    exit 2
  fi
done

mkdir -p "$BOOTSTRAP_ROOT/logs"
LOG_FILE=$BOOTSTRAP_ROOT/logs/fresh_swt_bootstrap_$(date +%Y%m%d_%H%M%S).log
exec > >(tee -a "$LOG_FILE") 2>&1

if command -v flock >/dev/null 2>&1; then
  exec 9>"$BOOTSTRAP_ROOT/fresh_swt_bootstrap.lock"
  if ! flock -n 9; then
    echo "Another fresh-server bootstrap is already running." >&2
    exit 73
  fi
fi

echo "== BRT6 fresh SWT server bootstrap =="
echo "project_root=$PROJECT_ROOT"
echo "workspace_root=$WORKSPACE_ROOT"
echo "repository_root=$REPO_ROOT"
echo "log=$LOG_FILE"

# A full preparation retains twelve Git repositories, package caches, and 52
# dependency templates. Refuse obviously undersized disks before downloads.
AVAILABLE_KB=$(df -Pk "$WORKSPACE_ROOT" | awk 'NR == 2 {print $4}')
AVAILABLE_INODES=$(df -Pi "$WORKSPACE_ROOT" | awk 'NR == 2 {print $4}')
MINIMUM_KB=$((50 * 1024 * 1024))
if [[ ! "$AVAILABLE_KB" =~ ^[0-9]+$ || "$AVAILABLE_KB" -lt "$MINIMUM_KB" ]]; then
  echo "At least 50 GiB of free space is required under $WORKSPACE_ROOT." >&2
  echo "available_kb=${AVAILABLE_KB:-unknown}" >&2
  exit 2
fi
if [[ "$AVAILABLE_INODES" =~ ^[0-9]+$ && "$AVAILABLE_INODES" -lt 200000 ]]; then
  echo "At least 200000 free inodes are required under $WORKSPACE_ROOT." >&2
  echo "available_inodes=$AVAILABLE_INODES" >&2
  exit 2
fi
if [[ "$AVAILABLE_KB" -lt $((100 * 1024 * 1024)) ]]; then
  echo "WARNING: less than 100 GiB is free; 100+ GiB is recommended."
fi

if [[ "$INSTALL_SYSTEM_PACKAGES" == true ]]; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get is unavailable; install equivalent prerequisites and rerun with --skip-system-packages." >&2
    exit 2
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    ROOT_COMMAND=()
  elif command -v sudo >/dev/null 2>&1; then
    ROOT_COMMAND=(sudo)
  else
    echo "System package installation needs root or sudo." >&2
    exit 2
  fi

  echo "== Install operating-system prerequisites =="
  APT_UPDATED=false
  for attempt in 1 2 3; do
    if "${ROOT_COMMAND[@]}" apt-get update; then
      APT_UPDATED=true
      break
    fi
    echo "apt-get update failed (attempt $attempt/3)."
    sleep $((attempt * 5))
  done
  if [[ "$APT_UPDATED" != true ]]; then
    echo "apt-get update failed after three attempts." >&2
    exit 2
  fi
  "${ROOT_COMMAND[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    autoconf automake bash bison build-essential ca-certificates cmake curl \
    file flex gfortran git gettext hunspell-en-us libbz2-dev libenchant-2-dev \
    libffi-dev libfreetype6-dev libjpeg-dev liblapack-dev liblzma-dev \
    libopenblas-dev libpng-dev libreadline-dev libsqlite3-dev libssl-dev \
    libtool libxml2-dev libxslt1-dev locales make ninja-build patch pkg-config \
    procps rsync tmux unzip util-linux xz-utils zip zlib1g-dev

  if [[ -f /etc/locale.gen ]]; then
    "${ROOT_COMMAND[@]}" sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
    "${ROOT_COMMAND[@]}" locale-gen en_US.UTF-8
  fi
fi

echo "== Locate and validate the existing Conda =="
conda_candidates=()
if [[ -n "$REQUESTED_CONDA_EXE" ]]; then
  conda_candidates+=("$REQUESTED_CONDA_EXE")
fi
if command -v conda >/dev/null 2>&1; then
  conda_candidates+=("$(command -v conda)")
fi
conda_candidates+=(
  "$HOME/miniforge3/bin/conda"
  "$HOME/miniconda3/bin/conda"
  "$HOME/anaconda3/bin/conda"
  /opt/conda/bin/conda
  /root/conda/ENTER/bin/conda
)
CONDA_EXE_RESOLVED=
for candidate in "${conda_candidates[@]}"; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    CONDA_EXE_RESOLVED=$(cd "$(dirname "$candidate")" && pwd)/$(basename "$candidate")
    break
  fi
done
if [[ -z "$CONDA_EXE_RESOLVED" ]]; then
  echo "No executable Conda was found. Pass --conda-exe /absolute/path/to/conda." >&2
  exit 2
fi

# Ignore inherited activation state and use one explicit Conda for every
# controller, template, generation, and evaluation command.
unset CONDA_PREFIX CONDA_PREFIX_1 CONDA_PREFIX_2 CONDA_PREFIX_3
unset CONDA_PREFIX_4 CONDA_PREFIX_5 CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER
unset CONDA_SHLVL CONDA_PYTHON_EXE _CE_CONDA _CE_M PYTHONSTARTUP
export CONDA_EXE=$CONDA_EXE_RESOLVED
export PATH="$(dirname "$CONDA_EXE"):$PATH"
export CONDA_NO_PLUGINS=true
export CONDA_SOLVER=classic
mkdir -p "$CONDA_STORAGE_ROOT/envs" "$CONDA_STORAGE_ROOT/pkgs"
export CONDA_ENVS_PATH=$CONDA_STORAGE_ROOT/envs
export CONDA_PKGS_DIRS=$CONDA_STORAGE_ROOT/pkgs
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

CONDA_INFO_FILE=$BOOTSTRAP_ROOT/conda_info.json
if ! "$CONDA_EXE" info --json > "$CONDA_INFO_FILE"; then
  echo "The installed Conda cannot run safely, even with plugins disabled." >&2
  echo "Repair that Conda installation or pass a working path with --conda-exe." >&2
  exit 2
fi
CONDA_BASE=$("$CONDA_EXE" info --base)
CONDA_SH=$CONDA_BASE/etc/profile.d/conda.sh
if [[ ! -f "$CONDA_SH" ]]; then
  echo "Conda initialization script is missing: $CONDA_SH" >&2
  exit 2
fi
export BRT3_CONDA_SH=$CONDA_SH
echo "conda=$CONDA_EXE"
echo "conda_base=$CONDA_BASE"
"$CONDA_EXE" --version

# Keep this project's channel and solver policy out of the user's global
# .condarc. conda-forge retains the legacy Python versions required by SWT.
CONDARC_FILE=$BOOTSTRAP_ROOT/condarc
cat > "$CONDARC_FILE" <<'EOF'
channels:
  - conda-forge
channel_priority: strict
show_channel_urls: true
auto_activate_base: false
number_channel_notices: false
remote_connect_timeout_secs: 30
remote_read_timeout_secs: 120
remote_max_retries: 5
EOF
export CONDARC=$CONDARC_FILE

# Probe the oldest Python required by the 276-row SWT dependency contracts.
PROBE_PARENT=$(mktemp -d "$BOOTSTRAP_ROOT/conda-probe.XXXXXX")
PROBE_PREFIX=$PROBE_PARENT/python36
cleanup_probe() {
  set +e
  if [[ -d "$PROBE_PREFIX" ]]; then
    "$CONDA_EXE" env remove -p "$PROBE_PREFIX" -y >/dev/null 2>&1
  fi
  rmdir "$PROBE_PARENT" >/dev/null 2>&1 || true
}
trap cleanup_probe EXIT
"$CONDA_EXE" create -p "$PROBE_PREFIX" -y python=3.6 pip
"$CONDA_EXE" run -p "$PROBE_PREFIX" python -c \
  'import sys; assert sys.version_info[:2] == (3, 6); print("legacy_python_probe=" + sys.version)'
"$CONDA_EXE" env remove -p "$PROBE_PREFIX" -y
# Some Conda releases remove the now-empty parent directory together with the
# environment prefix.  Cleanup is complete in either case.
rmdir "$PROBE_PARENT" >/dev/null 2>&1 || true
trap - EXIT

if [[ -n "$REQUESTED_CONTROLLER_PYTHON" ]]; then
  echo "== Validate the user-provided BRT5 controller environment =="
  if [[ ! -x "$REQUESTED_CONTROLLER_PYTHON" ]]; then
    echo "Controller Python is not executable: $REQUESTED_CONTROLLER_PYTHON" >&2
    exit 2
  fi
  PYTHON_BIN=$(cd "$(dirname "$REQUESTED_CONTROLLER_PYTHON")" && pwd)/$(basename "$REQUESTED_CONTROLLER_PYTHON")
  "$PYTHON_BIN" -m pip check
  "$PYTHON_BIN" -c \
    'import datasets, packaging, requests, sys; assert sys.version_info >= (3, 10), sys.version; print("reused_controller_python=" + sys.executable)'
else
  echo "== Create the BRT5 controller environment =="
  if "$CONDA_EXE" run -n "$CONTROLLER_ENV" python -c 'import sys' >/dev/null 2>&1; then
    "$CONDA_EXE" install -n "$CONTROLLER_ENV" -y python=3.12 pip
  else
    "$CONDA_EXE" create -n "$CONTROLLER_ENV" -y python=3.12 pip
  fi
  CONTROLLER_PREFIX=$(
    "$CONDA_EXE" run -n "$CONTROLLER_ENV" python -c 'import sys; print(sys.prefix)' |
      tail -n 1 | tr -d '\r'
  )
  PYTHON_BIN=$CONTROLLER_PREFIX/bin/python
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Controller Python was not created: $PYTHON_BIN" >&2
    exit 2
  fi
  "$PYTHON_BIN" -m pip install --upgrade \
    pip==26.1.2 setuptools==82.0.1 wheel==0.47.0
  "$PYTHON_BIN" -m pip install -r "$PROJECT_ROOT/requirements-reproduce.txt"
  "$PYTHON_BIN" -m pip check
  "$PYTHON_BIN" -c \
    'import datasets, packaging, requests, sys; assert sys.version_info[:2] == (3, 12); print("controller_python=" + sys.executable)'
fi

echo "== Create the official SWT harness environment =="
if "$CONDA_EXE" run -n "$OFFICIAL_SWT_ENV" python -c 'import sys' >/dev/null 2>&1; then
  "$CONDA_EXE" install -n "$OFFICIAL_SWT_ENV" -y python=3.12 pip
else
  "$CONDA_EXE" create -n "$OFFICIAL_SWT_ENV" -y python=3.12 pip
fi
OFFICIAL_SWT_PREFIX=$(
  "$CONDA_EXE" run -n "$OFFICIAL_SWT_ENV" python -c 'import sys; print(sys.prefix)' |
    tail -n 1 | tr -d '\r'
)
SWTBENCH_PYTHON=$OFFICIAL_SWT_PREFIX/bin/python
if [[ ! -x "$SWTBENCH_PYTHON" ]]; then
  echo "Official SWT Python was not created: $SWTBENCH_PYTHON" >&2
  exit 2
fi
"$SWTBENCH_PYTHON" -m pip install --upgrade \
  pip==26.1.2 setuptools==82.0.1 wheel==0.47.0
"$SWTBENCH_PYTHON" -m pip install -r "$PROJECT_ROOT/requirements-reproduce.txt"
PYTHONPATH="$PROJECT_ROOT/evaluation/vendor/swtbench" \
  "$SWTBENCH_PYTHON" -c 'import docker, src.main, unidiff; print("official_swt_python=ready")'

export PYTHON_BIN
export SWTBENCH_PYTHON
export PYTHONPATH="$WORKSPACE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export BRT_WORKSPACE_ROOT=$WORKSPACE_ROOT
export BRT4_CONDA_ENV_PREFIX=$ENV_PREFIX
export BRT4_ENV_CACHE_DIR=$BOOTSTRAP_ROOT/environment-cache
export REPO_ROOT

echo "== Configure and validate API credentials =="
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi
# A local .env is allowed to provide credentials, but it must not redirect the
# already validated runtime to another Conda or workspace.
export CONDA_EXE=$CONDA_EXE_RESOLVED
export BRT3_CONDA_SH=$CONDA_SH
export CONDARC=$CONDARC_FILE
export CONDA_NO_PLUGINS=true
export CONDA_SOLVER=classic
export CONDA_ENVS_PATH=$CONDA_STORAGE_ROOT/envs
export CONDA_PKGS_DIRS=$CONDA_STORAGE_ROOT/pkgs
export PYTHON_BIN
export PYTHONPATH="$WORKSPACE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export BRT_WORKSPACE_ROOT=$WORKSPACE_ROOT
export BRT4_CONDA_ENV_PREFIX=$ENV_PREFIX
export BRT4_ENV_CACHE_DIR=$BOOTSTRAP_ROOT/environment-cache
export REPO_ROOT
API_POOL_FILE=$PROJECT_ROOT/.secrets/api_pool.json
if [[ -f "$API_POOL_FILE" ]]; then
  chmod 600 "$API_POOL_FILE"
elif [[ -n "${DEEPSEEK_API_KEYS:-${DEEPSEEK_API_KEY:-}}" ]]; then
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/configure_api_keys.py" --from-env
elif "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
from brt6.llm.api_pool import configured_apis
raise SystemExit(0 if configured_apis() else 1)
PY
then
  : # An explicit BRT_API_POOL_FILE/BRT_API_POOL_JSON is already usable.
elif [[ "$NON_INTERACTIVE" == true || ! -t 0 ]]; then
  echo "No API key is configured." >&2
  echo "Set DEEPSEEK_API_KEYS, or rerun interactively to enter keys securely." >&2
  exit 3
else
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/configure_api_keys.py"
fi
API_COUNT=$(
  "$PYTHON_BIN" - <<'PY'
from brt6.llm.api_pool import configured_apis

entries = configured_apis()
bad_markers = ("replace", "placeholder", "example", "dummy", "your_key")
if not entries or any(any(marker in key.lower() for marker in bad_markers) for key, _, _ in entries):
    raise SystemExit("API configuration is empty or still contains a placeholder")
print(len(entries))
PY
)
echo "api_pool_entries=$API_COUNT"

echo "== Clone and verify all SWT repositories and commits =="
mkdir -p "$REPO_ROOT"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/bootstrap_repositories.py" \
  --dataset "$PROJECT_ROOT/data/issues/swt276_issues.json" \
  --repo-root "$REPO_ROOT" \
  --manifest "$BOOTSTRAP_ROOT/repositories.json"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/bootstrap_repositories.py" \
  --dataset "$PROJECT_ROOT/data/issues/swt276_issues.json" \
  --repo-root "$REPO_ROOT" \
  --check-only \
  --manifest "$BOOTSTRAP_ROOT/repositories.check.json"

echo "== Write the stable runtime environment =="
{
  echo '# Generated by bootstrap_fresh_swt_server.sh; source before SWT runs.'
  echo 'unset CONDA_PREFIX CONDA_PREFIX_1 CONDA_PREFIX_2 CONDA_PREFIX_3'
  echo 'unset CONDA_PREFIX_4 CONDA_PREFIX_5 CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER'
  echo 'unset CONDA_SHLVL CONDA_PYTHON_EXE _CE_CONDA _CE_M PYTHONSTARTUP'
  printf 'export CONDA_EXE=%q\n' "$CONDA_EXE"
  printf 'export BRT3_CONDA_SH=%q\n' "$CONDA_SH"
  printf 'export CONDARC=%q\n' "$CONDARC_FILE"
  printf 'export CONDA_NO_PLUGINS=true\n'
  printf 'export CONDA_SOLVER=classic\n'
  printf 'export CONDA_ENVS_PATH=%q\n' "$CONDA_ENVS_PATH"
  printf 'export CONDA_PKGS_DIRS=%q\n' "$CONDA_PKGS_DIRS"
  printf 'export PYTHON_BIN=%q\n' "$PYTHON_BIN"
  printf 'export SWTBENCH_PYTHON=%q\n' "$SWTBENCH_PYTHON"
  printf 'export BRT_WORKSPACE_ROOT=%q\n' "$WORKSPACE_ROOT"
  printf 'export BRT4_CONDA_ENV_PREFIX=%q\n' "$ENV_PREFIX"
  printf 'export BRT4_ENV_CACHE_DIR=%q\n' "$BRT4_ENV_CACHE_DIR"
  printf 'export REPO_ROOT=%q\n' "$REPO_ROOT"
  printf 'export LANG=en_US.UTF-8\n'
  printf 'export LC_ALL=en_US.UTF-8\n'
  printf 'export PATH=%q:$PATH\n' "$(dirname "$CONDA_EXE")"
  printf 'export PYTHONPATH=%q${PYTHONPATH:+:$PYTHONPATH}\n' "$WORKSPACE_ROOT"
} > "$RUNTIME_ENV_FILE"
chmod 600 "$RUNTIME_ENV_FILE"

if [[ "$PREWARM" == true ]]; then
  echo "== Prebuild all 52 SWT dependency-template environments =="
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/prewarm_swt_environments.py" \
    --dataset "$PROJECT_ROOT/data/issues/swt276_issues.json" \
    --work-root "$BOOTSTRAP_ROOT/swt-template-environments" \
    --timeout 3600 \
    --retries 3 \
    --workers "$PREWARM_WORKERS"
else
  echo "WARNING: --skip-prewarm was used; this server is not yet ready for a full run."
fi

echo "== Validate frozen experiment inputs =="
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/validate_behavior_target_cache.py" \
  --cache-dir "$PROJECT_ROOT/data/behavior_targets/swt/full_method_f2p_47_46_20260717" \
  --instances-path "$PROJECT_ROOT/data/issues/swt276_issues.json" \
  --dataset-mode swt \
  --code-retrieval-path "$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json" \
  --test-retrieval-path "$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json" \
  --output-path "$BOOTSTRAP_ROOT/behavior_target_cache_validation.json"

if [[ "$PREWARM" == true ]]; then
  "$PYTHON_BIN" - "$BOOTSTRAP_ROOT/swt-template-environments/summary.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
summary = json.loads(path.read_text(encoding="utf-8"))
if summary.get("total") != 52 or summary.get("ready_count") != 52 or summary.get("failed_count") != 0:
    raise SystemExit(f"SWT template prewarm is incomplete: {summary}")
print("template_environment_gate=52/52_ready")
PY
fi

echo "== Run local syntax and regression checks =="
bash -n \
  "$PROJECT_ROOT/scripts/bootstrap_fresh_swt_server.sh" \
  "$PROJECT_ROOT/scripts/run_swt_experiment.sh" \
  "$PROJECT_ROOT/scripts/run_p0_simple_llm_selector_full.sh"
"$PYTHON_BIN" -m compileall -q \
  "$PROJECT_ROOT/core" "$PROJECT_ROOT/evaluation" "$PROJECT_ROOT/execution" \
  "$PROJECT_ROOT/generation" "$PROJECT_ROOT/llm" "$PROJECT_ROOT/mutation" \
  "$PROJECT_ROOT/pipeline" "$PROJECT_ROOT/retrieval" "$PROJECT_ROOT/runtime" \
  "$PROJECT_ROOT/scripts" "$PROJECT_ROOT/validation"
if [[ "$RUN_TESTS" == true ]]; then
  cd "$WORKSPACE_ROOT"
  "$PYTHON_BIN" -m unittest discover -s brt6/tests -v
fi

TRACKED_STATUS=$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=no || true)
if [[ -n "$TRACKED_STATUS" ]]; then
  echo "WARNING: tracked source changes are present."
  echo "$TRACKED_STATUS"
  echo "The paper-facing launcher will require these changes to be reviewed and committed."
fi

cat <<EOF

Fresh SWT server preparation completed successfully.
Conda:              $CONDA_EXE
Controller Python:  $PYTHON_BIN
Repository cache:   $REPO_ROOT
Conda env/package storage: $CONDA_STORAGE_ROOT
Prewarm workers:     $PREWARM_WORKERS
Runtime environment:$RUNTIME_ENV_FILE
Bootstrap log:      $LOG_FILE

Run the frozen full-method experiment with:
  cd $PROJECT_ROOT
  bash scripts/run_swt_experiment.sh \\
    --behavior-target on \\
    --behavior-target-cache data/behavior_targets/swt/full_method_f2p_47_46_20260717
EOF

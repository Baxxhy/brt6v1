#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}
PACKAGE_ROOT=${PACKAGE_ROOT:-$(cd "$PROJECT_ROOT/.." && pwd)}

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

usage() {
  cat <<'EOF'
Usage: bash scripts/run_p0_simple_llm_selector_full.sh --dataset {swt|tdd} [ablation switch]

Options:
  --dataset {swt|tdd}  Select the experiment dataset (default: swt).
  --model {deepseek|gpt}
                       Select the isolated LLM/API pool (default: deepseek).
                       gpt resolves to gpt-5.4-mini by default.
  --behavior-target {on|off}
                       Enable BehaviorTarget (default: on). Use off for the
                       "w/o Behavior Target" ablation; IssueRewrite is skipped.
  --behavior-target-cache PATH
                       Reuse a frozen, versioned BehaviorTarget cache and skip
                       IssueRewrite. If omitted, BehaviorTarget is regenerated.
  --mutation {on|off}  Enable explicit mutation planning (default: on).
  --specialized-feedback {on|off}
                       Enable setup/trigger/assertion-specific feedback (default: on).
  --environment-feedback {on|off}
                       Enable candidate-level setup/dependency repair (default: on).
  --trigger-feedback {on|off}
                       Enable trigger repair feedback (default: on).
  --assertion-feedback {on|off}
                       Enable observation/assertion repair feedback (default: on).
  --semantic-delta {on|off}
                       Enable preserve/change/avoid search state (default: on).
  -h, --help           Show this help message.

At most one component may be off. Any ablation runs F2P only; the all-on
configuration keeps the full F2P + Patch Coverage evaluation. Generation
feedback and formal scoring always use the official SWTBench or TDDBench
Docker harness; BRT6 never creates or repairs a host project environment.

DATASET_MODE, INSTANCES_PATH, GOLD_DATASET, and RUN_DIR may still be
overridden through environment variables.
EOF
}

DATASET_MODE=${DATASET_MODE:-swt}
LLM_PROVIDER=${LLM_PROVIDER:-deepseek}
ENABLE_BEHAVIOR_TARGET=${ENABLE_BEHAVIOR_TARGET:-true}
ENABLE_MUTATION=${ENABLE_MUTATION:-true}
ENABLE_SPECIALIZED_FEEDBACK=${ENABLE_SPECIALIZED_FEEDBACK:-true}
ENABLE_ENVIRONMENT_FEEDBACK=${ENABLE_ENVIRONMENT_FEEDBACK:-true}
ENABLE_TRIGGER_FEEDBACK=${ENABLE_TRIGGER_FEEDBACK:-true}
ENABLE_ASSERTION_FEEDBACK=${ENABLE_ASSERTION_FEEDBACK:-true}
ENABLE_SEMANTIC_DELTA=${ENABLE_SEMANTIC_DELTA:-true}
BEHAVIOR_TARGET_CACHE=${BEHAVIOR_TARGET_CACHE:-}

normalize_switch() {
  case "$1" in
    on|true|1) printf 'true' ;;
    off|false|0) printf 'false' ;;
    *) return 1 ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset)
      if [[ $# -lt 2 ]]; then
        echo "--dataset requires swt or tdd" >&2
        usage >&2
        exit 2
      fi
      DATASET_MODE=$2
      shift 2
      ;;
    --dataset=*)
      DATASET_MODE=${1#*=}
      shift
      ;;
    --model)
      if [[ $# -lt 2 ]]; then
        echo "--model requires deepseek or gpt" >&2
        usage >&2
        exit 2
      fi
      LLM_PROVIDER=$2
      shift 2
      ;;
    --model=*)
      LLM_PROVIDER=${1#*=}
      shift
      ;;
    --behavior-target)
      if [[ $# -lt 2 ]]; then
        echo "--behavior-target requires on or off" >&2
        usage >&2
        exit 2
      fi
      case "$2" in
        on|true|1) ENABLE_BEHAVIOR_TARGET=true ;;
        off|false|0) ENABLE_BEHAVIOR_TARGET=false ;;
        *)
          echo "invalid --behavior-target value '$2': expected on or off" >&2
          exit 2
          ;;
      esac
      shift 2
      ;;
    --behavior-target=*)
      BEHAVIOR_TARGET_VALUE=${1#*=}
      case "$BEHAVIOR_TARGET_VALUE" in
        on|true|1) ENABLE_BEHAVIOR_TARGET=true ;;
        off|false|0) ENABLE_BEHAVIOR_TARGET=false ;;
        *)
          echo "invalid --behavior-target value '$BEHAVIOR_TARGET_VALUE': expected on or off" >&2
          exit 2
          ;;
      esac
      shift
      ;;
    --behavior-target-cache)
      if [[ $# -lt 2 ]]; then
        echo "--behavior-target-cache requires a directory path" >&2
        usage >&2
        exit 2
      fi
      BEHAVIOR_TARGET_CACHE=$2
      shift 2
      ;;
    --behavior-target-cache=*)
      BEHAVIOR_TARGET_CACHE=${1#*=}
      if [[ -z "$BEHAVIOR_TARGET_CACHE" ]]; then
        echo "--behavior-target-cache requires a non-empty directory path" >&2
        exit 2
      fi
      shift
      ;;
    --mutation|--specialized-feedback|--environment-feedback|--trigger-feedback|--assertion-feedback|--semantic-delta)
      if [[ $# -lt 2 ]]; then
        echo "$1 requires on or off" >&2
        usage >&2
        exit 2
      fi
      NORMALIZED=$(normalize_switch "$2") || {
        echo "invalid $1 value '$2': expected on or off" >&2
        exit 2
      }
      case "$1" in
        --mutation) ENABLE_MUTATION=$NORMALIZED ;;
        --specialized-feedback) ENABLE_SPECIALIZED_FEEDBACK=$NORMALIZED ;;
        --environment-feedback) ENABLE_ENVIRONMENT_FEEDBACK=$NORMALIZED ;;
        --trigger-feedback) ENABLE_TRIGGER_FEEDBACK=$NORMALIZED ;;
        --assertion-feedback) ENABLE_ASSERTION_FEEDBACK=$NORMALIZED ;;
        --semantic-delta) ENABLE_SEMANTIC_DELTA=$NORMALIZED ;;
      esac
      shift 2
      ;;
    --mutation=*|--specialized-feedback=*|--environment-feedback=*|--trigger-feedback=*|--assertion-feedback=*|--semantic-delta=*)
      OPTION_NAME=${1%%=*}
      OPTION_VALUE=${1#*=}
      NORMALIZED=$(normalize_switch "$OPTION_VALUE") || {
        echo "invalid $OPTION_NAME value '$OPTION_VALUE': expected on or off" >&2
        exit 2
      }
      case "$OPTION_NAME" in
        --mutation) ENABLE_MUTATION=$NORMALIZED ;;
        --specialized-feedback) ENABLE_SPECIALIZED_FEEDBACK=$NORMALIZED ;;
        --environment-feedback) ENABLE_ENVIRONMENT_FEEDBACK=$NORMALIZED ;;
        --trigger-feedback) ENABLE_TRIGGER_FEEDBACK=$NORMALIZED ;;
        --assertion-feedback) ENABLE_ASSERTION_FEEDBACK=$NORMALIZED ;;
        --semantic-delta) ENABLE_SEMANTIC_DELTA=$NORMALIZED ;;
      esac
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$LLM_PROVIDER" in
  deepseek|gpt) ;;
  *)
    echo "invalid --model value '$LLM_PROVIDER': expected deepseek or gpt" >&2
    exit 2
    ;;
esac

for SWITCH_NAME in \
  ENABLE_BEHAVIOR_TARGET ENABLE_MUTATION ENABLE_SPECIALIZED_FEEDBACK \
  ENABLE_ENVIRONMENT_FEEDBACK ENABLE_TRIGGER_FEEDBACK ENABLE_ASSERTION_FEEDBACK; do
  SWITCH_VALUE=${!SWITCH_NAME}
  NORMALIZED=$(normalize_switch "$SWITCH_VALUE") || {
    echo "invalid $SWITCH_NAME='$SWITCH_VALUE': expected on or off" >&2
    exit 2
  }
  printf -v "$SWITCH_NAME" '%s' "$NORMALIZED"
done

OFF_COUNT=0
for SWITCH_VALUE in \
  "$ENABLE_BEHAVIOR_TARGET" "$ENABLE_MUTATION" "$ENABLE_SPECIALIZED_FEEDBACK" \
  "$ENABLE_ENVIRONMENT_FEEDBACK" "$ENABLE_TRIGGER_FEEDBACK" "$ENABLE_ASSERTION_FEEDBACK"; do
  if [[ "$SWITCH_VALUE" == "false" ]]; then
    OFF_COUNT=$((OFF_COUNT + 1))
  fi
done
if [[ "$OFF_COUNT" -gt 1 ]]; then
  echo "ablation switches are mutually exclusive; at most one component may be off" >&2
  exit 2
fi
if [[ -n "$BEHAVIOR_TARGET_CACHE" && "$ENABLE_BEHAVIOR_TARGET" == "false" ]]; then
  echo "--behavior-target-cache cannot be combined with --behavior-target off" >&2
  exit 2
fi
if [[ -n "$BEHAVIOR_TARGET_CACHE" && "$BEHAVIOR_TARGET_CACHE" != /* ]]; then
  BEHAVIOR_TARGET_CACHE=$PROJECT_ROOT/$BEHAVIOR_TARGET_CACHE
fi

CONDA_EXE=${CONDA_EXE:-}
if [[ -z "$CONDA_EXE" ]] && command -v conda >/dev/null 2>&1; then
  CONDA_EXE=$(command -v conda)
fi
for CANDIDATE in \
  "$HOME/miniforge3/bin/conda" \
  "$HOME/miniconda3/bin/conda" \
  "/root/conda/ENTER/bin/conda"; do
  if [[ -z "$CONDA_EXE" && -x "$CANDIDATE" ]]; then
    CONDA_EXE=$CANDIDATE
  fi
done
if [[ -z "$CONDA_EXE" || ! -x "$CONDA_EXE" ]]; then
  echo "Conda is unavailable. Run: bash scripts/bootstrap_machine.sh" >&2
  exit 2
fi
CONDA_BASE=$("$CONDA_EXE" info --base)
export CONDA_EXE
export BRT3_CONDA_SH=${BRT3_CONDA_SH:-$CONDA_BASE/etc/profile.d/conda.sh}
export BRT_WORKSPACE_ROOT=${BRT_WORKSPACE_ROOT:-$PACKAGE_ROOT}
export DOCKER_HOST=${DOCKER_HOST:-unix:///run/mutate-docker.sock}
export PATH="$(dirname "$CONDA_EXE"):$PATH"
export PYTHONPATH="$PACKAGE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN=${PYTHON_BIN:-$CONDA_BASE/envs/icore/bin/python}
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Framework environment 'icore' is unavailable. Run: bash scripts/bootstrap_machine.sh" >&2
  exit 2
fi

if [[ "$DATASET_MODE" == "swt" ]]; then
  DEFAULT_OFFICIAL_PYTHON=$CONDA_BASE/envs/swtbench/bin/python
  [[ -x "$DEFAULT_OFFICIAL_PYTHON" ]] || DEFAULT_OFFICIAL_PYTHON=$CONDA_BASE/envs/brt6_swtbench/bin/python
  OFFICIAL_PYTHON=${SWTBENCH_PYTHON:-$DEFAULT_OFFICIAL_PYTHON}
else
  OFFICIAL_PYTHON=${TDDBENCH_PYTHON:-$CONDA_BASE/envs/brt6_tddbench/bin/python}
fi
if [[ ! -x "$OFFICIAL_PYTHON" ]]; then
  echo "Official $DATASET_MODE harness environment is unavailable: $OFFICIAL_PYTHON" >&2
  echo "Run: bash scripts/bootstrap_official_benchmarks.sh" >&2
  exit 2
fi

case "$DATASET_MODE" in
  swt)
    DEFAULT_INSTANCES_PATH=$PROJECT_ROOT/data/issues/swt276_issues.json
    DEFAULT_GOLD_DATASET=$PROJECT_ROOT/data/official/swt276_official_eval.json
    ;;
  tdd)
    DEFAULT_INSTANCES_PATH=$PACKAGE_ROOT/TDD-Bench-Verified/TDD_Bench.json
    DEFAULT_GOLD_DATASET=$DEFAULT_INSTANCES_PATH
    ;;
  *)
    echo "invalid dataset '$DATASET_MODE': expected swt or tdd" >&2
    exit 2
    ;;
esac

RUN_TIMESTAMP=${RUN_TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
RUN_VARIANT_SUFFIX=""
COMPUTE_PATCH_COVERAGE=${COMPUTE_PATCH_COVERAGE:-true}
ABLATION_ID=full
METHOD_VARIANT=full
if [ "$ENABLE_BEHAVIOR_TARGET" = "false" ]; then
  RUN_VARIANT_SUFFIX="_wo_behavior_target"
  ABLATION_ID=wo_behavior_target
  METHOD_VARIANT="w/o Behavior Target"
elif [ "$ENABLE_MUTATION" = "false" ]; then
  RUN_VARIANT_SUFFIX="_wo_mutation"
  ABLATION_ID=wo_mutation
  METHOD_VARIANT="w/o Mutation"
elif [ "$ENABLE_SPECIALIZED_FEEDBACK" = "false" ]; then
  RUN_VARIANT_SUFFIX="_generic_iteration"
  ABLATION_ID=generic_iteration
  METHOD_VARIANT="Generic Iteration"
elif [ "$ENABLE_ENVIRONMENT_FEEDBACK" = "false" ]; then
  RUN_VARIANT_SUFFIX="_wo_environment_feedback"
  ABLATION_ID=wo_environment_feedback
  METHOD_VARIANT="w/o Environment Feedback"
elif [ "$ENABLE_TRIGGER_FEEDBACK" = "false" ]; then
  RUN_VARIANT_SUFFIX="_wo_trigger_feedback"
  ABLATION_ID=wo_trigger_feedback
  METHOD_VARIANT="w/o Trigger Feedback"
elif [ "$ENABLE_ASSERTION_FEEDBACK" = "false" ]; then
  RUN_VARIANT_SUFFIX="_wo_assertion_feedback"
  ABLATION_ID=wo_assertion_feedback
  METHOD_VARIANT="w/o Assertion Feedback"
elif [ "$ENABLE_SEMANTIC_DELTA" = "false" ]; then
  RUN_VARIANT_SUFFIX="_wo_semantic_delta"
  ABLATION_ID=wo_semantic_delta
  METHOD_VARIANT="w/o Semantic Delta Contract"
fi
if [ "$ABLATION_ID" != "full" ]; then
  COMPUTE_PATCH_COVERAGE=false
fi
RUN_DIR=${RUN_DIR:-$PROJECT_ROOT/results/runs/p0_simple_llm_selector_${DATASET_MODE}${RUN_VARIANT_SUFFIX}_${RUN_TIMESTAMP}}
export TMPDIR=${TMPDIR:-"$RUN_DIR/tmp"}
export TEMP=${TEMP:-"$RUN_DIR/tmp"}
export TMP=${TMP:-"$RUN_DIR/tmp"}
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-"$RUN_DIR/cache"}
INSTANCES_PATH=${INSTANCES_PATH:-$DEFAULT_INSTANCES_PATH}
GOLD_DATASET=${GOLD_DATASET:-$DEFAULT_GOLD_DATASET}
CODE_RETRIEVAL=${CODE_RETRIEVAL:-$PROJECT_ROOT/retrieval_results/code/code_retrieval_results_gpt.json}
TEST_RETRIEVAL=${TEST_RETRIEVAL:-$PROJECT_ROOT/retrieval_results/test/icore/gpt/related_tests.json}
REPO_ROOT=${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}
if [[ -n "${BRT_MODEL_ID:-}" ]]; then
  MODEL=$BRT_MODEL_ID
elif [[ "$LLM_PROVIDER" == "gpt" ]]; then
  MODEL=${GPT_MODEL:-gpt-5.4-mini}
else
  MODEL=${MODEL:-${DEEPSEEK_MODEL:-deepseek-v4-flash}}
fi
export BRT_LLM_PROVIDER=$LLM_PROVIDER
ISSUE_WORKERS=${ISSUE_WORKERS:-6}
GENERATION_WORKERS=${GENERATION_WORKERS:-6}
EVALUATION_WORKERS=${EVALUATION_WORKERS:-6}
MAX_SEMANTIC_ROUNDS=${MAX_SEMANTIC_ROUNDS:-5}
if [[ ! "$MAX_SEMANTIC_ROUNDS" =~ ^([1-9]|10)$ ]]; then
  echo "MAX_SEMANTIC_ROUNDS must be an integer from 1 to 10" >&2
  exit 2
fi
RUNTIME_BACKEND=${RUNTIME_BACKEND:-official_docker}
SWTBENCH_ROOT_VALUE=${SWTBENCH_ROOT:-$PROJECT_ROOT/evaluation/vendor/swtbench}
if [[ "$RUNTIME_BACKEND" != "official_docker" ]]; then
  echo "brt6 generation only supports RUNTIME_BACKEND=official_docker" >&2
  exit 2
fi
export OFFICIAL_HARNESS_PYTHON=$OFFICIAL_PYTHON

API_POOL_SIZE=$("$PYTHON_BIN" - "$LLM_PROVIDER" <<'PY'
import sys
from brt6.llm.api_pool import configured_apis
print(len(configured_apis(sys.argv[1])))
PY
)
if [[ "$API_POOL_SIZE" -eq 0 ]]; then
  echo "No $LLM_PROVIDER API key is configured. Run: $PYTHON_BIN $PROJECT_ROOT/scripts/configure_api_keys.py --provider $LLM_PROVIDER" >&2
  exit 3
fi
echo "api_pool_provider=$LLM_PROVIDER"
echo "api_pool_entries=$API_POOL_SIZE"

DATASET_SIZE=$("$PYTHON_BIN" - "$INSTANCES_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
if isinstance(data, list):
    rows = [row for row in data if isinstance(row, dict)]
elif isinstance(data, dict):
    rows = [row for row in data.values() if isinstance(row, dict)]
else:
    raise SystemExit(f"unsupported dataset structure in {path}")
instance_ids = [str(row.get("instance_id") or "") for row in rows]
if not rows or any(not instance_id for instance_id in instance_ids):
    raise SystemExit(f"dataset contains empty or invalid rows: {path}")
if len(instance_ids) != len(set(instance_ids)):
    raise SystemExit(f"dataset contains duplicate instance_id values: {path}")
print(len(rows))
PY
) || exit $?

ISSUE_DIR=$RUN_DIR/issue_rewrite
GENERATION_DIR=$RUN_DIR/generation
FORMAL_DIR=$RUN_DIR/evaluation/formal_f2p
LOG_DIR=$RUN_DIR/logs
mkdir -p "$ISSUE_DIR" "$GENERATION_DIR" "$FORMAL_DIR" "$LOG_DIR" "$TMPDIR" "$XDG_CACHE_HOME"

GIT_COMMIT=$(git -C "$PROJECT_ROOT" rev-parse HEAD) || exit $?
GIT_BRANCH=$(git -C "$PROJECT_ROOT" branch --show-current) || exit $?
GIT_BRANCH=${GIT_BRANCH:-DETACHED}
GIT_TRACKED_STATUS=$(git -C "$PROJECT_ROOT" status --porcelain --untracked-files=no)
GIT_TRACKED_CLEAN=true
if [[ -n "$GIT_TRACKED_STATUS" ]]; then
  GIT_TRACKED_CLEAN=false
fi
if [[ -n "$GIT_TRACKED_STATUS" && "${BRT_ALLOW_DIRTY_WORKTREE:-0}" != "1" ]]; then
  echo "tracked source changes are present; commit them before a paper-facing run" >&2
  echo "$GIT_TRACKED_STATUS" >&2
  echo "Set BRT_ALLOW_DIRTY_WORKTREE=1 only for a non-comparable development run." >&2
  exit 2
fi
BEHAVIOR_CACHE_VALIDATION_PATH=""
if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
  BEHAVIOR_CACHE_VALIDATION_PATH=$RUN_DIR/behavior_target_cache_validation.json
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/validate_behavior_target_cache.py" \
    --cache-dir "$BEHAVIOR_TARGET_CACHE" \
    --instances-path "$INSTANCES_PATH" \
    --dataset-mode "$DATASET_MODE" \
    --code-retrieval-path "$CODE_RETRIEVAL" \
    --test-retrieval-path "$TEST_RETRIEVAL" \
    --output-path "$BEHAVIOR_CACHE_VALIDATION_PATH" >/dev/null || exit $?
fi

"$PYTHON_BIN" - \
  "$RUN_DIR/run_config.json" \
  "$BEHAVIOR_CACHE_VALIDATION_PATH" \
  "$GIT_COMMIT" \
  "$GIT_BRANCH" \
  "$GIT_TRACKED_CLEAN" <<PY
import json
import sys
from pathlib import Path

behavior_source = {
    "mode": "regenerated" if "$ENABLE_BEHAVIOR_TARGET" == "true" else "disabled",
    "cache_id": "",
    "source_signature": "" if "$ENABLE_BEHAVIOR_TARGET" == "true" else "behavior_target_disabled",
}
if sys.argv[2]:
    behavior_source = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
config = {
    "dataset_mode": "$DATASET_MODE",
    "llm_provider": "$LLM_PROVIDER",
    "llm_model": "$MODEL",
    "behavior_target": "$ENABLE_BEHAVIOR_TARGET" == "true",
    "mutation": "$ENABLE_MUTATION" == "true",
    "specialized_feedback": "$ENABLE_SPECIALIZED_FEEDBACK" == "true",
    "environment_feedback": "$ENABLE_ENVIRONMENT_FEEDBACK" == "true",
    "trigger_feedback": "$ENABLE_TRIGGER_FEEDBACK" == "true",
    "assertion_feedback": "$ENABLE_ASSERTION_FEEDBACK" == "true",
    "ablation_id": "$ABLATION_ID",
    "method_variant": "$METHOD_VARIANT",
    "run_suffix": "$RUN_VARIANT_SUFFIX",
    "patch_cov_enabled": "$COMPUTE_PATCH_COVERAGE" == "true",
    "behavior_target_source": behavior_source,
    "behavior_target_source_signature": behavior_source.get("source_signature", ""),
    "framework_git_commit": sys.argv[3],
    "framework_git_branch": sys.argv[4],
    "tracked_worktree_clean": sys.argv[5] == "true",
    "generation_runtime_backend": "$RUNTIME_BACKEND",
    "host_project_environment_created": False,
    "formal_evaluator": "official_swtbench" if "$DATASET_MODE" == "swt" else "official_tddbench",
    "formal_runtime_backend": "docker",
}
config["ablation_signature"] = ";".join(
    f"{name}={int(config[name])}"
    for name in (
        "behavior_target", "mutation", "specialized_feedback",
        "environment_feedback", "trigger_feedback", "assertion_feedback",
    )
)
Path(sys.argv[1]).write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

exec > >(tee -a "$LOG_DIR/full_pipeline.log") 2>&1

echo "__BRT_STAGE__ start $(date --iso-8601=seconds)"
echo "dataset_mode=$DATASET_MODE"
echo "llm_provider=$LLM_PROVIDER"
echo "llm_model=$MODEL"
echo "instances_path=$INSTANCES_PATH"
echo "dataset_size=$DATASET_SIZE"
echo "framework_git_commit=$GIT_COMMIT"
echo "behavior_target_enabled=$ENABLE_BEHAVIOR_TARGET"
echo "behavior_target_source=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["behavior_target_source"]["mode"])' "$RUN_DIR/run_config.json")"
if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
  echo "behavior_target_cache=$BEHAVIOR_TARGET_CACHE"
fi
echo "mutation_enabled=$ENABLE_MUTATION"
echo "specialized_feedback_enabled=$ENABLE_SPECIALIZED_FEEDBACK"
echo "environment_feedback_enabled=$ENABLE_ENVIRONMENT_FEEDBACK"
echo "trigger_feedback_enabled=$ENABLE_TRIGGER_FEEDBACK"
echo "assertion_feedback_enabled=$ENABLE_ASSERTION_FEEDBACK"
echo "patch_coverage_enabled=$COMPUTE_PATCH_COVERAGE"
echo "ablation_id=$ABLATION_ID"
echo "method_variant=$METHOD_VARIANT"
echo "framework_python=$PYTHON_BIN"
echo "generation_runtime_backend=$RUNTIME_BACKEND"
echo "official_harness_python=$OFFICIAL_PYTHON"
echo "host_project_environment_created=false"
"$PYTHON_BIN" -c 'import sys; print("resolved_framework_python=" + sys.executable)'

cd "$PACKAGE_ROOT"

ISSUE_RC=1
ISSUE_TARGETS=0
if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
  ISSUE_RC=0
  ISSUE_TARGETS=$DATASET_SIZE
  echo "__BRT_STAGE__ issue_rewrite_skipped reason=explicit_behavior_target_cache targets=$ISSUE_TARGETS $(date --iso-8601=seconds)"
elif [ "$ENABLE_BEHAVIOR_TARGET" = "true" ]; then
  echo "__BRT_STAGE__ issue_rewrite_start $(date --iso-8601=seconds)"
  for ISSUE_ATTEMPT in 1 2 3; do
    RESUME_ARGS=()
    if [ "$ISSUE_ATTEMPT" -gt 1 ]; then
      RESUME_ARGS+=(--resume)
    fi
    echo "__BRT_STAGE__ issue_rewrite_attempt=$ISSUE_ATTEMPT"
    "$PYTHON_BIN" -m brt6.pipeline.run_issue_rewrite \
      --instances_path "$INSTANCES_PATH" \
      --code_retrieval_path "$CODE_RETRIEVAL" \
      --test_retrieval_path "$TEST_RETRIEVAL" \
      --output_dir "$ISSUE_DIR" \
      --llm-provider "$LLM_PROVIDER" \
      --model "$MODEL" \
      --max_workers "$ISSUE_WORKERS" \
      --temperature 0.1 \
      --max_tokens 4096 \
      "${RESUME_ARGS[@]}"
    ISSUE_RC=$?
    ISSUE_TARGETS=$("$PYTHON_BIN" - "$INSTANCES_PATH" "$ISSUE_DIR" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows = data if isinstance(data, list) else list(data.values())
issue_dir = Path(sys.argv[2])
print(sum((issue_dir / str(row.get("instance_id")) / "behavior_target.json").is_file() for row in rows))
PY
)
    echo "__BRT_PROGRESS__ issue_targets=$ISSUE_TARGETS/$DATASET_SIZE rc=$ISSUE_RC"
    if [ "$ISSUE_RC" -eq 0 ] && [ "$ISSUE_TARGETS" -eq "$DATASET_SIZE" ]; then
      break
    fi
    sleep 5
  done
  echo "__BRT_STAGE__ issue_rewrite_end rc=$ISSUE_RC $(date --iso-8601=seconds)"

  if [ "$ISSUE_RC" -ne 0 ] || [ "$ISSUE_TARGETS" -eq 0 ]; then
    echo "__BRT_STAGE__ abort_no_behavior_targets rc=$ISSUE_RC targets=$ISSUE_TARGETS"
    exit "$ISSUE_RC"
  fi
else
  ISSUE_RC=0
  echo "__BRT_STAGE__ issue_rewrite_skipped reason=w/o_behavior_target $(date --iso-8601=seconds)"
fi

echo "__BRT_STAGE__ generation_start $(date --iso-8601=seconds)"
GENERATION_RUNTIME_ARGS=(
  --dataset_mode "$DATASET_MODE"
  --runtime_backend "$RUNTIME_BACKEND"
  --official_harness_python "$OFFICIAL_PYTHON"
  --swtbench_root "$SWTBENCH_ROOT_VALUE"
  --tddbench_root "${TDD_BENCH_ROOT:-$PACKAGE_ROOT/TDD-Bench-Verified}"
)
BEHAVIOR_CACHE_ARGS=()
if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
  BEHAVIOR_CACHE_ARGS+=(--behavior-target-cache "$BEHAVIOR_TARGET_CACHE")
fi
GENERATION_COMMAND=(
"$PYTHON_BIN" -m brt6.pipeline.run \
  --instances_path "$INSTANCES_PATH" \
  --code_retrieval_path "$CODE_RETRIEVAL" \
  --test_retrieval_path "$TEST_RETRIEVAL" \
  --repo_root_base "$REPO_ROOT" \
  --output_dir "$GENERATION_DIR" \
  --llm-provider "$LLM_PROVIDER" \
  --model "$MODEL" \
  --max_workers "$GENERATION_WORKERS" \
  --resume \
  --max_semantic_rounds "$MAX_SEMANTIC_ROUNDS" \
  --validation_mode buggy_only \
  --timeout "${GENERATION_TIMEOUT:-3600}" \
  --temperature 0.1 \
  --max_tokens 4096 \
  --enable_behavior_target "$ENABLE_BEHAVIOR_TARGET" \
  --enable_seed_mutation "$ENABLE_MUTATION" \
  --enable_specialized_feedback "$ENABLE_SPECIALIZED_FEEDBACK" \
  --enable_environment_feedback "$ENABLE_ENVIRONMENT_FEEDBACK" \
  --enable_trigger_feedback "$ENABLE_TRIGGER_FEEDBACK" \
  --enable_assertion_feedback "$ENABLE_ASSERTION_FEEDBACK" \
  --enable_semantic_delta "$ENABLE_SEMANTIC_DELTA" \
  "${BEHAVIOR_CACHE_ARGS[@]}" \
  "${GENERATION_RUNTIME_ARGS[@]}"
)
if [[ -n "$BEHAVIOR_TARGET_CACHE" ]]; then
  "${GENERATION_COMMAND[@]}"
elif [ "$ENABLE_BEHAVIOR_TARGET" = "true" ]; then
  BRT4_BEHAVIOR_CACHE_DIR="$ISSUE_DIR" "${GENERATION_COMMAND[@]}"
else
  env -u BRT4_BEHAVIOR_CACHE_DIR "${GENERATION_COMMAND[@]}"
fi
GENERATION_RC=$?
echo "__BRT_STAGE__ generation_end rc=$GENERATION_RC $(date --iso-8601=seconds)"

GENERATION_GATE_PATH=$RUN_DIR/generation_gate.json
ALLOW_INCOMPLETE_FORMAL_EVAL=${ALLOW_INCOMPLETE_FORMAL_EVAL:-true}
FORMAL_EVALUATION_ALLOWED=$("$PYTHON_BIN" - \
  "$INSTANCES_PATH" "$GENERATION_DIR" "$GENERATION_RC" "$GENERATION_GATE_PATH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from brt6.core.run_gate import formal_evaluation_allowed

instances_path = Path(sys.argv[1])
generation_dir = Path(sys.argv[2])
generation_rc = int(sys.argv[3])
gate_path = Path(sys.argv[4])
data = json.loads(instances_path.read_text(encoding="utf-8"))
rows = data if isinstance(data, list) else list(data.values())
generated_ids = []
missing_ids = []
for row in rows:
    instance_id = str(row.get("instance_id") or "")
    final_test = generation_dir / instance_id / "final_test.py"
    try:
        present = final_test.is_file() and final_test.stat().st_size > 0
    except OSError:
        present = False
    (generated_ids if present else missing_ids).append(instance_id)
artifacts_complete = bool(rows) and not missing_ids
allow_incomplete = str(__import__("os").environ.get("ALLOW_INCOMPLETE_FORMAL_EVAL", "false")).lower() in {"1", "true", "yes", "on"}
allowed = formal_evaluation_allowed(
    generation_rc, len(generated_ids), len(rows), allow_incomplete
)
reasons = []
if generation_rc != 0 and not allow_incomplete:
    reasons.append(f"generation returned {generation_rc}")
if not artifacts_complete and not allow_incomplete:
    reasons.append(
        f"only {len(generated_ids)}/{len(rows)} non-empty final_test.py artifacts exist"
    )
gate = {
    "checked_at": datetime.now(timezone.utc).astimezone().isoformat(),
    "generation_returncode": generation_rc,
    "dataset_size": len(rows),
    "generated_tests": len(generated_ids),
    "missing_generation": len(missing_ids),
    "generated_ids": generated_ids,
    "missing_ids": missing_ids,
    "artifacts_complete": artifacts_complete,
    "allow_incomplete_formal_eval": allow_incomplete,
    "formal_evaluation_allowed": allowed,
    "formal_evaluation_skip_reason": "; ".join(reasons),
}
gate_path.write_text(
    json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("true" if allowed else "false")
PY
) || exit $?

FORMAL_EVALUATION_SKIPPED=false
FORMAL_EVALUATION_SKIP_REASON=""
if [[ "$FORMAL_EVALUATION_ALLOWED" == "true" ]]; then
  echo "__BRT_STAGE__ formal_f2p_start $(date --iso-8601=seconds)"
  "$PYTHON_BIN" "$PROJECT_ROOT/scripts/run_official_eval_after_generation.py" \
    --dataset "$DATASET_MODE" \
    --outputs-dir "$GENERATION_DIR" \
    --dataset-file "$GOLD_DATASET" \
    --official-dataset-name "$GOLD_DATASET" \
    --max-workers "$EVALUATION_WORKERS" \
    --timeout "${FORMAL_EVAL_TIMEOUT:-3600}" \
    --evaluation-dir "$FORMAL_DIR" \
    --run-id "$(basename "$RUN_DIR")" \
    --model-name "brt6-$MODEL" \
    --compute-coverage "$COMPUTE_PATCH_COVERAGE" \
    --official-python "$OFFICIAL_PYTHON" \
    --swtbench-root "$SWTBENCH_ROOT_VALUE" \
    --tddbench-root "${TDD_BENCH_ROOT:-$PACKAGE_ROOT/TDD-Bench-Verified}"
  EVALUATION_RC=$?
  echo "__BRT_STAGE__ formal_f2p_end rc=$EVALUATION_RC $(date --iso-8601=seconds)"
else
  EVALUATION_RC=3
  FORMAL_EVALUATION_SKIPPED=true
  FORMAL_EVALUATION_SKIP_REASON=$("$PYTHON_BIN" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["formal_evaluation_skip_reason"])' \
    "$GENERATION_GATE_PATH")
  echo "__BRT_STAGE__ formal_f2p_skipped reason=$FORMAL_EVALUATION_SKIP_REASON $(date --iso-8601=seconds)"
fi

"$PYTHON_BIN" - "$RUN_DIR" "$INSTANCES_PATH" "$ISSUE_RC" "$GENERATION_RC" "$EVALUATION_RC" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

run = Path(sys.argv[1])
instances_path = Path(sys.argv[2])
issue_rc, generation_rc, evaluation_rc = map(int, sys.argv[3:])
run_config_path = run / 'run_config.json'
run_config = json.loads(run_config_path.read_text(encoding='utf-8'))
data = json.loads(instances_path.read_text(encoding="utf-8"))
instances = data if isinstance(data, list) else list(data.values())
def has_nonempty_final_test(row):
    path = run / 'generation' / row['instance_id'] / 'final_test.py'
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False

generated = sum(has_nonempty_final_test(row) for row in instances)
instance_summaries = []
for row in instances:
    summary_path = run / 'generation' / row['instance_id'] / 'summary.json'
    if not summary_path.is_file():
        continue
    try:
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        continue
    if isinstance(summary, dict):
        instance_summaries.append(summary)
repair_route_counts = {
    route: sum(
        int((summary.get('repair_route_counts') or {}).get(route) or 0)
        for summary in instance_summaries
    )
    for route in (
        'dependency_recovery', 'environment', 'trigger', 'assertion', 'generic'
    )
}
metrics_path = run / 'evaluation' / 'formal_f2p' / 'metrics.json'
generation_gate_path = run / 'generation_gate.json'
generation_gate = (
    json.loads(generation_gate_path.read_text(encoding='utf-8'))
    if generation_gate_path.is_file()
    else {}
)
formal_evaluation_skipped = not bool(
    generation_gate.get('formal_evaluation_allowed', False)
)
metrics = (
    json.loads(metrics_path.read_text())
    if metrics_path.is_file() and not formal_evaluation_skipped
    else {}
)
completion = {
    'finished_at': datetime.now(timezone.utc).astimezone().isoformat(),
    'dataset_mode': run_config['dataset_mode'],
    'llm_provider': run_config.get('llm_provider', 'deepseek'),
    'llm_model': run_config.get('llm_model', ''),
    'behavior_target_enabled': run_config['behavior_target'],
    'mutation_enabled': run_config['mutation'],
    'specialized_feedback_enabled': run_config['specialized_feedback'],
    'environment_feedback_enabled': run_config['environment_feedback'],
    'trigger_feedback_enabled': run_config['trigger_feedback'],
    'assertion_feedback_enabled': run_config['assertion_feedback'],
    'method_variant': run_config['method_variant'],
    'ablation_id': run_config['ablation_id'],
    'ablation_signature': run_config['ablation_signature'],
    'ablation_config': run_config,
    'behavior_target_source': run_config.get('behavior_target_source', {}),
    'behavior_target_source_signature': run_config.get('behavior_target_source_signature', ''),
    'framework_git_commit': run_config.get('framework_git_commit', ''),
    'framework_git_branch': run_config.get('framework_git_branch', ''),
    'tracked_worktree_clean': run_config.get('tracked_worktree_clean', False),
    'instances_path': str(instances_path),
    'issue_rewrite_returncode': issue_rc,
    'generation_returncode': generation_rc,
    'evaluation_returncode': evaluation_rc,
    'dataset_size': len(instances),
    'generated_tests': generated,
    'missing_generation': len(instances) - generated,
    'generation_complete': bool(
        generation_gate.get('artifacts_complete', False)
    ),
    'generation_gate': generation_gate,
    'formal_evaluation_skipped': formal_evaluation_skipped,
    'formal_evaluation_skip_reason': generation_gate.get(
        'formal_evaluation_skip_reason', ''
    ),
    'delta_calls': sum(
        int(summary.get('delta_calls') or 0)
        for summary in instance_summaries
    ),
    'repair_route_counts': repair_route_counts,
    'formal_total_instances': metrics.get('total_instances'),
    'f2p_success': metrics.get('f2p_success'),
    'f2p_fail': metrics.get('f2p_fail'),
    'f2p_at_1_percent': metrics.get('f2p_at_1_percent'),
    'by_status': metrics.get('by_status', {}),
    'denominator_valid': (
        None
        if formal_evaluation_skipped
        else metrics.get('total_instances') == len(instances)
    ),
    'patch_cov_enabled': run_config['patch_cov_enabled'],
    'patch_cov_at_1_percent': metrics.get('patch_cov_at_1_percent'),
    'patch_cov_delta_at_1_percent': metrics.get('patch_cov_delta_at_1_percent'),
    'patch_cov_definition': metrics.get('patch_cov_definition'),
}
(run / 'completion.json').write_text(json.dumps(completion, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(completion, ensure_ascii=False, indent=2))
PY

echo "__BRT_STAGE__ complete $(date --iso-8601=seconds)"
if [[ "$GENERATION_RC" -ne 0 ]]; then
  exit "$GENERATION_RC"
fi
exit "$EVALUATION_RC"

#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MODEL=${MODEL:?Set MODEL to the same model as Full}
PROVIDER=${PROVIDER:?Set PROVIDER to gpt or deepseek}
RUN=${RUN_DIR:?Set RUN_DIR to a new absolute directory under /root}
TARGET_CACHE=${TARGET_CACHE:?Set TARGET_CACHE to the matching frozen Full C1 cache}
[[ "$RUN" == /root/* ]] || { echo 'RUN_DIR must be under /root' >&2; exit 2; }
[[ -d "$TARGET_CACHE" ]] || { echo 'Missing TARGET_CACHE' >&2; exit 2; }
CODE=$RUN/code/brt6
PYTHON=${PYTHON_BIN:-/root/miniconda3/envs/icore/bin/python}
SWT_PYTHON=${SWTBENCH_PYTHON:-/root/miniconda3/envs/swtbench/bin/python}
mkdir -p "$RUN/logs" "$RUN/tmp"
exec 9>"$RUN/launcher.lock"
flock -n 9 || exit 2
export PYTHONPATH=$RUN/code
export BRT_API_POOL_FILE=${BRT_API_POOL_FILE:?Set BRT_API_POOL_FILE}
export BRT_ALLOWED_API_HOST=api.open.xiaojingai.com
export BRT_MODEL_ID="$MODEL" BRT_DISABLE_THINKING=1 BRT_LLM_STREAM=1
export BRT3_LLM_REQUEST_TIMEOUT=600 BRT3_LLM_MAX_ATTEMPTS=2 BRT_RETRY_TRANSIENT_API=1
export BRT_LLM_TRUNCATION_MAX_TOKENS=8192 BRT_COST_DIR=$RUN
export BRT_REQUIRE_OFFICIAL_DOCKER=1 BRT_ALLOW_DIRTY_WORKTREE=1
export BRT_OFFICIAL_DOCKER_STARTUP_TIMEOUT=7200 DOCKER_HOST=unix:///run/mutate-docker.sock
export TMPDIR=$RUN/tmp TEMP=$RUN/tmp TMP=$RUN/tmp
unset BRT_DISABLE_DIRECT_FALLBACK BRT4_BEHAVIOR_CACHE_DIR BRT_DIVERSE_ADAPTATION
"$PYTHON" "$ROOT/scripts/prepare_plain_generator.py" --run-dir "$RUN" --target-cache "$TARGET_CACHE"
cd "$RUN"
trap '"$PYTHON" "$ROOT/scripts/summarize_api_cost.py" --run-dir "$RUN" >>"$RUN/logs/cost.log" 2>&1 || true' EXIT

generate() {
  "$PYTHON" -u -m brt6.pipeline.run \
    --instances_path "$ROOT/data/issues/swt276_issues.json" \
    --code_retrieval_path "$ROOT/retrieval_results/code/code_retrieval_results_gpt.json" \
    --test_retrieval_path "$ROOT/retrieval_results/test/icore/gpt/related_tests.json" \
    --behavior-target-cache "$TARGET_CACHE" \
    --repo_root_base /root/Baxxhy/BugReproduce/swe_repos --output_dir "$RUN/generation" \
    --model "$MODEL" --llm-provider "$PROVIDER" --temperature 0.1 --max_tokens 4096 \
    --max_workers 20 --max_semantic_rounds "${MAX_SEMANTIC_ROUNDS:-6}" --timeout 7200 --resume \
    --validation_mode buggy_only --alignment-verifier strict --dataset_mode swt \
    --runtime_backend official_docker --official_harness_python "$SWT_PYTHON" \
    --swtbench_root "$ROOT/evaluation/vendor/swtbench" \
    --enable_behavior_target true --enable_seed_mutation false --enable_semantic_delta false \
    --enable_specialized_feedback true --enable_environment_feedback true \
    --enable_trigger_feedback true --enable_assertion_feedback true "$@"
}

if [[ ! -f "$RUN/pilot.done" ]]; then
  generate --instance_ids_file "$RUN/pilot_ids.txt" >>"$RUN/logs/generation.log" 2>&1
  "$PYTHON" - "$RUN" <<'PY'
import json,sys
from pathlib import Path
r=Path(sys.argv[1])
for iid in (r/'pilot_ids.txt').read_text().splitlines():
 d=r/'generation'/iid
 s=json.loads((d/'summary.json').read_text())
 assert s['status'] not in {'ERROR','SETUP_ERROR','PAUSED_API','PAUSED_INFRA'}, (iid,s['status'])
 assert s['ablation_id']=='wo_individual_adaptation', (iid,s.get('ablation_id'))
 assert not list(d.rglob('delta_round_*.json')), iid
 branches=list((d/'seed_candidates').glob('seed_*'))
 assert len(branches)==3, (iid,len(branches))
 for b in branches:
  p=(b/'prompts/generation_round_0.txt').read_text()
  assert '[Shared Top-3 reference collection]' in p, b
  assert '"rank": 2' in p, b
 for vp in d.rglob('prompts/strict_verifier_round_*.txt'):
  text=vp.read_text()
  assert 'Semantic Delta Diagnosis' not in text, vp
  assert '"next_operator":' not in text, vp
 for gp in d.rglob('prompts/generation_round_*.txt'):
  text=gp.read_text()
  assert '"next_operator":' not in text, gp
  assert 'The only Semantic Delta for this round' not in text, gp
 print(iid,s['status'],'joint Top-3 / no Delta / 3 candidates verified',flush=True)
(r/'pilot.done').write_text('pilot passed; no F2P threshold\n')
PY
fi

if [[ ! -f "$RUN/generation.done" ]]; then
  generate >>"$RUN/logs/generation.log" 2>&1
  "$PYTHON" - "$RUN" <<'PY'
import json,sys
from pathlib import Path
r=Path(sys.argv[1]); s=json.loads((r/'generation/summary.json').read_text())
assert s['total']==276 and len(s['results'])==276, s.get('total')
bad=[v for v in s['results'] if v.get('status') in {'PAUSED_API','PAUSED_INFRA','ERROR'}]
assert not bad, f'{len(bad)} instances require resume; outputs preserved'
(r/'generation.done').write_text('complete\n')
PY
fi
if [[ ! -f "$RUN/selection.done" ]]; then
  "$PYTHON" "$CODE/scripts/run_generation_strict_icore.py" --generation "$RUN/generation" \
    --output "$RUN/strict_icore" --issues "$ROOT/data/issues/swt276_issues.json" --workers 20 \
    >"$RUN/logs/selection.log" 2>&1
  touch "$RUN/selection.done"
fi
if [[ ! -f "$RUN/evaluation.done" ]]; then
  "$SWT_PYTHON" "$ROOT/scripts/run_official_eval_after_generation.py" --dataset swt \
    --outputs-dir "$RUN/strict_icore/generation" --dataset-file "$ROOT/data/official/swt276_official_eval.json" \
    --official-dataset-name "$ROOT/data/official/swt276_official_eval.json" \
    --evaluation-dir "$RUN/evaluation/official_f2p" --max-workers 20 --timeout 7200 \
    --run-id "$(basename "$RUN")" --model-name "trait-wo-c2-$MODEL" \
    --compute-coverage false --official-python "$SWT_PYTHON" --swtbench-root "$ROOT/evaluation/vendor/swtbench" \
    >"$RUN/logs/evaluation.log" 2>&1
  touch "$RUN/evaluation.done"
fi
touch "$RUN/completed.done"

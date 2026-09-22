"""Run two end-to-end plumbing checks, then one Top-3 full experiment.

The continuation gate checks completion and infrastructure errors, never F2P.
Credentials are loaded from the existing private pool, never copied to logs.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', nargs='?', default=datetime.now().strftime('%Y%m%d_%H%M%S'))
    parser.add_argument('--start-full-after-canary', action='store_true',
                        help='Requires separate explicit authorization for the full run.')
    args = parser.parse_args()
    name = args.name
    if not name.replace('_', '').isalnum():
        raise ValueError('Invalid run name')
    work = ROOT / 'results/runs' / ('gpt_fusion_' + name)
    work.mkdir(exist_ok=False)
    env = os.environ.copy()
    env.update({
        'BRT_API_POOL_FILE': str(ROOT / '.secrets/api_pool_gate_gpt.json'),
        'BRT_MODEL_ID': 'gpt-5.4-mini', 'BRT_DISABLE_THINKING': '1',
        'BRT_FINAL_SELECTION': 'strict_icore', 'BRT_ALLOW_DIRTY_WORKTREE': '1',
        'BRT_LLM_POOL_ROTATION_ATTEMPTS': '10', 'BRT3_LLM_REQUEST_TIMEOUT': '1200',
        'DOCKER_HOST': 'unix:///run/mutate-docker.sock',
        'MAX_SEMANTIC_ROUNDS': '5', 'COMPUTE_PATCH_COVERAGE': 'false',
        'GENERATION_TIMEOUT': '7200', 'FORMAL_EVAL_TIMEOUT': '7200',
        'BRT_WORKTREE_TIMEOUT': '1200', 'ALLOW_INCOMPLETE_FORMAL_EVAL': 'true',
        'PYTHONPATH': str(ROOT.parent),
    })
    os.environ['BRT_API_POOL_FILE'] = env['BRT_API_POOL_FILE']
    from brt6.llm.api_pool import configured_apis
    from brt6.llm.llm_client import LLMClient
    pool = configured_apis('gpt')
    assert len({p[0] for p in pool}) == 2, 'Expected two distinct keys'
    assert all(p[1].rstrip('/') == 'https://api.open.xiaojingai.com/v1' for p in pool)
    assert LLMClient(provider='gpt', model='gpt-5.4-mini').model == 'gpt-5.4-mini'
    ids = ['django__django-11019', 'matplotlib__matplotlib-23913']
    source = ROOT / 'data/issues/swt276_issues.json'
    rows = read(source)
    subset = [r for r in rows if r['instance_id'] in ids]
    assert len(rows) == 276 and len(subset) == 2
    (work / 'canary_issues.json').write_text(json.dumps(subset, indent=2))
    gold = ROOT / 'data/official/swt276_official_eval.json'
    gold_subset = [r for r in read(gold) if r['instance_id'] in ids]
    assert len(gold_subset) == 2
    (work / 'canary_eval.json').write_text(json.dumps(gold_subset, indent=2))
    # Immutable source evidence for this run; excludes secrets and results.
    with tarfile.open(work / 'implementation.tar.gz', 'w:gz') as archive:
        for directory in ['core', 'execution', 'generation', 'io', 'issue', 'llm',
                          'mutation', 'pipeline', 'prompts_en', 'retrieval',
                          'runtime', 'validation', 'evaluation', 'scripts']:
            for path in sorted((ROOT / directory).rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py', '.sh', '.md'}:
                    archive.add(path, arcname=str(path.relative_to(ROOT)))
    command = ['bash', str(ROOT / 'scripts/run_p0_simple_llm_selector_full.sh'),
               '--dataset', 'swt', '--model', 'gpt', '--semantic-delta', 'on']

    def state(stage, **extra):
        value = {'stage': stage, 'model': 'gpt-5.4-mini', 'top_k_branches': 3,
                 'full_runs': int(args.start_full_after_canary), 'gate_uses_f2p': False, **extra}
        (work / 'state.json').write_text(json.dumps(value, indent=2))
        print(json.dumps(value), flush=True)

    def run(stage, issues, workers):
        run_env = env | {'RUN_DIR': str(work / stage), 'INSTANCES_PATH': str(issues),
                         'GOLD_DATASET': str(work / 'canary_eval.json' if stage == 'canary' else gold),
                         'ISSUE_WORKERS': str(workers), 'GENERATION_WORKERS': str(workers),
                         'EVALUATION_WORKERS': str(workers)}
        state(stage + '_running')
        with (work / (stage + '.log')).open('w') as log:
            rc = subprocess.run(command, cwd=ROOT, env=run_env, stdout=log, stderr=subprocess.STDOUT).returncode
        if rc:
            state(stage + '_failed', returncode=rc)
            raise SystemExit(rc)

    run('canary', work / 'canary_issues.json', 2)
    completion = read(work / 'canary/completion.json')
    ranking = read(work / 'canary/strict_icore/progress.json')
    assert completion['generation_complete'], 'Incomplete canary generation'
    assert completion['generated_tests'] == 2, 'Missing canary tests'
    assert completion['formal_total_instances'] == 2, 'Wrong evaluation denominator'
    assert completion['by_status']['errors'] == 0, 'Canary evaluation infrastructure error'
    assert ranking['completed'] == 2 and not ranking['errors'], 'Incomplete ranking'
    if not args.start_full_after_canary:
        state('canary_completed_awaiting_full_authorization')
        return
    # Refuse automatic continuation if production code changed during canary.
    with tarfile.open(work / 'implementation.tar.gz') as archive:
        for member in archive.getmembers():
            if member.isfile():
                assert (ROOT / member.name).read_bytes() == archive.extractfile(member).read(), member.name
    state('canary_passed')
    run('full', source, 20)
    state('full_completed')


if __name__ == '__main__':
    main()

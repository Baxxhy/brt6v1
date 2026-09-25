"""Materialize the historical ordinary-generator ablation without API requests."""
import argparse
import json
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--target-cache', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    run = args.run_dir.resolve()
    cache = args.target_cache.resolve()
    if not run.is_relative_to(Path('/root')) or not cache.is_dir():
        parser.error('Use a run directory under /root and an existing C1 cache')
    source = root / 'ablations/plain_generator/brt6'
    package = run / 'code/brt6'
    files = [p for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    if package.exists():
        for path in files:
            dest = package / path.relative_to(source)
            if not dest.is_file() or dest.read_bytes() != path.read_bytes():
                raise RuntimeError(f'Existing snapshot differs: {dest}; use a new run directory')
    else:
        shutil.copytree(source, package, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Runtime inputs remain external; credentials are never copied into source.
    for name in ('config', 'data', 'retrieval_results', 'evaluation', '.runtime', '.secrets'):
        dest = package / name
        if dest.is_symlink():
            if dest.resolve() != (root / name).resolve():
                raise RuntimeError(f'Wrong runtime link: {dest}')
        elif dest.exists():
            raise RuntimeError(f'Unexpected snapshot input: {dest}')
        else:
            dest.symlink_to(root / name, target_is_directory=True)
    manifest = {'implementation': 'historical_plain_generator_20260924',
                'target_cache': str(cache), 'individual_adaptation': False,
                'semantic_delta': False, 'reference_mode': 'joint_top3',
                'feedback': 'ordinary_execution'}
    path = run / 'ablation_source.json'
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise RuntimeError('Resume manifest differs; use the original cache or a new directory')
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    rows = json.loads((root / 'data/issues/swt276_issues.json').read_text())
    (run / 'pilot_ids.txt').write_text(''.join(row['instance_id'] + '\n' for row in rows[:3]))
    print(package)


if __name__ == '__main__':
    main()

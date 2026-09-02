#!/usr/bin/env python3
"""Validate a portable frozen BehaviorTarget cache before an experiment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt6.core.behavior_target_cache import (  # noqa: E402
    validate_behavior_target_cache,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--instances-path", required=True)
    parser.add_argument("--dataset-mode", choices=("swt", "tdd"), required=True)
    parser.add_argument("--code-retrieval-path", required=True)
    parser.add_argument("--test-retrieval-path", required=True)
    parser.add_argument("--output-path", default="")
    args = parser.parse_args()
    try:
        provenance = validate_behavior_target_cache(
            args.cache_dir,
            args.instances_path,
            dataset_mode=args.dataset_mode,
            code_retrieval_path=args.code_retrieval_path,
            test_retrieval_path=args.test_retrieval_path,
        )
    except ValueError as exc:
        print(f"BehaviorTarget cache validation failed: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(provenance, ensure_ascii=False, indent=2) + "\n"
    if args.output_path:
        output = Path(args.output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

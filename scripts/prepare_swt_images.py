#!/usr/bin/env python3
"""Build or verify SWT-Bench instance images on a fresh server."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "evaluation" / "vendor" / "swtbench"
PACKAGE_ROOT = ROOT.parent
for path in (PACKAGE_ROOT, HARNESS, HARNESS / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import docker
from src.constants import MAP_VERSION_TO_INSTALL
from src.docker_build import build_instance_image_from_exec_spec
from src.exec_spec import ExecSpec, make_exec_spec

from brt6.runtime.swt_cached_compat import configure_cached_official_runtime


def load_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = list(payload.values()) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"empty or unsupported dataset: {path}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--repo-root", type=Path, default=Path("/root/Baxxhy/BugReproduce/swe_repos")
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be positive")

    rows = load_rows(args.dataset.resolve())
    instance_ids = [str(row.get("instance_id") or "") for row in rows]
    if len(instance_ids) != len(set(instance_ids)):
        raise ValueError("dataset contains duplicate instance IDs")

    configure_cached_official_runtime(
        MAP_VERSION_TO_INSTALL, ExecSpec, args.repo_root.resolve()
    )
    specs = []
    for row in rows:
        safe = {
            field: row.get(field) or ""
            for field in (
                "instance_id",
                "repo",
                "version",
                "base_commit",
                "environment_setup_commit",
            )
        }
        safe["environment_setup_commit"] = (
            safe["environment_setup_commit"] or safe["base_commit"]
        )
        safe["golden_code_patch"] = ""
        specs.append(make_exec_spec(safe))

    client = docker.from_env(timeout=1200)
    missing_specs = []
    for spec in specs:
        try:
            client.images.get(spec.instance_image_key)
        except docker.errors.ImageNotFound:
            missing_specs.append(spec)
    client.close()

    if not args.check_only and missing_specs:
        failed: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(build_instance_image_from_exec_spec, spec, False): spec
                for spec in missing_specs
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    future.result()
                    print(f"built {spec.instance_id}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    failed.append((spec.instance_id, repr(exc)))
                    print(f"failed {spec.instance_id}: {exc}", flush=True)
        if failed:
            print(json.dumps({"build_failures": failed}, indent=2))

    client = docker.from_env(timeout=1200)
    missing = []
    for spec in specs:
        try:
            client.images.get(spec.instance_image_key)
        except docker.errors.ImageNotFound:
            missing.append((spec.instance_id, spec.instance_image_key))
    client.close()
    print(f"required={len(specs)} missing={len(missing)}")
    for instance_id, image in missing:
        print(f"{instance_id}\t{image}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

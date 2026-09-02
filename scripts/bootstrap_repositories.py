#!/usr/bin/env python3
"""Clone benchmark repositories and verify every requested commit exists."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [row for row in data.values() if isinstance(row, dict)]
    raise ValueError(f"unsupported dataset structure: {path}")


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, text=True, check=check)


def missing_commits(repo_dir: Path, commits: list[str]) -> list[str]:
    if not commits:
        return []
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "--batch-check"],
        input="".join(f"{commit}^{{commit}}\n" for commit in commits),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    lines = (proc.stdout or "").splitlines()
    missing = [
        commit
        for commit, line in zip(commits, lines)
        if line.endswith(" missing")
    ]
    if len(lines) < len(commits):
        missing.extend(commits[len(lines) :])
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", action="append", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--manifest", default="")
    args = parser.parse_args()

    requested: dict[str, set[str]] = defaultdict(set)
    for raw_path in args.dataset:
        path = Path(raw_path).expanduser().resolve()
        for row in load_rows(path):
            repo = str(row.get("repo") or "").strip()
            if not repo or "/" not in repo:
                raise ValueError(f"row has invalid repo in {path}: {repo!r}")
            for field in ("base_commit", "environment_setup_commit"):
                commit = str(row.get(field) or "").strip()
                if commit:
                    requested[repo].add(commit)

    repo_root = Path(args.repo_root).expanduser().resolve()
    repo_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    failed = False
    for repo, commits in sorted(requested.items()):
        target = repo_root / repo.split("/", 1)[1]
        remote = f"https://github.com/{repo}.git"
        if not (target / ".git").is_dir():
            if args.check_only:
                records.append({"repo": repo, "path": str(target), "status": "MISSING_REPO"})
                failed = True
                continue
            run(["git", "clone", "--no-checkout", remote, str(target)])
        ordered_commits = sorted(commits)
        missing = missing_commits(target, ordered_commits)
        if missing and not args.check_only:
            run(["git", "-C", str(target), "fetch", "--tags", "--prune", "origin"])
            missing = missing_commits(target, missing)
            for commit in list(missing):
                run(
                    ["git", "-C", str(target), "fetch", "origin", commit],
                    check=False,
                )
            missing = missing_commits(target, missing)
        status = "READY" if not missing else "MISSING_COMMITS"
        failed = failed or bool(missing)
        records.append(
            {
                "repo": repo,
                "path": str(target),
                "remote": remote,
                "requested_commits": len(commits),
                "missing_commits": missing,
                "status": status,
            }
        )
        print(f"{repo}: {status} ({len(commits)} commits)", flush=True)

    manifest = {
        "datasets": [str(Path(value).expanduser().resolve()) for value in args.dataset],
        "repo_root": str(repo_root),
        "repositories": records,
        "ready": not failed,
    }
    if args.manifest:
        path = Path(args.manifest).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

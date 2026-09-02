#!/usr/bin/env python3
"""Export the current working tree as a fresh, secret-free Git repository."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {
    ".git", ".secrets", ".bootstrap", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "__pycache__", "artifacts", "results", "outputs", "logs",
}
EXCLUDED_FILES = {".env", ".coverage"}


def ignore(directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        path = Path(directory) / name
        if name in EXCLUDED_DIRS or name in EXCLUDED_FILES:
            ignored.add(name)
        elif path.is_file() and path.suffix in {".pyc", ".pyo", ".log", ".pid"}:
            ignored.add(name)
    return ignored


def run(command: list[str], cwd: Path) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    args = parser.parse_args()
    destination = Path(args.destination).expanduser().resolve()
    if destination == SOURCE_ROOT or SOURCE_ROOT in destination.parents:
        print("destination must be outside the source repository", file=sys.stderr)
        return 2
    if destination.exists() and any(destination.iterdir()):
        print(f"destination is not empty: {destination}", file=sys.stderr)
        return 2
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SOURCE_ROOT,
        destination,
        dirs_exist_ok=True,
        ignore=ignore,
        symlinks=False,
    )
    checker = destination / "scripts" / "check_repository_secrets.py"
    run([sys.executable, str(checker), str(destination)], destination)
    run(["git", "init", "-b", "main"], destination)
    run(["git", "add", "."], destination)
    print(
        "\nClean repository prepared. Inspect `git status`, commit, add your remote, "
        "and push. Configure keys separately on every machine."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

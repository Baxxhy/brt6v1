#!/usr/bin/env python3
"""Fail when a repository snapshot contains likely plaintext credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


SKIP_DIRS = {
    ".git", ".secrets", "artifacts", "results", "outputs", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
TEXT_SUFFIXES = {
    "", ".env", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml",
}
PLACEHOLDERS = ("replace", "example", "your_key", "placeholder", "dummy")
TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:api[_-]?key|token)\s*[=:]\s*['\"]([A-Za-z0-9._-]{16,})['\"]", re.I),
)


def candidate_files(root: Path):
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        base = Path(directory)
        for filename in filenames:
            path = base / filename
            if path.name == ".env" or path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    findings: list[str] = []
    for path in candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        low = text.lower()
        for pattern in TOKEN_PATTERNS:
            for match in pattern.finditer(text):
                token = match.group(1) if match.lastindex else match.group(0)
                if not any(marker in token.lower() for marker in PLACEHOLDERS):
                    findings.append(str(path.relative_to(root)))
                    break
        if path.name.endswith("api_pool.json"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            rows = payload.get("apis", []) if isinstance(payload, dict) else []
            if any(
                isinstance(row, dict)
                and str(row.get("api_key") or "").strip()
                and not any(
                    marker in str(row.get("api_key") or "").lower()
                    for marker in PLACEHOLDERS
                )
                for row in rows
            ):
                findings.append(str(path.relative_to(root)))
    findings = sorted(set(findings))
    if findings:
        print("Potential plaintext secrets found:")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print("Secret scan passed: no plaintext API credentials detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

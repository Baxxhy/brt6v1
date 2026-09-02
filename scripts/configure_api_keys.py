#!/usr/bin/env python3
"""Create or validate the ignored local multi-key API configuration."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / ".secrets" / "api_pool.json"
PROVIDER_DEFAULTS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-v3"),
    "gpt": ("https://aigc.x-see.cn/v1", "gpt-5.4-mini"),
}


def _split_keys(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _entry_provider(row: dict) -> str:
    provider = str(row.get("provider") or "").strip().lower()
    if provider:
        return provider
    model = str(row.get("model") or "").strip().lower()
    return "gpt" if model.startswith("gpt-") else "deepseek"


def _entries_from_environment(provider: str) -> list[dict[str, str]]:
    prefix = "GPT" if provider == "gpt" else "DEEPSEEK"
    raw = os.environ.get(f"{prefix}_API_KEYS") or os.environ.get(f"{prefix}_API_KEY") or ""
    keys = _split_keys(raw)
    default_base, default_model = PROVIDER_DEFAULTS[provider]
    base_url = os.environ.get(f"{prefix}_BASE_URL") or default_base
    model = os.environ.get(f"{prefix}_MODEL") or default_model
    return [
        {
            "name": f"{provider}-{index}",
            "provider": provider,
            "api_key": key,
            "base_url": base_url,
            "model": model,
        }
        for index, key in enumerate(keys, 1)
    ]


def _read_existing_entries(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid existing API pool file: {exc}") from exc
    rows = payload.get("apis", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("API pool must be a list or an object containing an 'apis' list")
    return rows


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def _validate(path: Path, provider: str | None = None) -> int:
    if not path.is_file():
        print(f"API pool is not configured: {path}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Invalid API pool file: {exc}", file=sys.stderr)
        return 1
    rows = payload.get("apis", []) if isinstance(payload, dict) else []
    valid = [row for row in rows if isinstance(row, dict) and str(row.get("api_key") or "").strip()]
    selected = [row for row in valid if provider is None or _entry_provider(row) == provider]
    if not valid:
        print("API pool contains no usable keys", file=sys.stderr)
        return 1
    if provider is not None and not selected:
        print(f"API pool contains no usable {provider} keys", file=sys.stderr)
        return 1
    mode = oct(path.stat().st_mode & 0o777)
    selected_text = f", {provider}_entries={len(selected)}" if provider else ""
    print(
        f"API pool ready: entries={len(valid)}{selected_text}, "
        f"permissions={mode}, path={path}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--provider", choices=tuple(PROVIDER_DEFAULTS))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--from-env", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    if args.check:
        return _validate(output, args.provider)

    provider = args.provider or "deepseek"
    entries = _entries_from_environment(provider) if args.from_env else []
    if args.from_env and not entries:
        prefix = "GPT" if provider == "gpt" else "DEEPSEEK"
        print(
            f"No {provider} keys found in {prefix}_API_KEY or {prefix}_API_KEYS",
            file=sys.stderr,
        )
        return 2
    if not entries:
        default_base, default_model = PROVIDER_DEFAULTS[provider]
        raw_keys = getpass.getpass(
            f"Paste one or more {provider} keys (input hidden): "
        )
        keys = _split_keys(raw_keys)
        if not keys:
            print("No keys supplied", file=sys.stderr)
            return 2
        base_url = input(f"Base URL [{default_base}]: ").strip() or default_base
        model = input(f"Model [{default_model}]: ").strip() or default_model
        entries = [
            {
                "name": f"{provider}-{index}",
                "provider": provider,
                "api_key": key,
                "base_url": base_url,
                "model": model,
            }
            for index, key in enumerate(keys, 1)
        ]
    try:
        existing = _read_existing_entries(output)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    merged = [row for row in existing if _entry_provider(row) != provider]
    merged.extend(entries)
    _write_private_json(output, {"apis": merged})
    return _validate(output, provider)


if __name__ == "__main__":
    raise SystemExit(main())

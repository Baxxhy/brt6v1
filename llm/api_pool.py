"""Secret-safe multi-endpoint API pool configuration.

Real credentials must live in an ignored local file or environment variables,
never in this tracked module.  See ``config/api_pool.example.json``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v3"
DEFAULT_GPT_BASE_URL = "https://aigc.x-see.cn/v1"
DEFAULT_GPT_MODEL = "gpt-5.4-mini"
SUPPORTED_PROVIDERS = ("deepseek", "gpt")

# Compatibility names retained for callers that imported the old constants.
API_NAMES: list[str] = []
API_KEYS: list[str] = []
API_BASE_URLS: list[str] = []
API_MODELS: list[str] = []


def _split_env(name: str) -> list[str]:
    raw = str(os.environ.get(name) or "")
    return [item.strip() for item in re.split(r"[,\n]", raw) if item.strip()]


def _expand(values: list[str], size: int, default: str) -> list[str]:
    if not values:
        return [default] * size
    if len(values) == 1:
        return values * size
    if len(values) != size:
        raise ValueError(
            f"API pool field has {len(values)} values but {size} keys were configured"
        )
    return values


def _normalize_provider(value: object, model: str = "") -> str:
    provider = str(value or "").strip().lower()
    if not provider:
        provider = "gpt" if model.strip().lower().startswith("gpt-") else "deepseek"
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"unsupported API pool provider {provider!r}; expected deepseek or gpt"
        )
    return provider


def _normalize_entries(payload: Any) -> list[tuple[str, str, str, str]]:
    rows = payload.get("apis", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("API pool JSON must be a list or an object containing 'apis'")
    entries: list[tuple[str, str, str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"API pool entry {index} must be an object")
        key = str(row.get("api_key") or row.get("key") or "").strip()
        if not key:
            continue
        raw_model = str(row.get("model") or "").strip()
        provider = _normalize_provider(row.get("provider"), raw_model)
        default_base = (
            DEFAULT_GPT_BASE_URL if provider == "gpt" else DEFAULT_BASE_URL
        )
        default_model = DEFAULT_GPT_MODEL if provider == "gpt" else DEFAULT_MODEL
        base_url = str(row.get("base_url") or default_base).strip()
        model = raw_model or default_model
        entries.append((provider, key, base_url, model))
    return entries


def _configured_file() -> Path | None:
    explicit = str(os.environ.get("BRT_API_POOL_FILE") or "").strip()
    candidates = [Path(explicit).expanduser()] if explicit else []
    project_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            project_root / ".secrets" / "api_pool.json",
            Path.home() / ".config" / "brt5" / "api_pool.json",
        ]
    )
    return next((path for path in candidates if path.is_file()), None)


def _entries_from_file(path: Path) -> list[tuple[str, str, str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid API pool file: {path}") from exc
    return _normalize_entries(payload)


def _provider_entries_from_environment(
    provider: str,
) -> list[tuple[str, str, str, str]]:
    if provider == "gpt":
        keys = _split_env("GPT_API_KEYS") or _split_env("GPT_API_KEY")
        default_base = DEFAULT_GPT_BASE_URL
        default_model = DEFAULT_GPT_MODEL
        base_names = ("GPT_BASE_URLS", "GPT_BASE_URL")
        model_names = ("GPT_MODELS", "GPT_MODEL")
    else:
        keys = _split_env("DEEPSEEK_API_KEYS") or _split_env("DEEPSEEK_API_KEY")
        default_base = DEFAULT_BASE_URL
        default_model = DEFAULT_MODEL
        base_names = ("DEEPSEEK_BASE_URLS", "DEEPSEEK_BASE_URL")
        model_names = ("DEEPSEEK_MODELS", "DEEPSEEK_MODEL")
    if not keys:
        return []
    bases = _expand(
        _split_env(base_names[0]) or _split_env(base_names[1]),
        len(keys),
        default_base,
    )
    models = _expand(
        _split_env(model_names[0]) or _split_env(model_names[1]),
        len(keys),
        default_model,
    )
    return [(provider, key, base, model) for key, base, model in zip(keys, bases, models)]


def _entries_from_environment(
    provider: str | None = None,
) -> list[tuple[str, str, str, str]]:
    json_payload = str(os.environ.get("BRT_API_POOL_JSON") or "").strip()
    if json_payload:
        try:
            entries = _normalize_entries(json.loads(json_payload))
            return [entry for entry in entries if provider in {None, entry[0]}]
        except json.JSONDecodeError as exc:
            raise ValueError("BRT_API_POOL_JSON is not valid JSON") from exc
    providers = (provider,) if provider else SUPPORTED_PROVIDERS
    return [
        entry
        for selected_provider in providers
        for entry in _provider_entries_from_environment(selected_provider)
    ]


def _legacy_local_entries() -> list[tuple[str, str, str, str]]:
    """Read the ignored pre-migration pool on this machine only.

    This compatibility path is never present in a clean clone or export.
    """
    legacy = Path(__file__).resolve().parents[1] / ".secrets" / "api_pool_legacy.py"
    if not legacy.is_file():
        return []
    spec = importlib.util.spec_from_file_location("brt5_local_api_pool", legacy)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loader = getattr(module, "configured_apis", None)
    if not callable(loader):
        return []
    return [("deepseek", key, base, model) for key, base, model in loader()]


def configured_apis(provider: str | None = None) -> list[tuple[str, str, str]]:
    """Return one provider's ``(key, base_url, model)`` entries without keys in logs."""

    selected_provider = (
        _normalize_provider(provider) if provider is not None else None
    )
    path = _configured_file()
    if path is not None:
        file_entries = [
            entry
            for entry in _entries_from_file(path)
            if selected_provider in {None, entry[0]}
        ]
        if file_entries:
            return [(key, base, model) for _, key, base, model in file_entries]
    entries = _entries_from_environment(selected_provider)
    if entries:
        return [(key, base, model) for _, key, base, model in entries]
    legacy = [
        entry
        for entry in _legacy_local_entries()
        if selected_provider in {None, entry[0]}
    ]
    return [(key, base, model) for _, key, base, model in legacy]

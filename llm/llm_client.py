"""Provider-aware OpenAI-compatible LLM client."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from .api_pool import configured_apis
from ..core.config import (
    DEFAULT_LLM_BACKOFF_BASE,
    DEFAULT_LLM_MAX_ATTEMPTS,
    DEFAULT_LLM_RATE_LIMIT_BACKOFF,
    DEFAULT_LLM_REQUEST_TIMEOUT,
    DEFAULT_LLM_PROVIDER,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    load_llm_config,
)


class LLMClient:
    _key_lock = threading.Lock()
    _key_indices: dict[str, int] = {}

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        cfg = load_llm_config(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.provider = cfg.provider or DEFAULT_LLM_PROVIDER
        self.model = cfg.model or DEFAULT_MODEL
        self._uses_local_pool = False
        local_api = self._pick_local_api(self.provider) if not api_key else None
        if local_api:
            local_key, local_base, local_model = local_api
            self.api_key = local_key
            self.base_url = (local_base or cfg.base_url or "https://api.deepseek.com").rstrip("/")
            self.model = local_model or self.model
            self._uses_local_pool = True
        else:
            multi_key = self._pick_env_key(self.provider) if not api_key else None
            self.api_key = api_key or multi_key or cfg.api_key
            self.base_url = (cfg.base_url or "https://api.deepseek.com").rstrip("/")
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.request_timeout = int(
            os.environ.get("BRT3_LLM_REQUEST_TIMEOUT", DEFAULT_LLM_REQUEST_TIMEOUT)
        )
        self.max_attempts = int(
            os.environ.get("BRT3_LLM_MAX_ATTEMPTS", DEFAULT_LLM_MAX_ATTEMPTS)
        )
        self.backoff_base = float(
            os.environ.get("BRT3_LLM_BACKOFF_BASE", DEFAULT_LLM_BACKOFF_BASE)
        )
        self.rate_limit_backoff = float(
            os.environ.get(
                "BRT3_LLM_RATE_LIMIT_BACKOFF",
                DEFAULT_LLM_RATE_LIMIT_BACKOFF,
            )
        )
        if not self.api_key:
            env_name = "GPT_API_KEY" if self.provider == "gpt" else "DEEPSEEK_API_KEY"
            raise ValueError(f"missing {self.provider} API key; set {env_name}")

    @classmethod
    def _next_index(cls, provider: str, size: int) -> int:
        index = cls._key_indices.get(provider, 0)
        cls._key_indices[provider] = index + 1
        return index % size

    @classmethod
    def _pick_local_api(cls, provider: str) -> tuple[str, str, str] | None:
        entries = configured_apis(provider)
        if not entries:
            return None
        with cls._key_lock:
            entry = entries[cls._next_index(provider, len(entries))]
        return entry

    @staticmethod
    def _env_keys(provider: str) -> list[str]:
        env_name = "GPT_API_KEYS" if provider == "gpt" else "DEEPSEEK_API_KEYS"
        raw = os.environ.get(env_name) or ""
        return [x.strip() for x in raw.split(",") if x.strip()]

    @classmethod
    def _pick_env_key(cls, provider: str) -> str | None:
        keys = cls._env_keys(provider)
        if not keys:
            return None
        with cls._key_lock:
            key = keys[cls._next_index(provider, len(keys))]
        return key

    def _rotate_api(self) -> None:
        if self._uses_local_pool:
            entry = self._pick_local_api(self.provider)
            if entry:
                self.api_key, base_url, local_model = entry
                self.base_url = (base_url or self.base_url).rstrip("/")
                if local_model:
                    self.model = local_model
                return
        rotated = self._pick_env_key(self.provider)
        if rotated:
            self.api_key = rotated

    @staticmethod
    def _chat_url(base_url: str) -> str:
        url = base_url.rstrip("/")
        if url.endswith("/v1"):
            return url + "/chat/completions"
        if not url.endswith("/v1/chat/completions") and not url.endswith("/chat/completions"):
            return url + "/v1/chat/completions"
        return url

    def chat(self, system_prompt: str, user_prompt: str, temperature: float | None = None, max_tokens: int | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_error: Exception | None = None
        max_attempts = max(
            self.max_attempts,
            len(configured_apis(self.provider)),
            len(self._env_keys(self.provider)),
        )
        for attempt in range(max_attempts):
            payload["model"] = self.model
            data = json.dumps(payload).encode("utf-8")
            url = self._chat_url(self.base_url)
            headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:  # noqa: S310
                    body = resp.read().decode("utf-8")
                parsed = json.loads(body)
                return parsed["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"LLM HTTP {exc.code}: {body[:500]}")
                # Malformed/oversized requests are deterministic for this
                # payload. Rotating through the entire key pool only adds a
                # long exponential backoff and cannot make them succeed.
                if exc.code in {400, 404, 405, 413, 422}:
                    break
                if exc.code in {401, 403, 429}:
                    self._rotate_api()
                if exc.code == 429 and attempt < max_attempts - 1:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        server_wait = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        server_wait = 0.0
                    wait = max(
                        server_wait,
                        min(self.rate_limit_backoff * (2**attempt), 300.0),
                    )
                    time.sleep(wait)
                    continue
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt < max_attempts - 1:
                time.sleep(min(self.backoff_base * (2**attempt), 180.0))
        raise RuntimeError(f"LLM request failed after {max_attempts} attempts: {last_error}")

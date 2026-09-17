"""Provider-aware OpenAI-compatible LLM client."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .api_pool import configured_apis, pool_policy
from ..core.config import (
    DEFAULT_LLM_BACKOFF_BASE,
    DEFAULT_DEEPSEEK_BASE_URL,
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
        policy = pool_policy() if not api_key else {}
        self.immediate_failover = bool(policy.get("immediate_failover", False))
        requested_model = None if policy.get("endpoint_model_authoritative") else model
        self._requested_model = requested_model
        local_api = self._pick_local_api(self.provider) if not api_key else None
        if local_api:
            local_key, local_base, local_model = local_api
            self.api_key = local_key
            self.base_url = (local_base or cfg.base_url or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
            # An explicit method configuration must not be silently replaced by
            # a model alias stored in a generic credential pool.
            self.model = requested_model or local_model or self.model
            self._uses_local_pool = True
        else:
            multi_key = self._pick_env_key(self.provider) if not api_key else None
            self.api_key = api_key or multi_key or cfg.api_key
            self.base_url = (cfg.base_url or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
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
        self.stream = str(os.environ.get("BRT_LLM_STREAM") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.truncation_max_tokens = int(
            os.environ.get(
                "BRT_LLM_TRUNCATION_MAX_TOKENS",
                max(8192, int(self.max_tokens)),
            )
        )
        if not self.api_key:
            env_name = "GPT_API_KEY" if self.provider == "gpt" else "DEEPSEEK_API_KEY"
            raise ValueError(f"missing {self.provider} API key; set {env_name}")
        self._validate_allowed_host()

    @staticmethod
    def _allowed_hosts() -> set[str]:
        raw = str(os.environ.get("BRT_ALLOWED_API_HOST") or "")
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    def _validate_allowed_host(self) -> None:
        allowed = self._allowed_hosts()
        if not allowed:
            return
        host = (urlsplit(self.base_url).hostname or "").lower()
        if host not in allowed:
            expected = ", ".join(sorted(allowed))
            raise ValueError(
                f"configured LLM host {host or '<missing>'!r} is not allowed; "
                f"expected one of: {expected}"
            )

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
                if self._requested_model:
                    self.model = self._requested_model
                elif local_model:
                    self.model = local_model
                self._validate_allowed_host()
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

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        attempt_limit: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if os.environ.get("BRT_DISABLE_THINKING", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            payload["thinking"] = {"type": "disabled"}
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if self.stream:
            payload["stream"] = True
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_error: Exception | None = None
        # Bound one logical call independently of the pool size.  Failed
        # instances are retried by the stage-level resume loop, which keeps
        # scheduling fair: one unavailable request cannot occupy a worker
        # forever merely because many credentials are configured.
        pool_size = len(configured_apis(self.provider)) if self._uses_local_pool else 0
        # A logical request tries each configured endpoint at most once.  The
        # caller may resume a failed instance later; one request must never
        # hold a worker indefinitely.
        max_attempts = max(1, min(self.max_attempts, pool_size or self.max_attempts))
        if attempt_limit is not None:
            max_attempts = max(1, min(max_attempts, attempt_limit))
        attempt = 0
        while attempt < max_attempts:
            payload["model"] = self.model
            data = json.dumps(payload).encode("utf-8")
            url = self._chat_url(self.base_url)
            headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                open_request = getattr(self, 'open_request', urllib.request.urlopen)
                with open_request(req, timeout=self.request_timeout) as resp:  # noqa: S310
                    if self.stream:
                        stream_deadline = time.monotonic() + self.request_timeout
                        parts: list[str] = []
                        finish_reason = ""
                        self.last_usage = {}
                        self.last_model = payload["model"]
                        for raw_line in resp:
                            if time.monotonic() >= stream_deadline:
                                raise TimeoutError(
                                    "LLM streaming response exceeded the request deadline"
                                )
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line.startswith("data:"):
                                continue
                            event = line[5:].strip()
                            if not event:
                                continue
                            if event == "[DONE]":
                                break
                            chunk = json.loads(event)
                            self.last_usage = chunk.get("usage") or self.last_usage
                            self.last_model = chunk.get("model") or self.last_model
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            piece = delta.get("content")
                            if isinstance(piece, str):
                                parts.append(piece)
                            finish_reason = str(
                                choices[0].get("finish_reason") or finish_reason
                            )
                        content = "".join(parts)
                    else:
                        body = resp.read().decode("utf-8")
                        parsed = json.loads(body)
                        self.last_usage = parsed.get("usage") or {}
                        self.last_model = parsed.get("model") or payload["model"]
                        choice = parsed["choices"][0]
                        content = choice["message"].get("content")
                        finish_reason = str(choice.get("finish_reason") or "")
                self.last_finish_reason = finish_reason
                if finish_reason == "length":
                    current_limit = int(payload["max_tokens"])
                    payload["max_tokens"] = min(
                        max(current_limit * 2, current_limit + 1),
                        max(current_limit, self.truncation_max_tokens),
                    )
                    raise RuntimeError(
                        "LLM response was truncated at the provider output limit"
                    )
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("LLM returned empty content")
                return content
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"LLM HTTP {exc.code}: {body[:500]}")
                # Malformed/oversized requests are deterministic for this
                # payload. Rotating through the entire key pool only adds a
                # long exponential backoff and cannot make them succeed.
                if exc.code in {400, 404, 405, 413, 422}:
                    break
                if self.immediate_failover or exc.code in {401, 403, 429} or exc.code >= 500:
                    self._rotate_api()
                if not self.immediate_failover and exc.code == 429 and attempt < max_attempts - 1:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        server_wait = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        server_wait = 0.0
                    wait = max(
                        server_wait,
                        min(
                            self.rate_limit_backoff * (2 ** min(attempt, 8)),
                            300.0,
                        ),
                    )
                    time.sleep(wait)
                    attempt += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self._rotate_api()
            attempt += 1
            if attempt < max_attempts:
                time.sleep(
                    (0 if attempt % max(1, len(configured_apis(self.provider))) else 1)
                    if self.immediate_failover else
                    min(self.backoff_base * (2 ** min(attempt - 1, 8)), 180.0)
                )
        raise RuntimeError(f"LLM request failed after {attempt} attempts: {last_error}")

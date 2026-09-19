"""Provider-aware OpenAI-compatible LLM client."""

from __future__ import annotations

import json
import http.client
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
import warnings

from .api_pool import configured_apis, pool_policy
from .cost_tracking import RequestAccounting
from .errors import LLMUnavailableError, quota_exhausted
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
    # Process-local: a restarted experiment can use a replenished account.
    # Never persisted or printed, as the identity contains a credential.
    _disabled_endpoints: set[tuple[str, str]] = set()

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        cost_dir: str | None = None,
        usage_context: dict[str, str] | None = None,
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
        accounting_dir = os.environ.get("BRT_COST_DIR") or cost_dir
        self.accounting = (
            RequestAccounting(accounting_dir, usage_context) if accounting_dir else None
        )
        self._usage_option_unsupported_hosts: set[str] = set()
        self._initial_model = self.model

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
    def _pick_local_api(cls, provider: str, exclude=None) -> tuple[str, str, str] | None:
        entries = configured_apis(provider)
        if not entries:
            return None
        with cls._key_lock:
            start = cls._next_index(provider, len(entries))
            available = []
            for offset in range(len(entries)):
                entry = entries[(start + offset) % len(entries)]
                identity = (entry[1].rstrip("/"), entry[0])
                if identity not in cls._disabled_endpoints:
                    available.append(entry)
                    if identity not in (exclude or set()):
                        return entry
            # Exhausting a rotation is not exhausting the account. A healthy
            # endpoint remains retryable after a transient network failure.
            if available:
                return available[0]
        raise LLMUnavailableError("All configured API credentials are quota exhausted; resume after replenishment")

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

    def _rotate_api(self, exclude=None) -> None:
        if self._uses_local_pool:
            entry = self._pick_local_api(self.provider, exclude)
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

    def ensure_available(self) -> None:
        with self._key_lock:
            disabled = (self.base_url, self.api_key) in self._disabled_endpoints
        if disabled:
            if not self._uses_local_pool:
                raise LLMUnavailableError("API account quota exhausted; resume after replenishment")
            self._rotate_api()

    def request_identity(self) -> dict:
        """Credential-free configuration used only for same-run recovery."""
        endpoints = sorted({(base.rstrip("/"), self._requested_model or model)
                            for _, base, model in configured_apis(self.provider)}) if self._uses_local_pool else [(self.base_url, self._initial_model)]
        return {
            "provider": self.provider, "model": self._initial_model,
            "endpoints": endpoints, "temperature": self.temperature,
            "max_tokens": self.max_tokens, "stream": self.stream,
            "thinking_disabled": os.environ.get("BRT_DISABLE_THINKING", ""),
            "truncation_max_tokens": self.truncation_max_tokens,
        }

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
        from ..runtime.step_journal import current_journal
        journal = current_journal()
        arguments = dict(system_prompt=system_prompt, user_prompt=user_prompt,
                         temperature=temperature, max_tokens=max_tokens,
                         attempt_limit=attempt_limit, reasoning_effort=reasoning_effort,
                         operation=str(sys._getframe(1).f_globals.get("__name__") or "unknown"))
        if journal is None:
            return self._chat_uncached(**arguments)

        def request():
            content = self._chat_uncached(**arguments)
            return {"content": content, "usage": self.last_usage,
                    "model": self.last_model, "finish_reason": self.last_finish_reason}

        saved = journal.step("llm", {"configuration": self.request_identity(), **arguments}, request)
        self.last_usage = saved["usage"]
        self.last_model = saved["model"]
        self.last_finish_reason = saved["finish_reason"]
        return saved["content"]

    def _chat_uncached(
        self, system_prompt: str, user_prompt: str,
        temperature: float | None = None, max_tokens: int | None = None,
        attempt_limit: int | None = None, reasoning_effort: str | None = None,
        operation: str = "unknown",
    ) -> str:
        self.ensure_available()
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
            if self.accounting and urlsplit(self.base_url).hostname not in self._usage_option_unsupported_hosts:
                payload["stream_options"] = {"include_usage": True}
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_error: Exception | None = None
        # Keep a bounded default for probes and library callers. Full runs may
        # explicitly retry transient service failures without replaying the
        # instance's already completed model and execution steps.
        pool_size = len(configured_apis(self.provider)) if self._uses_local_pool else 0
        max_attempts = max(1, min(self.max_attempts, pool_size or self.max_attempts))
        if attempt_limit is not None:
            max_attempts = max(1, min(max_attempts, attempt_limit))
        retry_transient = attempt_limit is None and os.environ.get(
            "BRT_RETRY_TRANSIENT_API", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        transient_error = False
        attempt = 0
        attempted_endpoints = set()
        logical_request_id = str(uuid.uuid4())
        while attempt < max_attempts or (retry_transient and transient_error):
            self.ensure_available()
            transient_error = False
            attempted_endpoints.add((self.base_url, self.api_key))
            payload["model"] = self.model
            data = json.dumps(payload).encode("utf-8")
            url = self._chat_url(self.base_url)
            headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            request_host = urlsplit(url).hostname or ""
            requested_model = payload["model"]
            started = time.perf_counter()
            attempt_id = None
            response_metadata = {}
            outcome = "interrupted"
            status_code = None
            finish_reason = ""
            # Reset these before every attempt; a failed retry must not inherit
            # the previous response's usage or be charged for it twice.
            self.last_usage = {}
            self.last_model = requested_model
            if self.accounting:
                try:
                    attempt_id = self.accounting.start(
                        logical_request_id=logical_request_id, attempt=attempt + 1,
                        host=request_host, requested_model=requested_model,
                        operation=operation, max_tokens=payload["max_tokens"],
                    )
                except Exception as exc:
                    warnings.warn(f"API request accounting failed ({type(exc).__name__})")
            try:
                open_request = getattr(self, 'open_request', urllib.request.urlopen)
                with open_request(req, timeout=self.request_timeout) as resp:  # noqa: S310
                    status_code = getattr(resp, "status", None) or 200
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
                            response_metadata.update({k: v for k, v in chunk.items()
                                                      if k != "choices"})
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
                        response_metadata = {k: v for k, v in parsed.items() if k != "choices"}
                        self.last_usage = parsed.get("usage") or {}
                        self.last_model = parsed.get("model") or payload["model"]
                        choice = parsed["choices"][0]
                        content = choice["message"].get("content")
                        finish_reason = str(choice.get("finish_reason") or "")
                self.last_finish_reason = finish_reason
                if finish_reason == "length":
                    outcome = "truncated"
                    output_limit = int(payload.get("max_completion_tokens", payload.get("max_tokens", 0)))
                    if output_limit >= self.truncation_max_tokens:
                        raise LLMUnavailableError(
                            "Response truncated at configured output cap; paused before repeated paid requests"
                        )
                    current_limit = int(payload["max_tokens"])
                    payload["max_tokens"] = min(
                        max(current_limit * 2, current_limit + 1),
                        max(current_limit, self.truncation_max_tokens),
                    )
                    raise RuntimeError(
                        "LLM response was truncated at the provider output limit"
                    )
                if not isinstance(content, str) or not content.strip():
                    outcome = "empty_response"
                    raise RuntimeError("LLM returned empty content")
                outcome = "success"
                return content
            except LLMUnavailableError:
                raise
            except urllib.error.HTTPError as exc:
                status_code = exc.code
                outcome = "http_error"
                body = exc.read().decode("utf-8", errors="replace")
                try:
                    error_data = json.loads(body)
                    if isinstance(error_data, dict):
                        response_metadata = {k: v for k, v in error_data.items() if k != "choices"}
                except (ValueError, TypeError):
                    pass
                # Provider bodies can contain account identifiers. Persist only
                # the status and error category, never credentials or raw bodies.
                exhausted = quota_exhausted(body)
                transient_error = not exhausted and (exc.code in {408, 425, 429} or exc.code >= 500)
                last_error = RuntimeError(f"LLM HTTP {exc.code}: " + ("quota exhausted" if exhausted else "provider rejected request"))
                if exhausted:
                    with self._key_lock:
                        self._disabled_endpoints.add((self.base_url, self.api_key))
                if (
                    exc.code in {400, 422}
                    and "stream_options" in payload
                    and ("stream_options" in body or "include_usage" in body)
                ):
                    # Some compatible gateways reject the optional usage
                    # switch. Retry once without it, preserving the original
                    # prompts and generation parameters; usage may be unknown.
                    self._usage_option_unsupported_hosts.add(request_host)
                    payload.pop("stream_options")
                    max_attempts = max(max_attempts, attempt + 2)
                    attempt += 1
                    continue
                # Malformed/oversized requests are deterministic for this
                # payload. Rotating through the entire key pool only adds a
                # long exponential backoff and cannot make them succeed.
                if exc.code in {400, 404, 405, 413, 422} and not exhausted:
                    break
                if not self.immediate_failover and exc.code == 429 and attempt < max_attempts - 1:
                    self._rotate_api(attempted_endpoints)
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
                if outcome == "interrupted":
                    outcome = "request_error"
                last_error = exc
                # A received but unusable model response is not a test
                # failure. Full runs retry it too, retaining the existing
                # output-limit escalation and never caching partial content.
                transient_error = outcome in {"empty_response", "truncated"} or isinstance(exc, (
                    urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.HTTPException,
                ))
            finally:
                if attempt_id and self.accounting:
                    try:
                        if self.last_usage:
                            response_metadata["usage"] = self.last_usage
                        self.accounting.finish(
                            attempt_id, host=request_host, model=requested_model,
                            response=response_metadata, outcome=outcome,
                            status_code=status_code, finish_reason=finish_reason,
                            elapsed_seconds=time.perf_counter() - started,
                        )
                    except Exception as exc:
                        # A successful model response must never be requested
                        # again merely because writing accounting data failed.
                        warnings.warn(f"API usage could not be saved ({type(exc).__name__})")
            attempt += 1
            if attempt < max_attempts or (retry_transient and transient_error):
                self._rotate_api(attempted_endpoints)
                if retry_transient and transient_error and attempt >= max_attempts:
                    # Small jitter avoids synchronized retries across workers.
                    wait = min(15 * (2 ** min(attempt - max_attempts, 2)), 60) + uuid.uuid4().int % 10
                    status = f"HTTP {status_code}" if status_code else type(last_error).__name__
                    print(f"[API retry] {operation}: {status}; attempt {attempt + 1} in {wait}s; completed steps retained", flush=True)
                    time.sleep(wait)
                    continue
                time.sleep(
                    (0 if attempt % max(1, len(configured_apis(self.provider))) else 1)
                    if self.immediate_failover else
                    min(self.backoff_base * (2 ** min(attempt - 1, 8)), 180.0)
                )
        raise LLMUnavailableError(f"LLM request failed after {max(1, attempt)} attempts: {last_error}")

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from brt6.llm.llm_client import LLMClient


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        policy = patch("brt6.llm.llm_client.pool_policy", return_value={})
        policy.start()
        self.addCleanup(policy.stop)

        LLMClient._key_indices.clear()

    def test_gpt_provider_uses_only_gpt_pool_and_model(self) -> None:
        pools = {
            "deepseek": [("deepseek-key", "https://deepseek.invalid", "deepseek-v3")],
            "gpt": [("gpt-key", "https://gpt.invalid/v1", "gpt-5.4-mini")],
        }
        with patch(
            "brt6.llm.llm_client.configured_apis",
            side_effect=lambda provider=None: pools.get(provider, []),
        ) as configured:
            client = LLMClient(provider="gpt", model="gpt-5.4-mini")

        self.assertEqual(client.provider, "gpt")
        self.assertEqual(client.api_key, "gpt-key")
        self.assertEqual(client.base_url, "https://gpt.invalid/v1")
        self.assertEqual(client.model, "gpt-5.4-mini")
        configured.assert_called_with("gpt")

    def test_explicit_model_is_not_overridden_by_pool_alias(self) -> None:
        with patch(
            "brt6.llm.llm_client.configured_apis",
            return_value=[("key", "https://api.example.test/v1", "pool-alias")],
        ):
            client = LLMClient(provider="deepseek", model="requested-model")

        self.assertEqual(client.model, "requested-model")

    def test_allowed_api_host_rejects_mismatched_pool(self) -> None:
        with patch(
            "brt6.llm.llm_client.configured_apis",
            return_value=[("key", "https://wrong.example/v1", "model")],
        ), patch.dict(
            "os.environ",
            {"BRT_ALLOWED_API_HOST": "api.expected.example"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "is not allowed"):
                LLMClient(provider="deepseek", model="model")

    def test_allowed_api_host_accepts_matching_pool(self) -> None:
        with patch(
            "brt6.llm.llm_client.configured_apis",
            return_value=[("key", "https://api.expected.example/v1", "model")],
        ), patch.dict(
            "os.environ",
            {"BRT_ALLOWED_API_HOST": "api.expected.example"},
            clear=False,
        ):
            client = LLMClient(provider="deepseek", model="model")

        self.assertEqual(client.base_url, "https://api.expected.example/v1")

    def test_non_retryable_http_error_stops_after_one_request(self) -> None:
        client = LLMClient(
            api_key="test-key",
            base_url="https://example.invalid",
        )
        error = urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"context length exceeded"}'),
        )
        with patch(
            "brt6.llm.llm_client.urllib.request.urlopen",
            side_effect=error,
        ) as request, patch("brt6.llm.llm_client.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "LLM HTTP 400"):
                client.chat("system", "oversized prompt")

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_reasoning_effort_is_sent_only_when_requested(self) -> None:
        client = LLMClient(api_key="test-key", base_url="https://example.invalid")
        captured = []

        def open_request(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(
                json.dumps({
                    "model": "test-model",
                    "usage": {},
                    "choices": [{"message": {"content": "OK"}}],
                }).encode("utf-8")
            )

        client.open_request = open_request
        client.chat("system", "user")
        client.chat("system", "user", reasoning_effort="none")

        self.assertNotIn("reasoning_effort", captured[0])
        self.assertEqual(captured[1]["reasoning_effort"], "none")

    def test_gpt_uses_openai_token_and_reasoning_fields(self) -> None:
        client = LLMClient(
            provider="gpt",
            model="gpt-5.4-mini",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
        captured = []

        def open_request(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(
                json.dumps({
                    "model": "gpt-5.4-mini",
                    "usage": {},
                    "choices": [{
                        "message": {"content": "OK"},
                        "finish_reason": "stop",
                    }],
                }).encode("utf-8")
            )

        client.open_request = open_request
        with patch.dict(os.environ, {"BRT_DISABLE_THINKING": "1"}, clear=False):
            client.chat("system", "user")

        self.assertEqual(captured[0]["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", captured[0])
        self.assertEqual(captured[0]["reasoning_effort"], "none")
        self.assertNotIn("thinking", captured[0])

    def test_gpt5_mini_disable_thinking_uses_low_reasoning(self) -> None:
        client = LLMClient(
            provider="gpt",
            model="gpt-5-mini",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
        captured = []

        def open_request(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(
                json.dumps({
                    "model": "gpt-5-mini",
                    "usage": {},
                    "choices": [{
                        "message": {"content": "OK"},
                        "finish_reason": "stop",
                    }],
                }).encode("utf-8")
            )

        client.open_request = open_request
        with patch.dict(os.environ, {"BRT_DISABLE_THINKING": "1"}, clear=False):
            client.chat("system", "user")

        self.assertEqual(captured[0]["reasoning_effort"], "low")

    def test_gpt5_mini_accepts_versioned_model_identity(self) -> None:
        client = LLMClient(
            provider="gpt", model="gpt-5-mini", api_key="test-key",
            base_url="https://example.invalid/v1",
        )

        def open_request(request, timeout):
            return io.BytesIO(json.dumps({
                "model": "gpt-5-mini-2025-08-07",
                "usage": {},
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }).encode("utf-8"))

        client.open_request = open_request
        self.assertEqual(client.chat("system", "user", attempt_limit=1), "OK")

    def test_gpt5_mini_rejects_different_returned_model_family(self) -> None:
        client = LLMClient(
            provider="gpt", model="gpt-5-mini", api_key="test-key",
            base_url="https://example.invalid/v1",
        )

        def open_request(request, timeout):
            return io.BytesIO(json.dumps({
                "model": "gpt-5.4-mini",
                "usage": {},
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            }).encode("utf-8"))

        client.open_request = open_request
        with self.assertRaisesRegex(RuntimeError, "different model family"):
            client.chat("system", "user", attempt_limit=1)

    def test_empty_content_is_retried(self) -> None:
        client = LLMClient(api_key="test-key", base_url="https://example.invalid")
        bodies = iter(
            [
                {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
                {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
            ]
        )

        def open_request(request, timeout):
            return io.BytesIO(json.dumps(next(bodies)).encode("utf-8"))

        client.open_request = open_request
        with patch("brt6.llm.llm_client.time.sleep"):
            self.assertEqual(client.chat("system", "user"), "OK")

    def test_length_finish_reason_is_retried(self) -> None:
        client = LLMClient(api_key="test-key", base_url="https://example.invalid")
        captured = []
        bodies = iter(
            [
                {"choices": [{"message": {"content": "{\"x\":"}, "finish_reason": "length"}]},
                {"choices": [{"message": {"content": "{\"x\":1}"}, "finish_reason": "stop"}]},
            ]
        )

        def open_request(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(json.dumps(next(bodies)).encode("utf-8"))

        client.open_request = open_request
        with patch("brt6.llm.llm_client.time.sleep"):
            self.assertEqual(client.chat("system", "user"), "{\"x\":1}")
        self.assertEqual(captured[0]["max_tokens"], 4096)
        self.assertEqual(captured[1]["max_tokens"], 8192)

    def test_gpt_length_retry_increases_max_completion_tokens(self) -> None:
        client = LLMClient(
            provider="gpt",
            model="gpt-5.4-mini",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        )
        captured = []
        bodies = iter(
            [
                {"choices": [{"message": {"content": "{\"x\":"}, "finish_reason": "length"}]},
                {"choices": [{"message": {"content": "{\"x\":1}"}, "finish_reason": "stop"}]},
            ]
        )

        def open_request(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(json.dumps(next(bodies)).encode("utf-8"))

        client.open_request = open_request
        with patch("brt6.llm.llm_client.time.sleep"):
            self.assertEqual(client.chat("system", "user"), "{\"x\":1}")
        self.assertEqual(captured[0]["max_completion_tokens"], 4096)
        self.assertEqual(captured[1]["max_completion_tokens"], 8192)

    def test_call_attempts_are_not_expanded_to_pool_size(self) -> None:
        client = LLMClient(api_key="test-key", base_url="https://example.invalid")
        client.max_attempts = 3
        client.wait_forever = False

        with patch(
            "brt6.llm.llm_client.configured_apis",
            return_value=[("key", "https://example.invalid", "model")] * 13,
        ), patch.object(
            client,
            "open_request",
            side_effect=TimeoutError("timed out"),
            create=True,
        ) as request, patch("brt6.llm.llm_client.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                client.chat("system", "user")

        self.assertEqual(request.call_count, 3)

    def test_streaming_response_joins_content_and_records_usage(self) -> None:
        client = LLMClient(api_key="test-key", base_url="https://example.invalid")
        client.stream = True
        captured = []
        stream = b"".join([
            b'data: {"model":"test-model","choices":[{"delta":{"content":"hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
            b'data: {"usage":{"total_tokens":3},"choices":[]}\n\n',
            b'data: [DONE]\n\n',
        ])

        def open_request(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(stream)

        client.open_request = open_request
        self.assertEqual(client.chat("system", "user"), "hello")
        self.assertTrue(captured[0]["stream"])
        self.assertEqual(client.last_usage, {"total_tokens": 3})
        self.assertEqual(client.last_model, "test-model")

    def test_streaming_response_stops_at_done_without_waiting_for_eof(self) -> None:
        client = LLMClient(api_key="test-key", base_url="https://example.invalid")
        client.stream = True

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
                yield b"data: [DONE]\n\n"
                raise AssertionError("client read past the stream terminator")

        client.open_request = lambda request, timeout: Response()
        self.assertEqual(client.chat("system", "user"), "OK")

    def test_streaming_response_has_wall_clock_deadline(self) -> None:
        client = LLMClient(api_key="test-key", base_url="https://example.invalid")
        client.stream = True
        client.request_timeout = 10
        client.max_attempts = 1

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n'

        client.open_request = lambda request, timeout: Response()
        with patch(
            "brt6.llm.llm_client.time.monotonic",
            side_effect=[100.0, 111.0],
        ):
            with self.assertRaisesRegex(RuntimeError, "request deadline"):
                client.chat("system", "user")

    def test_immediate_failover_is_bounded_to_one_pool_cycle(self) -> None:
        with patch(
            "brt6.llm.llm_client.pool_policy",
            return_value={"immediate_failover": True},
        ), patch(
            "brt6.llm.llm_client.configured_apis",
            return_value=[
                ("key-1", "https://one.example/v1", "model"),
                ("key-2", "https://two.example/v1", "model"),
            ],
        ):
            client = LLMClient(provider="deepseek", model="model")
            client.max_attempts = 10
            with patch.object(
                client,
                "open_request",
                side_effect=TimeoutError("timed out"),
                create=True,
            ) as request, patch("brt6.llm.llm_client.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "after 2 attempts"):
                    client.chat("system", "user")

        self.assertEqual(request.call_count, 2)

    def test_explicit_pool_rotation_attempts_can_cycle_pool(self) -> None:
        with patch.dict(
            os.environ,
            {"BRT_LLM_POOL_ROTATION_ATTEMPTS": "10"},
        ), patch(
            "brt6.llm.llm_client.pool_policy",
            return_value={"immediate_failover": True},
        ), patch(
            "brt6.llm.llm_client.configured_apis",
            return_value=[
                ("key-1", "https://one.example/v1", "model"),
                ("key-2", "https://two.example/v1", "model"),
            ],
        ):
            client = LLMClient(provider="deepseek", model="model")
            with patch.object(
                client,
                "open_request",
                side_effect=TimeoutError("timed out"),
                create=True,
            ) as request, patch("brt6.llm.llm_client.time.sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "after 10 attempts"):
                    client.chat("system", "user")

        self.assertEqual(request.call_count, 10)
        self.assertEqual(sleep.call_count, 9)


if __name__ == "__main__":
    unittest.main()

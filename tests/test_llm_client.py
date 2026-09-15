from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from brt6.llm.llm_client import LLMClient


class LLMClientTests(unittest.TestCase):
    def setUp(self) -> None:
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


if __name__ == "__main__":
    unittest.main()

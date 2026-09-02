from __future__ import annotations

import io
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


if __name__ == "__main__":
    unittest.main()

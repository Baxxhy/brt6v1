from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from brt6.llm.cost_tracking import RequestAccounting, write_cost_summary
from brt6.llm.llm_client import LLMClient


class CostTrackingTests(unittest.TestCase):
    def setUp(self):
        LLMClient._disabled_endpoints.clear()
        root = Path(__file__).resolve().parents[1] / ".runtime"
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        prices = self.root / "prices.json"
        prices.write_text(json.dumps({"rates": [{
            "host": "example.invalid", "model": "test-model", "currency": "CNY",
            "input_per_million": 2, "output_per_million": 4,
        }]}))
        env = patch.dict(os.environ, {
            "BRT_COST_DIR": str(self.root), "BRT_LLM_PRICING_FILE": str(prices),
            "BRT_LLM_STREAM": "0", "BRT_ALLOWED_API_HOST": "",
        })
        env.start()
        self.addCleanup(env.stop)
        hook = patch("brt6.llm.cost_tracking.atexit.register")
        hook.start()
        self.addCleanup(hook.stop)

    def client(self):
        return LLMClient(api_key="secret-never-log-me", base_url="https://example.invalid",
                         model="test-model", usage_context={"stage": "design2", "instance_id": "sample"})

    @staticmethod
    def response(content="OK", finish="stop", usage=None):
        return io.BytesIO(json.dumps({"usage": usage or {}, "choices": [{
            "message": {"content": content}, "finish_reason": finish,
        }]}).encode())

    def test_retry_accounts_both_paid_responses_and_keeps_prompt_secret(self):
        client = self.client()
        replies = [self.response("partial", "length", {"prompt_tokens": 100, "completion_tokens": 20}),
                   self.response(usage={"prompt_tokens": 100, "completion_tokens": 30})]
        with patch.object(client, "open_request", side_effect=replies, create=True), \
                patch("brt6.llm.llm_client.time.sleep"):
            self.assertEqual(client.chat("private-system", "private-user"), "OK")
        report = write_cost_summary(self.root)
        self.assertEqual(report["request_attempts"], 2)
        self.assertEqual(report["logical_requests"], 1)
        self.assertEqual(report["retry_attempts"], 1)
        self.assertEqual(report["outcomes"], {"truncated": 1, "success": 1})
        self.assertEqual(float(report["total_cost"]["amount"]), 0.0006)
        ledger = (self.root / "api_requests.jsonl").read_text()
        for secret in ("secret-never-log-me", "private-system", "private-user", "partial"):
            self.assertNotIn(secret, ledger)

    def test_missing_usage_and_http_failure_are_unknown_not_free(self):
        client = self.client()
        client.max_attempts = 1
        with patch.object(client, "open_request", return_value=self.response(), create=True):
            client.chat("s", "u")
        error = urllib.error.HTTPError("https://example.invalid", 403, "quota", {},
                                      io.BytesIO(b'{"error":"insufficient_user_quota"}'))
        with patch.object(client, "open_request", side_effect=error, create=True):
            with self.assertRaises(RuntimeError):
                client.chat("s", "u")
        report = write_cost_summary(self.root)
        self.assertEqual(report["request_attempts"], 2)
        self.assertEqual(report["unknown_cost_attempts"], 2)
        self.assertIsNone(report["total_cost"])

    def test_http_failure_cannot_reuse_previous_usage(self):
        client = self.client()
        client.max_attempts = 1
        with patch.object(client, "open_request", return_value=self.response(
                usage={"prompt_tokens": 100, "completion_tokens": 20}), create=True):
            client.chat("s", "u")
        with patch.object(client, "open_request", side_effect=TimeoutError(), create=True):
            with self.assertRaises(RuntimeError):
                client.chat("s", "u")
        report = write_cost_summary(self.root)
        self.assertEqual(report["known_token_totals"]["prompt_tokens"], 100)
        self.assertEqual(report["unknown_cost_attempts"], 1)

    def test_streaming_usage_is_requested_and_recorded(self):
        client = self.client()
        client.stream = True
        bodies = b''.join([
            b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n',
            b'data: {"usage":{"prompt_tokens":100,"completion_tokens":20},"choices":[]}\n',
            b'data: [DONE]\n',
        ])
        def respond(req, timeout):
            self.assertTrue(json.loads(req.data)["stream_options"]["include_usage"])
            return io.BytesIO(bodies)
        client.open_request = respond
        self.assertEqual(client.chat("s", "u"), "OK")
        self.assertEqual(float(write_cost_summary(self.root)["total_cost"]["amount"]), 0.00028)

    def test_gateway_rejecting_usage_option_falls_back_without_changing_prompt(self):
        client = self.client()
        client.stream = True
        client.max_attempts = 1
        captured = []
        def respond(req, timeout):
            payload = json.loads(req.data)
            captured.append(payload)
            if "stream_options" in payload:
                raise urllib.error.HTTPError("https://example.invalid", 400, "unsupported", {},
                                             io.BytesIO(b'unknown parameter: stream_options'))
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\ndata: [DONE]\n')
        client.open_request = respond
        self.assertEqual(client.chat("s", "u"), "OK")
        self.assertEqual(client.chat("s", "u"), "OK")
        self.assertEqual(len(captured), 3)
        self.assertEqual(captured[0]["messages"], captured[1]["messages"])
        self.assertNotIn("stream_options", captured[2])
        report = write_cost_summary(self.root)
        self.assertEqual(report["request_attempts"], 3)
        self.assertEqual(report["unknown_cost_attempts"], 3)

    def test_reported_cost_takes_precedence_and_cache_reasoning_not_double_charged(self):
        prices = self.root / "prices.json"
        data = json.loads(prices.read_text())
        data["rates"][0].update(reported_cost_field="usage.billed_money", cached_input_per_million=0.5)
        prices.write_text(json.dumps(data))
        client = self.client()
        with patch.object(client, "open_request", return_value=self.response(usage={
            "prompt_tokens": 100, "completion_tokens": 20, "billed_money": "0.01",
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 10},
        }), create=True):
            client.chat("s", "u")
        report = write_cost_summary(self.root)
        self.assertEqual(report["total_cost"], {"amount": "0.01", "currency": "CNY", "basis": "reported"})
        with patch.object(client, "open_request", return_value=self.response(usage={
            "prompt_tokens": 100, "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 10},
        }), create=True):
            client.chat("s", "u")
        report = write_cost_summary(self.root)
        self.assertEqual(float(report["total_cost"]["amount"]), 0.01022)
        self.assertEqual(report["total_cost"]["basis"], "mixed")

    def test_resume_is_cumulative_and_concurrent_writes_are_complete(self):
        def call(_):
            client = self.client()
            client.open_request = lambda *a, **kw: self.response(usage={"prompt_tokens": 100, "completion_tokens": 20})
            client.chat("s", "u")
        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(call, range(40)))
        first = write_cost_summary(self.root)
        call(0)  # A new process/client resumes into the same ledger directory.
        second = write_cost_summary(self.root)
        self.assertEqual(first["request_attempts"], 40)
        self.assertEqual(second["request_attempts"], 41)
        self.assertEqual(second["malformed_ledger_lines"], 0)
        self.assertEqual(Decimal(second["total_cost"]["amount"]), 41 * Decimal("0.00028"))

    def test_interrupted_attempt_prevents_false_complete_total(self):
        recorder = RequestAccounting(self.root)
        recorder.start(logical_request_id="test", attempt=1, host="example.invalid", requested_model="test-model")
        report = write_cost_summary(self.root)
        self.assertEqual(report["outcomes"], {"interrupted_or_inflight": 1})
        self.assertIsNone(report["total_cost"])

    def test_missing_ledger_is_not_reported_as_zero_cost(self):
        report = write_cost_summary(self.root)
        self.assertFalse(report["ledger_present"])
        self.assertIsNone(report["total_cost"])

    def test_mixed_currencies_are_not_added_into_one_total(self):
        client = self.client()
        client.open_request = lambda *a, **kw: self.response(usage={"prompt_tokens": 100, "completion_tokens": 20})
        client.chat("s", "u")
        prices = self.root / "prices.json"
        data = json.loads(prices.read_text())
        data["rates"][0]["currency"] = "USD"
        prices.write_text(json.dumps(data))
        second = self.client()
        second.open_request = client.open_request
        second.chat("s", "u")
        report = write_cost_summary(self.root)
        self.assertEqual(report["unknown_cost_attempts"], 0)
        self.assertEqual(report["known_subtotal_by_currency"], {"CNY": "0.00028", "USD": "0.00028"})
        self.assertIsNone(report["total_cost"])

    def test_incomplete_ledger_line_keeps_totals_explicitly_incomplete(self):
        client = self.client()
        client.open_request = lambda *a, **kw: self.response(usage={"prompt_tokens": 100, "completion_tokens": 20})
        client.chat("s", "u")
        with (self.root / "api_requests.jsonl").open("a") as out:
            out.write('{"event":')
        report = write_cost_summary(self.root)
        self.assertEqual(report["malformed_ledger_lines"], 1)
        self.assertEqual(report["known_subtotal_by_currency"], {"CNY": "0.00028"})
        self.assertIsNone(report["total_cost"])

    def test_unknown_prices_can_be_filled_later_without_recounting(self):
        with patch.dict(os.environ, {"BRT_LLM_PRICING_FILE": ""}):
            client = self.client()
            client.open_request = lambda *a, **kw: self.response(usage={"prompt_tokens": 100, "completion_tokens": 20})
            client.chat("s", "u")
        self.assertIsNone(write_cost_summary(self.root)["total_cost"])
        priced = write_cost_summary(self.root, self.root / "prices.json")
        self.assertEqual(float(priced["total_cost"]["amount"]), 0.00028)
        self.assertEqual(priced["request_attempts"], 1)

    def test_accounting_failure_does_not_repeat_a_successful_model_request(self):
        client = self.client()
        with patch.object(client, "open_request", return_value=self.response(), create=True) as request, \
                patch.object(client.accounting, "finish", side_effect=OSError("disk full")), \
                self.assertWarns(UserWarning):
            self.assertEqual(client.chat("s", "u"), "OK")
        self.assertEqual(request.call_count, 1)

    def test_process_exit_and_summary_command_preserve_accumulated_cost(self):
        project = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(project.parent)}
        code = '''
import io, json
from brt6.llm.llm_client import LLMClient
client = LLMClient(api_key="test-only", base_url="https://example.invalid", model="test-model")
client.open_request = lambda *a, **kw: io.BytesIO(json.dumps({
    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]
}).encode())
client.chat("s", "u")
'''
        children = [subprocess.Popen([sys.executable, "-c", code], cwd=project, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(3)]
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stdout + stderr)
        report = json.loads((self.root / "api_cost_summary.json").read_text())
        self.assertEqual(report["request_attempts"], 3)
        self.assertEqual(report["total_cost"]["amount"], "0.00084")
        cli = subprocess.run([sys.executable, str(project / "scripts/summarize_api_cost.py"),
                              "--run-dir", str(self.root)], env=env, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["request_attempts"], 3)
        self.assertTrue((self.root / "api_cost_summary.md").exists())


if __name__ == "__main__":
    unittest.main()

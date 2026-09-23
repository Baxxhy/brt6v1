"""Offline fault injection: no network, real feedback control flow and journals."""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from collections import Counter
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brt6.core.prompts import SEED_MUTATION_PLAN_SYSTEM_PROMPT
from brt6.core.schema import (BehaviorTarget, ExecutionResult, HostContext,
                             FinalResult, InstanceContext, ProtocolRecovery, RetrievedTest,
                             StrictVerifierResult, VerifierDecision)
from brt6.execution.feedback import _load_cached_behavior, _propose_delta_safely, run_instance_pipeline
from brt6.llm.errors import LLMResponseTruncatedError, LLMUnavailableError
from brt6.llm.llm_client import LLMClient
from brt6.mutation.seed_mutator import propose_semantic_delta
from brt6.pipeline.run import _run_one, build_parser
from brt6.pipeline.run_issue_rewrite import run_one as rewrite_one
from brt6.runtime.step_journal import StepJournal, atomic_json, init_resume_run


class ResumeTests(unittest.TestCase):
    def setUp(self):
        LLMClient._disabled_endpoints.clear()
        LLMClient._key_indices.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "BRT_ALLOWED_API_HOST": "", "BRT_LLM_STREAM": "0", "BRT_COST_DIR": "",
            "BRT_DISABLE_THINKING": "1", "BRT4_BEHAVIOR_CACHE_DIR": "",
            "BRT_RETRY_TRANSIENT_API": "0",
        }))
        self.stack.enter_context(patch("brt6.llm.llm_client.pool_policy", return_value={}))
        self.stack.enter_context(patch("brt6.llm.llm_client.time.sleep"))
        root = Path(__file__).resolve().parents[1] / ".runtime" / "offline_tests"
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=root)))

    def client(self):
        c = LLMClient(api_key="offline-key", base_url="https://offline.invalid", model="offline-model")
        c.max_attempts = 1
        return c

    @staticmethod
    def response(content):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 11, "completion_tokens": 3}}).encode())

    @staticmethod
    def http(code, text):
        return urllib.error.HTTPError("https://offline.invalid", code, "error", {}, io.BytesIO(text.encode()))

    def test_quota_disables_endpoint_and_rotates_without_revisiting(self):
        entries = [("key-a", "https://a.invalid", "m"), ("key-b", "https://b.invalid", "m")]
        with patch("brt6.llm.llm_client.configured_apis", return_value=entries):
            c = LLMClient(model="m")
            c.max_attempts = 2
            # Another worker advanced the pool cursor: failover must still
            # exclude the endpoint already used by this logical request.
            LLMClient._key_indices["deepseek"] = 0
            seen = []
            def request(req, timeout):
                seen.append(req.full_url)
                if "a.invalid" in req.full_url:
                    raise self.http(403, '{"error":{"code":"insufficient_user_quota"}}')
                return self.response("OK")
            c.open_request = request
            self.assertEqual(c.chat("s", "u"), "OK")
            self.assertEqual(len(seen), 2)
            self.assertIn("b.invalid", seen[-1])
            self.assertEqual(LLMClient(model="m").api_key, "key-b")
            c.open_request = lambda *a, **kw: (_ for _ in ()).throw(self.http(403, "insufficient_quota"))
            with self.assertRaises(LLMUnavailableError):
                c.chat("s", "u2")
            with self.assertRaises(LLMUnavailableError):
                LLMClient(model="m")

    def test_rate_limit_is_not_account_exhaustion(self):
        c = self.client()
        c.open_request = Mock(side_effect=self.http(429, "rate limit exceeded"))
        with self.assertRaises(LLMUnavailableError):
            c.chat("s", "u")
        self.assertFalse(LLMClient._disabled_endpoints)
        c.open_request = lambda *a, **kw: self.response("OK")
        self.assertEqual(c.chat("s", "u"), "OK")

    def test_transient_failure_retries_last_available_key(self):
        entries = [("key-a", "https://a.invalid", "m"), ("key-b", "https://b.invalid", "m")]
        with patch("brt6.llm.llm_client.configured_apis", return_value=entries):
            LLMClient._disabled_endpoints.add(("https://a.invalid", "key-a"))
            c = LLMClient(model="m")
            c.max_attempts = 2
            requests = []
            def request(req, timeout):
                requests.append((req.full_url, req.data))
                if len(requests) == 1:
                    raise self.http(524, "gateway timeout")
                return self.response("OK")
            c.open_request = request
            self.assertEqual(c.chat("s", "u"), "OK")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], requests[1])
            self.assertIn("b.invalid", requests[0][0])

    def test_full_run_retries_only_transient_errors_and_replays_completed_steps(self):
        with patch.dict(os.environ, {"BRT_RETRY_TRANSIENT_API": "1"}):
            c = self.client()
            c.open_request = Mock(side_effect=[self.response("first"),
                self.http(524, "gateway timeout"), TimeoutError("timeout"),
                self.http(429, "rate limited"), self.response("second")])
            for _ in range(2):
                with StepJournal(self.root, {"experiment": "transient"}).activate():
                    self.assertEqual(c.chat("s", "first"), "first")
                    self.assertEqual(c.chat("s", "second"), "second")
            self.assertEqual(c.open_request.call_count, 5)
            calls = c.open_request.call_args_list
            self.assertTrue(all(x.args[0].data == calls[1].args[0].data for x in calls[1:]))

    def test_full_run_stops_for_quota_invalid_request_and_explicit_probe_limit(self):
        with patch.dict(os.environ, {"BRT_RETRY_TRANSIENT_API": "1"}):
            for code, body in [(403, "insufficient_user_quota"), (400, "bad request")]:
                with self.subTest(code=code):
                    LLMClient._disabled_endpoints.clear()
                    c = self.client()
                    c.open_request = Mock(side_effect=self.http(code, body))
                    with self.assertRaises(LLMUnavailableError):
                        c.chat("s", "u")
                    self.assertEqual(c.open_request.call_count, 1)
            c.open_request = Mock(side_effect=self.http(524, "gateway timeout"))
            with self.assertRaises(LLMUnavailableError):
                c.chat("s", "probe", attempt_limit=1)
            self.assertEqual(c.open_request.call_count, 1)

    def test_output_cap_is_local_failure_without_saving_partial_content(self):
        with patch.dict(os.environ, {"BRT_RETRY_TRANSIENT_API": "1"}):
            c = self.client()
            c.max_tokens = 4096
            c.truncation_max_tokens = 8192
            def partial():
                return io.BytesIO(json.dumps({"choices": [{"message": {"content": "partial"},
                    "finish_reason": "length"}]}).encode())
            c.open_request = Mock(side_effect=[self.response(""), partial(), partial(), self.response("complete")])
            with StepJournal(self.root, {"experiment": "incomplete_response"}).activate():
                with self.assertRaisesRegex(LLMResponseTruncatedError, "output cap"):
                    c.chat("s", "u")
            self.assertEqual(c.open_request.call_count, 3)
            self.assertEqual(list(self.root.glob('.resume/steps/*/step_*.json')), [])
            # A later explicit resume can succeed and cache only complete output.
            for _ in range(2):
                with StepJournal(self.root, {"experiment": "incomplete_response"}).activate():
                    self.assertEqual(c.chat("s", "u"), "complete")
            self.assertEqual(c.open_request.call_count, 4)
            self.assertEqual([json.loads(x.args[0].data)["max_tokens"]
                              for x in c.open_request.call_args_list], [4096, 4096, 8192, 4096])
            saved = list(self.root.glob('.resume/steps/*/step_*.json'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(saved[0].read_text())['result']['content'], 'complete')

    def test_twenty_workers_keep_step_positions_isolated(self):
        calls = []
        lock = threading.Lock()
        def worker(index):
            c = self.client()
            def request(req, timeout):
                with lock:
                    calls.append(index)
                return self.response(str(index))
            c.open_request = request
            with StepJournal(self.root / f"worker_{index}", {"id": index}).activate():
                return [c.chat("s", "same"), c.chat("s", "same")]
        with ThreadPoolExecutor(max_workers=20) as pool:
            first = list(pool.map(worker, range(20)))
            second = list(pool.map(worker, range(20)))
        self.assertEqual(first, second)
        self.assertEqual(first, [[str(i), str(i)] for i in range(20)])
        self.assertEqual(len(calls), 40)

    def test_quota_error_does_not_become_keep_delta(self):
        c = self.client()
        c.open_request = Mock(side_effect=self.http(403, "insufficient_user_quota"))
        for fn in (propose_semantic_delta, _propose_delta_safely):
            # Test both exception boundaries separately.
            LLMClient._disabled_endpoints.clear()
            arguments = ("demo", 0, BehaviorTarget("demo"), HostContext("demo"), None, c)
            with self.assertRaises(LLMUnavailableError):
                if fn is propose_semantic_delta:
                    fn(*arguments, output_dir=str(self.root))
                else:
                    fn("demo", 0, str(self.root), *arguments[2:])
        self.assertFalse((self.root / "delta_round_0.json").exists())

    def test_queued_instance_pauses_before_docker(self):
        event = threading.Event()
        event.set()
        args = SimpleNamespace(output_dir=str(self.root), _api_pause_event=event)
        with patch("brt6.pipeline.run.official_generation_runtime") as runtime:
            result = _run_one(args, "queued", {})
        self.assertEqual(result["status"], "PAUSED_API")
        runtime.assert_not_called()

    def test_ordered_requests_not_merged_and_changed_suffix_not_reused(self):
        c = self.client()
        c.open_request = Mock(side_effect=[self.response("first"), self.response("second"),
                                         self.response("changed"), self.response("new-tail")])
        for attempt in range(2):
            with StepJournal(self.root, {"experiment": 1}).activate():
                self.assertEqual(c.chat("s", "same"), "first")
                self.assertEqual(c.chat("s", "same"), "second")
        self.assertEqual(c.open_request.call_count, 2)
        with StepJournal(self.root, {"experiment": 1}).activate():
            self.assertEqual(c.chat("s", "changed"), "changed")
            self.assertEqual(c.chat("s", "same"), "new-tail")
        self.assertEqual(c.open_request.call_count, 4)

    def test_model_and_code_changes_invalidate_recovery(self):
        c = self.client()
        c.open_request = Mock(side_effect=[self.response("old"), self.response("new")])
        with StepJournal(self.root, {}).activate():
            self.assertEqual(c.chat("s", "u"), "old")
        c.temperature += 0.1
        with StepJournal(self.root, {}).activate():
            self.assertEqual(c.chat("s", "u"), "new")
        a = init_resume_run(str(self.root), False)
        self.assertEqual(init_resume_run(str(self.root), True), a)
        manifest = self.root / ".resume" / "implementation.json"
        saved = json.loads(manifest.read_text())
        saved["files"]["core/schema.py"] += "\n# altered\n"
        atomic_json(manifest, saved)
        self.assertNotEqual(init_resume_run(str(self.root), True), a)

    def cli_args(self):
        args = build_parser().parse_args([
            "--instances_path", "offline-issues", "--code_retrieval_path", "offline-code",
            "--test_retrieval_path", "offline-tests", "--repo_root_base", str(self.root),
            "--output_dir", str(self.root), "--api_key", "offline-key",
            "--base_url", "https://offline.invalid", "--model", "offline-model", "--resume",
        ])
        args._resume_revision = "offline-cli-revision"
        args._api_pause_event = threading.Event()
        args.behavior_target_source = {}
        args.behavior_target_source_signature = "offline-source"
        return args

    def test_cli_skips_only_completed_unchanged_instance(self):
        args = self.cli_args()
        context = InstanceContext("demo", "issue", buggy_repo_path=str(self.root))
        out = self.root / "demo"
        out.mkdir()
        # A historical accepted summary alone cannot certify three complete seeds.
        atomic_json(out / "summary.json", {"status": "ISSUE_ALIGNED_FAIL"})
        def pipeline(*a, **kw):
            (out / "final_test.py").write_text("def test_demo():\n    assert False\n")
            return FinalResult("demo", status="ISSUE_ALIGNED_FAIL", final_test_path=str(out / "final_test.py"))
        from contextlib import nullcontext
        with patch("brt6.pipeline.run.build_instance_context", return_value=context), \
             patch("brt6.pipeline.run.official_generation_runtime", side_effect=lambda **kw: nullcontext()) as docker, \
             patch("brt6.pipeline.run.run_instance_pipeline", side_effect=pipeline) as process:
            self.assertEqual(_run_one(args, "demo", {})["status"], "ISSUE_ALIGNED_FAIL")
            self.assertEqual(_run_one(args, "demo", {})["status"], "SKIP")
            self.assertEqual(docker.call_count, 1)
            context.issue_text = "different issue input"
            self.assertEqual(_run_one(args, "demo", {})["status"], "ISSUE_ALIGNED_FAIL")
            self.assertEqual(process.call_count, 2)
            (out / "final_test.py").write_text("changed artifact")
            self.assertEqual(_run_one(args, "demo", {})["status"], "ISSUE_ALIGNED_FAIL")
            self.assertEqual(process.call_count, 3)

    def test_c1_resume_reuses_first_response_when_json_retry_is_interrupted(self):
        args = self.cli_args()
        context = InstanceContext("demo", "issue")
        calls = []
        def make_client(**kwargs):
            c = self.client()
            def request(req, timeout):
                calls.append(json.loads(req.data))
                if len(calls) == 1:
                    return self.response('{"incomplete":')
                if len(calls) == 2:
                    raise TimeoutError("offline interrupted JSON retry")
                return self.response('{"expected_behavior":{"text":"expected"}}')
            c.open_request = request
            return c
        with patch("brt6.pipeline.run_issue_rewrite.build_instance_context", return_value=context), \
             patch("brt6.pipeline.run_issue_rewrite.LLMClient", side_effect=make_client):
            self.assertEqual(rewrite_one(args, "demo", {})["status"], "PAUSED_API")
            args._api_pause_event.clear()
            self.assertEqual(rewrite_one(args, "demo", {})["status"], "OK")
            self.assertEqual(len(calls), 3)
            self.assertEqual(rewrite_one(args, "demo", {})["status"], "SKIP")
            self.assertEqual(len(calls), 3)

    def test_explicit_target_input_is_not_shadowed_by_old_output_copy(self):
        source = self.root / "source"
        output = self.root / "output"
        BehaviorTarget("demo", issue_summary="old").save_json(str(output / "behavior_target.json"))
        BehaviorTarget("demo", issue_summary="updated").save_json(str(source / "demo" / "behavior_target.json"))
        with patch.dict(os.environ, {"BRT4_BEHAVIOR_CACHE_DIR": str(source)}):
            target = _load_cached_behavior(InstanceContext("demo", "issue"), str(output))
        self.assertEqual(target.issue_summary, "updated")

    def test_three_seed_interruption_reuses_successes_and_keeps_selection(self):
        iid = "offline__demo-1"
        repo = self.root / "repo"
        (repo / "tests").mkdir(parents=True)
        parent = "from math import sqrt\ndef test_seed():\n    assert sqrt(4) == 2\n"
        context = InstanceContext(iid, "sqrt(4) should be 3", repo="offline/demo", buggy_repo_path=str(repo),
            retrieved_tests=[RetrievedTest(iid, name=f"test_seed_{i}", file=f"tests/test_seed_{i}.py", code_content=parent) for i in range(3)])
        for seed in context.retrieved_tests:
            (repo / seed.file).write_text(parent)
        target = BehaviorTarget(iid, expected_behavior={"text": "sqrt(4) should be 3"})
        counters = Counter()

        def host(instance_id, seed, *args, **kw):
            counters["parent"] += 1
            return HostContext(iid, host_file=seed.file, seed_test_name=seed.name,
                seed_test_code=parent, seed_execution_status="PASS")

        def execute(*args, **kwargs):
            counters["execution"] += 1
            return ExecutionResult(iid, command=args[0], cwd=str(repo), returncode=1,
                stdout="AssertionError: 2.0 != 3", duration=0.5, status="ASSERTION_FAIL")

        def verify(issue, behavior, protocol, candidate, execution, source, client, *args, **kw):
            client.chat("offline-verifier", candidate.code + json.dumps(execution.to_dict()))
            return (VerifierDecision(iid, "accept", "accepted", [], "accept"),
                StrictVerifierResult(iid, decision="accept", failure_class="issue_aligned",
                    target_hit=True, oracle_grounded_in_issue=True, uses_public_behavior=True,
                    oracle_kind="assert", oracle_falsifiable=True, post_fix_failure_risk="low"))

        def run(folder, fail_call=None):
            folder.mkdir(exist_ok=True)
            target.save_json(str(folder / "behavior_target.json"))
            c = self.client()
            c.resume_revision = "frozen-offline-revision"
            requests = []
            def request(req, timeout):
                payload = json.loads(req.data)
                requests.append(payload)
                if len(requests) == fail_call:
                    raise TimeoutError("injected service interruption")
                system = payload["messages"][0]["content"]
                if system == SEED_MUTATION_PLAN_SYSTEM_PROMPT:
                    content = json.dumps({"schema_version": "semantic_delta.v2", "action": "MUTATE",
                        "dimension": "OBSERVATION", "seed_fact": "expected 2", "target_fact": "expected 3",
                        "change": "replace expected 2 with 3", "preserve": ["sqrt(4)"], "reason": "issue requirement"})
                elif system == "offline-verifier":
                    content = "accept"
                else:
                    content = "from math import sqrt\ndef test_generated():\n    assert sqrt(4) == 3\n"
                return self.response(content)
            c.open_request = request
            return c, requests, lambda: run_instance_pipeline(copy.deepcopy(context), c, str(folder), no_conda=True)

        with ExitStack() as stack:
            stack.enter_context(patch("brt6.execution.feedback.prepare_instance_worktree", return_value=(str(repo), {"status": "OK", "env_name": ""})))
            stack.enter_context(patch("brt6.execution.feedback.icore_test_command", side_effect=lambda repo, version, path, selector: f"python -m pytest {path}::{selector}"))
            stack.enter_context(patch("brt6.execution.feedback.rank_related_tests", side_effect=lambda tests, behavior: tests))
            stack.enter_context(patch("brt6.execution.feedback.build_host_context", side_effect=host))
            stack.enter_context(patch("brt6.execution.feedback.recover_test_protocol", side_effect=lambda *a: ProtocolRecovery(iid)))
            stack.enter_context(patch("brt6.execution.feedback.audit_recovered_protocol", side_effect=lambda p, *a: p))
            stack.enter_context(patch("brt6.execution.feedback.run_command_in_conda", side_effect=execute))
            stack.enter_context(patch("brt6.execution.feedback.verify_strict_semantics", side_effect=verify))
            _, baseline_calls, baseline_run = run(self.root / "baseline")
            baseline = baseline_run()
            self.assertEqual(baseline.status, "ISSUE_ALIGNED_FAIL", (self.root / "baseline" / "summary.json").read_text())
            self.assertEqual(len(baseline_calls), 9)
            counters.clear()
            interrupted_dir = self.root / "interrupted"
            _, first_calls, first_run = run(interrupted_dir, fail_call=6)
            with self.assertRaises(LLMUnavailableError):
                first_run()
            self.assertFalse((interrupted_dir / "direct_fallback").exists())
            self.assertEqual(json.loads((interrupted_dir / "summary.json").read_text())["status"], "PAUSED_API")
            _, resumed_calls, resumed_run = run(interrupted_dir)
            result = resumed_run()
            self.assertEqual(result.status, baseline.status)
            self.assertEqual(result.selected_seed_index, baseline.selected_seed_index)
            self.assertEqual(Path(result.final_test_path).read_bytes(), Path(baseline.final_test_path).read_bytes())
            self.assertEqual(len(first_calls), 6)  # five successes and one timeout
            self.assertEqual(len(resumed_calls), 4)  # missing verifier + third seed
            self.assertEqual(counters, {"parent": 3, "execution": 3})
            self.assertEqual(len(result.target_seed_attempts_summary), 3)
            for i in range(3):
                relative = f"seed_candidates/seed_{i}/candidate_ranking.json"
                actual = json.loads((interrupted_dir / relative).read_text())
                expected = json.loads((self.root / "baseline" / relative).read_text())
                self.assertEqual(actual["selected_attempt"], expected["selected_attempt"])
                self.assertEqual([x["rank_key"] for x in actual["checkpoints"]], [x["rank_key"] for x in expected["checkpoints"]])


if __name__ == "__main__":
    unittest.main()

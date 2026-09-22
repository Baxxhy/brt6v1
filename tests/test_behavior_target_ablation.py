from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from brt6.context.host_context import rank_related_tests
from brt6.core.ablation import AblationConfig
from brt6.core.behavior_evidence import render_evidence_prompt
from brt6.core.schema import (
    BehaviorTarget,
    HostContext,
    InstanceContext,
    RawIssueContext,
    RetrievedCode,
    RetrievedTest,
)
from brt6.execution.feedback import (
    _independent_adaptation_strategy,
    _load_behavior_evidence,
    _should_run_direct_fallback,
)
from brt6.generation.generator import generate_candidate
from brt6.mutation.seed_mutator import propose_semantic_delta
from brt6.pipeline.run import build_parser


class BehaviorTargetAblationTests(unittest.TestCase):
    def test_target_prompt_does_not_promote_issue_only_external_tool(self):
        from brt6.core.prompts import ISSUE_REWRITE_USER_PROMPT

        self.assertIn(
            "external tool used only to demonstrate it",
            ISSUE_REWRITE_USER_PROMPT,
        )
        self.assertIn(
            "not in the retrieved repository source or tests",
            ISSUE_REWRITE_USER_PROMPT,
        )

    def test_diversity_pilot_assigns_three_distinct_fixed_strategies(self) -> None:
        with patch.dict("os.environ", {"BRT_DIVERSE_ADAPTATION": "1"}):
            strategies = [_independent_adaptation_strategy(index) for index in range(3)]
        self.assertEqual(len(set(strategies)), 3)
        self.assertIn("PARENT-PRESERVING", strategies[0])
        self.assertIn("ISSUE-FIRST", strategies[1])
        self.assertIn("EVIDENCE-ALTERNATIVE", strategies[2])

    def test_diversity_pilot_is_off_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_independent_adaptation_strategy(0), "")

    def test_cli_defaults_to_current_full_method(self) -> None:
        parser = build_parser()
        required = [
            "--instances_path", "issues.json",
            "--code_retrieval_path", "code.json",
            "--test_retrieval_path", "tests.json",
            "--repo_root_base", "repos",
            "--output_dir", "out",
        ]
        self.assertTrue(parser.parse_args(required).enable_behavior_target)
        self.assertFalse(
            parser.parse_args(
                required + ["--enable_behavior_target", "false"]
            ).enable_behavior_target
        )

    def test_raw_context_has_no_structured_behavior_fields(self) -> None:
        payload = RawIssueContext("demo__repo-1", "raw issue text").to_dict()
        self.assertEqual(payload["issue_text"], "raw issue text")
        for forbidden in ("setup", "trigger", "oracle", "environment", "assertion"):
            self.assertNotIn(forbidden, payload)

    def test_disabled_branch_never_loads_behavior_cache(self) -> None:
        context = InstanceContext(
            "demo__repo-1",
            "raw issue text",
            retrieved_code=[
                RetrievedCode("demo__repo-1", path="src/demo.py", code_content="x = 1")
            ],
            retrieved_tests=[
                RetrievedTest("demo__repo-1", name="test_demo", file="tests/test_demo.py")
            ],
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "brt6.execution.feedback._load_cached_behavior",
            side_effect=AssertionError("BehaviorTarget cache must not be read"),
        ):
            evidence = _load_behavior_evidence(context, tmp, False)
            self.assertIsInstance(evidence, RawIssueContext)
            self.assertTrue((Path(tmp) / "raw_issue_context.json").is_file())
            self.assertTrue((Path(tmp) / "behavior_ablation_manifest.json").is_file())
            self.assertFalse((Path(tmp) / "behavior_target.json").exists())
            manifest = json.loads(
                (Path(tmp) / "behavior_ablation_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(manifest["issue_rewrite_invoked"])
            self.assertEqual(manifest["retrieved_code"][0]["path"], "src/demo.py")
            self.assertEqual(manifest["retrieved_tests"][0]["name"], "test_demo")

    def test_full_branch_still_uses_existing_cache_loader(self) -> None:
        context = InstanceContext("demo__repo-1", "raw issue text")
        expected = BehaviorTarget("demo__repo-1", issue_summary="summary")
        with tempfile.TemporaryDirectory() as tmp, patch(
            "brt6.execution.feedback._load_cached_behavior",
            return_value=expected,
        ) as loader:
            actual = _load_behavior_evidence(context, tmp, True)
        self.assertIs(actual, expected)
        loader.assert_called_once_with(context, tmp)

    def test_seed_order_is_identical_in_both_variants(self) -> None:
        tests = [
            RetrievedTest("x", name="first", file="a.py"),
            RetrievedTest("x", name="second", file="b.py"),
        ]
        full = rank_related_tests(tests, BehaviorTarget("x"))
        ablated = rank_related_tests(tests, RawIssueContext("x", "issue"))
        self.assertEqual([item.name for item in full], ["first", "second"])
        self.assertEqual([item.name for item in ablated], ["first", "second"])

    def test_prompt_text_changes_only_for_ablation(self) -> None:
        prompt = "BehaviorTarget: {}\nBehavior target: {}"
        self.assertEqual(render_evidence_prompt(prompt, BehaviorTarget("x")), prompt)
        rendered = render_evidence_prompt(prompt, RawIssueContext("x", "issue"))
        self.assertIn("without BehaviorTarget", rendered)
        self.assertIn("Raw issue (unstructured)", rendered)

    def test_full_generation_keeps_raw_issue_beside_structured_target(self) -> None:
        marker = "LOSSLESS_ISSUE_FACT_314159"
        behavior = BehaviorTarget("x", issue_summary="normalized summary")
        host = HostContext("x", seed_test_code="def test_seed():\n    assert True\n")
        seed = RetrievedTest(
            "x",
            name="test_seed",
            file="tests/test_seed.py",
            code_content=host.seed_test_code,
        )
        llm = Mock()
        llm.chat.return_value = "def test_generated():\n    assert False\n"
        with tempfile.TemporaryDirectory() as tmp:
            generate_candidate(
                "x",
                behavior,
                host,
                seed,
                [],
                llm,
                tmp,
                tmp,
                write_to_repo=False,
                issue_text=marker,
            )
            prompt = (Path(tmp) / "prompts" / "generation_round_0.txt").read_text(
                encoding="utf-8"
            )
        self.assertIn(marker, prompt)
        self.assertIn("normalized summary", prompt)
        self.assertIn("lossless source of facts", prompt)

    def test_raw_issue_is_not_duplicated_in_generation_prompt(self) -> None:
        marker = "RAW_ISSUE_SINGLE_COPY_271828"
        behavior = RawIssueContext("x", marker)
        host = HostContext("x", seed_test_code="def test_seed():\n    assert True\n")
        seed = RetrievedTest(
            "x",
            name="test_seed",
            file="tests/test_seed.py",
            code_content=host.seed_test_code,
        )
        llm = Mock()
        llm.chat.return_value = "def test_generated():\n    assert False\n"
        with tempfile.TemporaryDirectory() as tmp:
            generate_candidate(
                "x",
                behavior,
                host,
                seed,
                [],
                llm,
                tmp,
                tmp,
                write_to_repo=False,
                issue_text=marker,
            )
            prompt = (Path(tmp) / "prompts" / "generation_round_0.txt").read_text(
                encoding="utf-8"
            )
        self.assertEqual(prompt.count(marker), 1)
        self.assertIn('"structured_target": "unavailable"', prompt)

    def test_delta_planner_keeps_raw_issue_beside_structured_target(self) -> None:
        marker = "DELTA_ISSUE_FACT_161803"
        behavior = BehaviorTarget("x", issue_summary="normalized delta summary")
        host = HostContext("x", seed_test_code="def test_seed():\n    assert True\n")
        llm = Mock()
        llm.chat.return_value = json.dumps(
            {
                "schema_version": "semantic_delta.v2",
                "action": "KEEP",
                "dimension": "",
                "seed_fact": "seed",
                "target_fact": "target",
                "change": "",
                "preserve": [],
                "avoid": [],
                "reason": "insufficient evidence",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "prompts").mkdir()
            (Path(tmp) / "responses").mkdir()
            propose_semantic_delta(
                "x",
                0,
                behavior,
                host,
                None,
                llm,
                tmp,
                issue_text=marker,
            )
            prompt = (Path(tmp) / "prompts" / "delta_round_0.txt").read_text(
                encoding="utf-8"
            )
        self.assertIn(marker, prompt)
        self.assertIn("normalized delta summary", prompt)
        self.assertIn("INITIAL ADAPTATION", prompt)

    def test_feedback_delta_keeps_single_residual_scope(self) -> None:
        behavior = BehaviorTarget("x", issue_summary="target")
        host = HostContext("x", seed_test_code="def test_seed():\n    assert True\n")
        llm = Mock()
        llm.chat.return_value = json.dumps(
            {
                "schema_version": "semantic_delta.v2",
                "action": "KEEP",
                "dimension": "",
                "seed_fact": "seed",
                "target_fact": "target",
                "change": "",
                "preserve": [],
                "avoid": [],
                "reason": "insufficient evidence",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "prompts").mkdir()
            (Path(tmp) / "responses").mkdir()
            propose_semantic_delta("x", 1, behavior, host, None, llm, tmp)
            prompt = (Path(tmp) / "prompts" / "delta_round_1.txt").read_text(
                encoding="utf-8"
            )
        self.assertIn("FEEDBACK REPAIR", prompt)
        self.assertIn("single most blocking residual", prompt)

    def test_gpt_delta_planning_uses_low_reasoning(self) -> None:
        behavior = BehaviorTarget("x", issue_summary="target")
        host = HostContext("x", seed_test_code="def test_seed():\n    assert True\n")
        llm = Mock(provider="gpt")
        llm.chat.return_value = json.dumps(
            {
                "schema_version": "semantic_delta.v2",
                "action": "KEEP",
                "dimension": "",
                "seed_fact": "seed",
                "target_fact": "target",
                "change": "",
                "preserve": [],
                "avoid": [],
                "reason": "insufficient evidence",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "prompts").mkdir()
            (Path(tmp) / "responses").mkdir()
            propose_semantic_delta("x", 0, behavior, host, None, llm, tmp)
        self.assertEqual(llm.chat.call_args.kwargs["reasoning_effort"], "low")

    def test_gpt_generation_uses_low_reasoning_for_initial_and_repairs(self) -> None:
        behavior = BehaviorTarget("x", issue_summary="target")
        host = HostContext("x", seed_test_code="def test_seed():\n    assert True\n")
        seed = RetrievedTest(
            "x", name="test_seed", file="tests/test_seed.py",
            code_content=host.seed_test_code,
        )
        llm = Mock(provider="gpt", max_tokens=4096)
        llm.chat.return_value = "def test_generated():\n    assert False\n"
        with tempfile.TemporaryDirectory() as tmp:
            generate_candidate(
                "x", behavior, host, seed, [], llm, tmp, tmp,
                round_id=0, write_to_repo=False,
            )
            initial = llm.chat.call_args.kwargs
            generate_candidate(
                "x", behavior, host, seed, [], llm, tmp, tmp,
                round_id=1, write_to_repo=False,
            )
            repair = llm.chat.call_args.kwargs
        self.assertEqual(initial["reasoning_effort"], "low")
        self.assertEqual(initial["max_tokens"], 8192)
        self.assertEqual(repair["reasoning_effort"], "low")
        self.assertEqual(repair["max_tokens"], 8192)

    def test_direct_fallback_runs_only_after_full_target_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exported = Path(tmp) / "final_test.py"
            exported.write_text("def test_x():\n    assert False\n", encoding="utf-8")
            accepted = [
                {
                    "status": "ISSUE_ALIGNED_FAIL",
                    "final_test_path": str(exported),
                }
            ]
            unresolved = [
                {"status": "TRIGGER_UNRESOLVED", "final_test_path": ""}
            ]
            self.assertFalse(
                _should_run_direct_fallback(AblationConfig(), accepted)
            )
            self.assertTrue(
                _should_run_direct_fallback(
                    AblationConfig(),
                    [
                        {
                            "status": "ISSUE_ALIGNED_FAIL",
                            "final_test_path": str(Path(tmp) / "missing.py"),
                        }
                    ],
                )
            )
            self.assertTrue(
                _should_run_direct_fallback(AblationConfig(), unresolved)
            )
            self.assertFalse(
                _should_run_direct_fallback(
                    AblationConfig(behavior_target=False), unresolved
                )
            )

    def test_full_launcher_skips_rewrite_coverage_and_cache_when_disabled(self) -> None:
        launcher = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "run_p0_simple_llm_selector_full.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('--behavior-target {on|off}', launcher)
        self.assertIn('--behavior-target-cache PATH', launcher)
        self.assertIn(
            'issue_rewrite_skipped reason=explicit_behavior_target_cache',
            launcher,
        )
        self.assertIn(
            '--behavior-target-cache "$BEHAVIOR_TARGET_CACHE"', launcher
        )
        self.assertIn('issue_rewrite_skipped reason=w/o_behavior_target', launcher)
        self.assertIn('env -u BRT4_BEHAVIOR_CACHE_DIR', launcher)
        self.assertIn('--enable_behavior_target "$ENABLE_BEHAVIOR_TARGET"', launcher)
        self.assertIn(
            'COMPUTE_PATCH_COVERAGE=${COMPUTE_PATCH_COVERAGE:-true}', launcher
        )
        self.assertIn('COMPUTE_PATCH_COVERAGE=false', launcher)
        self.assertIn(
            '--compute-coverage "$COMPUTE_PATCH_COVERAGE"', launcher
        )
        self.assertIn('patch_coverage_enabled=$COMPUTE_PATCH_COVERAGE', launcher)


if __name__ == "__main__":
    unittest.main()

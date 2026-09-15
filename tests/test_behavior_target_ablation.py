from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brt6.context.host_context import rank_related_tests
from brt6.core.behavior_evidence import render_evidence_prompt
from brt6.core.schema import (
    BehaviorTarget,
    InstanceContext,
    RawIssueContext,
    RetrievedCode,
    RetrievedTest,
)
from brt6.execution.feedback import _load_behavior_evidence
from brt6.pipeline.run import build_parser


class BehaviorTargetAblationTests(unittest.TestCase):
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

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from brt6.context.host_context import rank_related_tests, select_related_test
from brt6.core.schema import (
    BehaviorTarget,
    CandidateTest,
    ExecutionResult,
    RetrievedTest,
    VerifierDecision,
)
from brt6.execution.executor import classify_execution, run_command_in_conda
from brt6.execution.feedback import _checkpoint_score, _save_checkpoint
from brt6.generation.generator import (
    _allows_target_reachability_oracle_pruning,
)
from brt6.issue.issue_rewriter import (
    apply_behavior_safety_constraints,
    behavior_from_dict,
)
from brt6.validation.strict_semantic_verifier import verify_strict_semantics
from brt6.validation.semantic_guard import audit_candidate, oracle_contract_summary


class _StaticLLM:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def chat(self, system: str, prompt: str) -> str:
        self.calls += 1
        self.system = system
        self.prompt = prompt
        return json.dumps(self.response, ensure_ascii=False)


class P0SimpleLLMSelectorTests(unittest.TestCase):
    def test_behavior_target_three_part_round_trip_is_lossless(self) -> None:
        flat = {
            "issue_summary": "summary",
            "trigger_condition": {
                "text": "call bad input",
                "evidence": ["issue line"],
                "confidence": "high",
            },
            "error_symptom": {
                "text": "wrong message",
                "evidence": ["trace"],
                "confidence": "high",
            },
            "expected_behavior": {
                "text": "correct message",
                "evidence": ["issue expectation"],
                "confidence": "high",
            },
            "target_apis": [{"name": "DurationField.clean", "source_path": "fields.py"}],
            "suspected_bug_locations": [],
            "related_test_seeds": [{"test_file": "tests/model.py", "test_name": "test_invalid"}],
            "mutation_hints": [{"slot": "input", "confidence": "medium"}],
            "observation_points": [{"kind": "exception"}],
            "assertion_hints": [{"preferred_assertion_style": "contains_fragment"}],
            "setup_hints": [{"hint": "SimpleTestCase", "confidence": "high"}],
            "uncertainties": ["exact stable fragment"],
        }
        first = behavior_from_dict("django__django-11049", flat)
        persisted = first.to_dict()
        self.assertEqual(
            set(persisted)
            - {"schema_version", "instance_id", "issue_summary", "uncertainties", "raw"},
            {"setup", "trigger", "oracle"},
        )
        self.assertEqual(
            persisted["trigger"]["trigger_condition"]["evidence"], ["issue line"]
        )
        self.assertEqual(persisted["raw"], flat)
        second = behavior_from_dict("django__django-11049", persisted)
        self.assertEqual(second.trigger_condition, first.trigger_condition)
        self.assertEqual(second.expected_behavior, first.expected_behavior)
        self.assertEqual(second.related_test_seeds, first.related_test_seeds)
        self.assertEqual(second.uncertainties, first.uncertainties)

    def test_behavior_recommendation_cannot_reorder_icore(self) -> None:
        model = RetrievedTest(
            "x", "test_invalid_string", "tests/model_fields/test_durationfield.py", "model"
        )
        form = RetrievedTest(
            "x", "test_overflow", "tests/forms_tests/test_durationfield.py", "form"
        )
        behavior = BehaviorTarget(
            "x",
            related_test_seeds=[
                {"test_file": form.file, "test_name": form.name, "confidence": "high"}
            ],
        )
        self.assertIs(select_related_test([model, form], behavior), model)
        self.assertEqual(rank_related_tests([model, form], behavior), [model, form])

    def test_behavior_audit_preserves_issue_valid_example(self) -> None:
        behavior = BehaviorTarget(
            "django__django-11049",
            trigger_condition={"text": "Use 14:00 to trigger validation"},
            expected_behavior={"text": "The validation error message has the corrected format."},
            mutation_hints=[{"target_pattern": "Treat 14:00 as invalid"}],
        )
        apply_behavior_safety_constraints(
            "The value '14:00' translates to fourteen minutes and is accepted.",
            behavior,
        )
        self.assertEqual(behavior.safety_constraints[0]["protected_inputs"], ["14:00"])

    def test_message_oracle_must_assert_required_fixed_side_token(self) -> None:
        behavior = BehaviorTarget(
            "pytest-dev__pytest-8906",
            expected_behavior={"text": "The error should provide actionable guidance."},
            assertion_hints=[
                {
                    "assertion_goal": (
                        "验证错误信息包含对 allow_module_level 的提示。"
                    )
                }
            ],
        )
        buggy_oracle = '''
def test_message(pytester):
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(["*Using pytest.skip outside of a test is not allowed*"])
'''
        fixed_oracle = '''
def test_message(pytester):
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(["*allow_module_level=True*"])
'''

        problem = audit_candidate(behavior, buggy_oracle)

        self.assertIn("allow_module_level", problem)
        self.assertEqual(audit_candidate(behavior, fixed_oracle), "")

    def test_oracle_contract_does_not_require_bare_assert(self) -> None:
        warning = '''
def test_warning():
    with pytest.warns(UserWarning):
        api()
'''
        logging = '''
def test_logging(self):
    with self.assertLogs("pkg", level="WARNING"):
        api()
'''
        no_exception = '''
def test_no_crash():
    api()
'''
        regular_behavior = BehaviorTarget("x", expected_behavior={"text": "emit a warning"})
        no_crash_behavior = BehaviorTarget(
            "x", expected_behavior={"text": "api should not crash and should work normally"}
        )
        self.assertTrue(oracle_contract_summary(regular_behavior, warning)["falsifiable"])
        self.assertTrue(oracle_contract_summary(regular_behavior, logging)["falsifiable"])
        summary = oracle_contract_summary(no_crash_behavior, no_exception)
        self.assertTrue(summary["falsifiable"])
        self.assertIn("NO_EXCEPTION", summary["kinds"])

    def test_pytester_nested_file_requires_unique_keyword_name(self) -> None:
        behavior = BehaviorTarget("pytest-dev__pytest-8906")
        positional = '''
def test_brt_case(pytester):
    pytester.makepyfile("def test_inner(): pass")
    pytester.runpytest()
'''
        named = '''
def test_brt_case(pytester):
    pytester.makepyfile(test_brt_inner_case="def test_inner(): pass")
    pytester.runpytest("test_brt_inner_case.py")
'''

        self.assertIn(
            "ImportPathMismatchError", audit_candidate(behavior, positional)
        )
        self.assertEqual(audit_candidate(behavior, named), "")

    def test_nested_pytest_collection_output_is_not_outer_collect_error(self) -> None:
        output = '''
collected 1 item
testing/test_brt_case.py::test_brt_case FAILED [100%]
E       Failed: nomatch: '*allow_module_level*'
Captured stdout call
collected 0 items / 1 error
ERROR collecting test_brt_inner_case.py
FAILED testing/test_brt_case.py::test_brt_case
'''
        behavior = BehaviorTarget(
            "pytest-dev__pytest-8906",
            error_symptom={"text": "pytest.skip module-level error"},
            target_apis=[{"name": "pytest.skip"}],
        )

        status = classify_execution(1, output, "", False, behavior)

        self.assertEqual(status, "ISSUE_ALIGNED_FAIL")

    def test_fixture_type_names_do_not_turn_issue_failures_into_setup_errors(self) -> None:
        issue_failures = [
            "AssertionError: LogCaptureFixture.clear() retained records",
            "assert b'expected' in fixture_bytes",
            "E assert '\\r' not in <CaptureFixture capfd>",
            "AssertionError: hidden unittest fixture was exposed",
            "@pytest.mark.usefixtures('rootdir')\nE assert href == '../index.svg'",
        ]
        for output in issue_failures:
            with self.subTest(output=output):
                self.assertEqual(
                    classify_execution(1, output, "", False),
                    "ASSERTION_FAIL",
                )

        missing_fixture = (
            "E fixture 'missing_service' not found\n"
            "> available fixtures: caplog, monkeypatch, tmp_path"
        )
        self.assertEqual(
            classify_execution(1, missing_fixture, "", False),
            "SETUP_ERROR",
        )

    def test_all_skipped_or_empty_run_is_not_pass(self) -> None:
        pytest_skip = "collected 1 item\n================ 1 skipped in 0.02s ================"
        sympy_skip = "tests[1] case s [OK]\n0 passed, 1 skipped, in 0.02 seconds"
        empty = "collected 0 items\nno tests ran in 0.01s"
        sympy_unmatched = (
            "================== tests finished: 0 passed, in 0.00 seconds "
            "==================="
        )
        django_unmatched = "Found 0 test(s).\nRan 0 tests in 0.000s\nOK"

        self.assertEqual(
            classify_execution(0, pytest_skip, "", False), "COLLECT_ERROR"
        )
        self.assertEqual(
            classify_execution(0, sympy_skip, "", False), "COLLECT_ERROR"
        )
        self.assertEqual(
            classify_execution(0, empty, "", False), "COLLECT_ERROR"
        )
        self.assertEqual(
            classify_execution(0, sympy_unmatched, "", False),
            "COLLECT_ERROR",
        )
        self.assertEqual(
            classify_execution(0, django_unmatched, "", False),
            "COLLECT_ERROR",
        )
        self.assertEqual(
            classify_execution(0, "1 passed in 0.02s", "", False), "PASS"
        )

    def test_generated_test_must_not_depend_on_skip_or_image_baseline(self) -> None:
        behavior = BehaviorTarget("demo__repo-1")
        skipped = '''
@pytest.mark.skipif(True, reason="optional")
def test_case():
    assert api()
'''
        image = '''
@image_comparison(["missing_baseline"])
def test_case():
    render()
'''
        import_or_skip = '''
def test_case():
    pytest.importorskip("optional")
    assert api()
'''

        self.assertIn("不得使用会跳过", audit_candidate(behavior, skipped))
        self.assertIn("baseline", audit_candidate(behavior, image))
        self.assertIn("importorskip", audit_candidate(behavior, import_or_skip))

    def test_generated_test_avoids_f_strings_for_python35_instances(self) -> None:
        behavior = BehaviorTarget("django__django-7530")
        candidate = '''
def test_case():
    value = 1
    assert api(), f"unexpected value: {value}"
'''

        self.assertIn("Python 3.5", audit_candidate(behavior, candidate))

    def test_generated_class_requires_recovered_module_setup(self) -> None:
        behavior = BehaviorTarget("django__django-14752")
        missing = '''
class ViewTests:
    as_view_args = {"admin_site": site}

    def test_case(self):
        assert api()
'''
        restored = '''
site = object()

class ViewTests:
    as_view_args = {"admin_site": site}

    def test_case(self):
        assert api()
'''

        self.assertIn("module_context", audit_candidate(behavior, missing))
        self.assertEqual(audit_candidate(behavior, restored), "")

    def test_issue_namespace_rejects_same_named_class_from_another_api(self) -> None:
        issue = """
from django.db import models
file = models.FilePathField(path=dynamic_path)
FilePathField.path should accept a callable.
"""
        behavior = BehaviorTarget(
            "django__django-10924",
            expected_behavior={"text": "FilePathField path accepts a callable"},
        )
        wrong = """
from django.forms import FilePathField
def test_case():
    field = FilePathField(path=lambda: '/tmp')
    assert callable(field.path)
"""
        correct = """
from django.db.models import FilePathField
def test_case():
    field = FilePathField(path=lambda: '/tmp')
    assert callable(field.path)
"""

        self.assertIn(
            "django.db.models.FilePathField",
            audit_candidate(behavior, wrong, issue_text=issue),
        )
        self.assertEqual(
            audit_candidate(behavior, correct, issue_text=issue),
            "",
        )

    def test_default_setting_test_must_not_override_the_setting(self) -> None:
        issue = (
            "Set default FILE_UPLOAD_PERMISSIONS to 0o644. In absence of "
            "explicitly configured FILE_UPLOAD_PERMISSIONS permissions differ."
        )
        behavior = BehaviorTarget(
            "django__django-10914",
            expected_behavior={"text": "The default file mode is 0o644."},
        )
        overridden = """
@override_settings(FILE_UPLOAD_PERMISSIONS=None)
def test_case():
    assert saved_mode() == 0o644
"""
        default = """
def test_case():
    assert saved_mode() == 0o644
"""

        self.assertIn(
            "不得显式覆盖 FILE_UPLOAD_PERMISSIONS",
            audit_candidate(behavior, overridden, issue_text=issue),
        )
        self.assertEqual(
            audit_candidate(behavior, default, issue_text=issue),
            "",
        )

    def test_unrelated_leading_type_precondition_cannot_mask_behavior(self) -> None:
        behavior = BehaviorTarget(
            "astropy__astropy-6938",
            expected_behavior={"text": "Exponent E is replaced by D."},
        )
        candidate = """
def test_case():
    value = public_api()
    assert isinstance(value, chararray.chararray)
    assert 'D' in value
"""

        self.assertIn("前置类型断言", audit_candidate(behavior, candidate))

    def test_rendered_output_literal_must_not_be_inferred_from_attribute_name(self) -> None:
        issue = """
tbl = QTable({'response': [0.7] * u.count})
tbl.write(sys.stdout, format='ascii.rst', header_rows=['name', 'unit'])
 response
       ct
"""
        behavior = BehaviorTarget(
            "astropy__astropy-14182",
            expected_behavior={"text": "RST output supports header rows."},
        )
        wrong = """
def test_case():
    output = render_table()
    assert 'count' in output
"""
        correct = """
def test_case():
    output = render_table()
    assert 'ct' in output
"""

        self.assertIn(
            "展示文本",
            audit_candidate(behavior, wrong, issue_text=issue),
        )
        self.assertEqual(audit_candidate(behavior, correct, issue_text=issue), "")

    def test_minimal_reproducer_rejects_unstated_schema_keywords(self) -> None:
        issue = """
The following qdp file should read into a Table rather than crashing:
read serr 1 2
1 0.5 1 0.5
Table.read('test.qdp', format='ascii.qdp')
"""
        behavior = BehaviorTarget(
            "astropy__astropy-14365",
            expected_behavior={"text": "The QDP input reads without crashing."},
        )
        wrong = """
def test_case():
    table = Table.read('test.qdp', format='ascii.qdp', names=['a', 'b', 'c', 'd'])
    assert table
"""
        correct = """
def test_case():
    table = Table.read('test.qdp', format='ascii.qdp')
    assert table
"""

        self.assertIn(
            "names",
            audit_candidate(behavior, wrong, issue_text=issue),
        )
        self.assertEqual(audit_candidate(behavior, correct, issue_text=issue), "")

    def test_strict_accept_is_overridden_by_deterministic_issue_guard(self) -> None:
        llm = _StaticLLM(
            {
                "decision": "accept",
                "failure_class": "issue_aligned",
                "target_hit": True,
                "oracle_grounded_in_issue": True,
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "reason": "looks aligned",
                "next_action": "accept",
            }
        )
        issue = "from django.db import models\nmodels.FilePathField path accepts callable"
        behavior = BehaviorTarget(
            "django__django-10924",
            expected_behavior={"text": "FilePathField path accepts callable"},
        )
        candidate = CandidateTest(
            "django__django-10924",
            code=(
                "from django.forms import FilePathField\n"
                "def test_case():\n"
                "    assert callable(FilePathField(path=lambda: '/tmp').path)\n"
            ),
        )
        execution = ExecutionResult(
            "django__django-10924",
            returncode=1,
            stdout="FAILED TypeError: expected str, got function",
            status="ASSERTION_FAIL",
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                issue,
                behavior,
                None,
                candidate,
                execution,
                "",
                llm,
                raw,
                0,
            )

        self.assertEqual(decision.decision, "repair_trigger")
        self.assertEqual(strict.failure_class, "target_not_hit")
        self.assertIn("django.db.models.FilePathField", decision.reason)

    def test_executor_returns_real_buggy_log_without_dynamic_tracing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = run_command_in_conda(
                "python -c \"raise ValueError('buggy symptom')\"",
                raw,
                no_conda=True,
                instance_id="execution",
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("buggy symptom", result.stderr)
        self.assertNotIn("runtime_target_hit", result.to_dict())
        self.assertNotIn("target_hit_evidence", result.to_dict())

    def test_llm_accepts_issue_aligned_buggy_failure_without_dynamic_evidence(self) -> None:
        llm = _StaticLLM(
            {
                "decision": "accept",
                "failure_class": "issue_aligned",
                "target_hit": True,
                "oracle_grounded_in_issue": True,
                "uses_public_behavior": True,
                "reason": "The assertion captures the expected fixed behavior.",
                "next_action": "accept",
            }
        )
        candidate = CandidateTest("x", code="def test_x():\n    assert api() == 2\n")
        execution = ExecutionResult(
            "x",
            command="pytest test_x.py",
            returncode=1,
            stdout="FAILED expected 2, got 1",
            status="ASSERTION_FAIL",
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "api() currently returns 1 but should return 2.",
                BehaviorTarget("x", expected_behavior={"text": "api returns 2"}),
                None,
                candidate,
                execution,
                "def api(): return 1",
                llm,
                raw,
                0,
            )
            prompt = Path(raw, "prompts", "strict_verifier_round_0.txt").read_text()
        self.assertEqual(decision.decision, "accept")
        self.assertTrue(strict.target_hit)
        self.assertIn("currently returns 1 but should return 2", prompt)
        self.assertIn("FAILED expected 2, got 1", prompt)
        self.assertNotIn("动态目标命中", prompt)

    def test_simple_semantic_ranking_prefers_llm_accept(self) -> None:
        execution = ExecutionResult(returncode=1, status="ASSERTION_FAIL")
        accepted = VerifierDecision("x", "accept")
        rejected = VerifierDecision("x", "repair_oracle")
        strict = SimpleNamespace(
            failure_class="issue_aligned",
            target_hit=True,
            oracle_grounded_in_issue=True,
            uses_public_behavior=True,
        )
        accepted_score, _ = _checkpoint_score(execution, accepted, strict)
        rejected_score, _ = _checkpoint_score(execution, rejected, strict)
        self.assertGreater(accepted_score, rejected_score)

        with tempfile.TemporaryDirectory() as raw:
            candidate = CandidateTest("x", code="def test_x():\n    assert False\n")
            accepted_checkpoint = _save_checkpoint(
                raw, 0, candidate, execution, accepted, None, strict_result=strict
            )
            rejected_checkpoint = _save_checkpoint(
                raw, 1, candidate, execution, rejected, None, strict_result=strict
            )
        self.assertGreater(
            tuple(accepted_checkpoint.rank_key), tuple(rejected_checkpoint.rank_key)
        )
        self.assertEqual(accepted_checkpoint.oracle_risk, {})
        self.assertEqual(accepted_checkpoint.surrogate, {})

    def test_confirmed_no_exception_oracle_is_hard_ranking_eligible(self) -> None:
        behavior = BehaviorTarget(
            "x",
            expected_behavior={"text": "jobs=0 falls back to at least one process"},
        )
        candidate = CandidateTest(
            "x",
            code="def test_x():\n    public_api(jobs=0)\n",
            mutation_adherence={"status": "FULL"},
        )
        execution = ExecutionResult(
            "x",
            returncode=1,
            stdout="ValueError: Number of processes must be at least 1",
            status="ISSUE_ALIGNED_FAIL",
        )
        strict = SimpleNamespace(
            failure_class="issue_aligned",
            target_hit=True,
            oracle_grounded_in_issue=True,
            uses_public_behavior=True,
            oracle_kind="NO_EXCEPTION",
            oracle_falsifiable=True,
        )
        with tempfile.TemporaryDirectory() as raw:
            checkpoint = _save_checkpoint(
                raw,
                0,
                candidate,
                execution,
                VerifierDecision("x", "accept"),
                None,
                behavior=behavior,
                issue_text="jobs=0 must not crash",
                strict_result=strict,
            )
        self.assertEqual(checkpoint.rank_key[0], 1)
        self.assertIn("NO_EXCEPTION", checkpoint.oracle_contract_kinds)

    def test_target_not_hit_can_prune_only_a_blocking_control_oracle(self) -> None:
        behavior = BehaviorTarget(
            "x", expected_behavior={"text": "modules=[] returns -3"}
        )
        before = """
def test_x():
    assert default_api() == -3
    assert target_api(modules=[]) == -3
"""
        after = """
def test_x():
    assert target_api(modules=[]) == -3
"""
        changed_target_oracle = """
def test_x():
    assert target_api(modules=[]) == 3
"""
        feedback = {"failure_class": "target_not_hit"}
        self.assertTrue(
            _allows_target_reachability_oracle_pruning(
                "trigger", feedback, before, after, behavior
            )
        )
        self.assertFalse(
            _allows_target_reachability_oracle_pruning(
                "trigger",
                feedback,
                before,
                changed_target_oracle,
                behavior,
            )
        )
        self.assertFalse(
            _allows_target_reachability_oracle_pruning(
                "setup", feedback, before, after, behavior
            )
        )

    def test_plan_or_oracle_contract_violation_is_selection_ineligible(self) -> None:
        behavior = BehaviorTarget(
            "x", expected_behavior={"text": "api should return the public result"}
        )
        execution = ExecutionResult(returncode=1, status="ASSERTION_FAIL")
        strict = SimpleNamespace(
            failure_class="issue_aligned",
            target_hit=True,
            oracle_grounded_in_issue=True,
            uses_public_behavior=True,
        )
        violated = CandidateTest(
            "x",
            code="def test_x():\n    assert api() == 2\n",
            mutation_adherence={"status": "VIOLATED"},
        )
        clean = CandidateTest(
            "x",
            code="def test_x():\n    assert api() == 2\n",
            mutation_adherence={"status": "FULL"},
        )
        with tempfile.TemporaryDirectory() as raw:
            bad = _save_checkpoint(
                raw,
                0,
                violated,
                execution,
                VerifierDecision("x", "accept"),
                None,
                behavior=behavior,
                issue_text="api should return 2",
                strict_result=strict,
            )
            good = _save_checkpoint(
                raw,
                1,
                clean,
                execution,
                VerifierDecision("x", "repair_oracle"),
                None,
                behavior=behavior,
                issue_text="api should return 2",
                strict_result=strict,
            )
        self.assertGreater(tuple(good.rank_key), tuple(bad.rank_key))

    def test_active_feedback_has_no_surrogate_call(self) -> None:
        import brt6.execution.feedback as feedback

        source = inspect.getsource(feedback)
        self.assertNotIn("run_surrogate_patch_loop(", source)
        self.assertNotIn("assess_surrogate_risk(", source)


if __name__ == "__main__":
    unittest.main()

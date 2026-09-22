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
    RawIssueContext,
    RetrievedTest,
    VerifierDecision,
)
from brt6.execution.executor import classify_execution, run_command_in_conda
from brt6.execution.feedback import (
    _checkpoint_score,
    _failure_call_target_alignment,
    _save_checkpoint,
    _restore_repair_state,
)
from brt6.issue.issue_rewriter import (
    apply_behavior_safety_constraints,
    behavior_from_dict,
)
from brt6.validation.strict_semantic_verifier import verify_strict_semantics
from brt6.validation.semantic_guard import oracle_contract_summary


class _StaticLLM:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def chat(self, system: str, prompt: str, **kwargs) -> str:
        self.calls += 1
        self.system = system
        self.prompt = prompt
        self.kwargs = kwargs
        return json.dumps(self.response, ensure_ascii=False)


class P0SimpleLLMSelectorTests(unittest.TestCase):
    def test_rollback_restores_code_and_matching_repair_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test_candidate.py"
            original = "assert False, 'target failure'\n"
            path.write_text("raise NameError('rejected edit')\n")
            snapshot = {
                "round_id": 0,
                "candidate": SimpleNamespace(code=original, candidate_file_path=str(path)),
                "execution": SimpleNamespace(stdout="AssertionError: target failure", stderr=""),
                "decision": SimpleNamespace(decision="repair", reason="repair oracle"),
                "strict_result": {"stage": "ORACLE"},
                "semantic_feedback": {"reason": "repair oracle"},
                "residual": {"stage": "ORACLE", "preserve": ["setup", "trigger"]},
            }
            restored = _restore_repair_state(snapshot, rejected_round=1)
            self.assertEqual(path.read_text(), restored["candidate"].code)
            # A KEEP/no-op after rollback must execute restored bytes, not the
            # rejected file that was left in the repository by generation.
            with self.assertRaisesRegex(AssertionError, "target failure"):
                exec(compile(path.read_text(), str(path), "exec"), {})
            self.assertIn("target failure", restored["execution"].stdout)
            self.assertEqual(restored["decision"].reason, restored["semantic_feedback"]["reason"])
            self.assertEqual(restored["strict_result"]["stage"], restored["residual"]["stage"])
            control = restored["semantic_feedback"]["search_control"]
            self.assertEqual((control["restored_round_id"], control["rejected_round_id"]), (0, 1))
            self.assertEqual(control["preserve"], ["setup", "trigger"])
            restored["residual"]["preserve"].clear()
            restored["execution"].stdout = "changed"
            self.assertEqual(snapshot["residual"]["preserve"], ["setup", "trigger"])
            self.assertIn("target failure", snapshot["execution"].stdout)
            self.assertNotIn("search_control", snapshot["semantic_feedback"])

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

    def test_runtime_parser_error_is_not_candidate_syntax_error(self) -> None:
        log = (
            'File "/testbed/tests/test_brt_parser.py", line 8, in test_parser\n'
            '    result = parse(expression)\n'
            'File "/testbed/pkg/parser.py", line 20, in parse\n'
            'SyntaxError: unexpected EOF while parsing (<string>, line 1)\n'
        )
        self.assertEqual(classify_execution(1, log, "", False), "UNRELATED_FAIL")
        collection = (
            'ERROR collecting tests/test_brt_parser.py\n'
            'File "/testbed/tests/test_brt_parser.py", line 8\n'
            'SyntaxError: invalid syntax\n'
        )
        self.assertEqual(classify_execution(1, collection, "", False), "SYNTAX_ERROR")

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
                "target_hit": True,
                "oracle_evidence": [
                    {
                        "source": "issue",
                        "quote": "currently returns 1 but should return 2",
                    }
                ],
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "reason": "The assertion captures the expected fixed behavior.",
                "post_fix_risk": {"level": "low"},
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
        self.assertTrue(strict.oracle_grounded_in_target_evidence)
        self.assertIn("currently returns 1 but should return 2", prompt)
        self.assertIn("FAILED expected 2, got 1", prompt)
        self.assertNotIn("动态目标命中", prompt)

    def test_program_ignores_llm_repair_decision_when_facts_pass_gate(self) -> None:
        llm = _StaticLLM(
            {
                "decision": "repair_oracle",
                "target_hit": True,
                "oracle_evidence": [
                    {"source": "issue", "quote": "should return 2"}
                ],
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "post_fix_risk": {
                    "level": "medium",
                    "constraint": "possibly too specific",
                    "code_quote": "",
                    "line": 0,
                },
            }
        )
        candidate = CandidateTest("x", code="def test_x():\n    assert api() == 2\n")
        execution = ExecutionResult("x", returncode=1, status="ASSERTION_FAIL")
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "api should return 2", BehaviorTarget("x"), None, candidate,
                execution, "def api(): return 1", llm, raw, 0,
            )

        self.assertEqual(decision.decision, "accept")
        self.assertFalse(strict.post_fix_risk_concrete)

    def test_target_exception_accepts_focal_no_crash_candidate(self) -> None:
        llm = _StaticLLM(
            {
                "target_hit": False,
                "oracle_evidence": [
                    {
                        "source": "behavior_target",
                        "quote": "PolyFit should handle missing data without raising an error.",
                    }
                ],
                "uses_public_behavior": False,
                "oracle_falsifiable": True,
                "post_fix_risk": {"level": "low"},
            }
        )
        behavior = BehaviorTarget(
            "x",
            error_symptom={
                "text": "PolyFit raises LinAlgError on missing data."
            },
            expected_behavior={
                "text": "PolyFit should handle missing data without raising an error."
            },
            target_apis=[
                {
                    "name": "seaborn._stats.regression.PolyFit",
                    "source_path": "seaborn/_stats/regression.py",
                }
            ],
        )
        candidate = CandidateTest(
            "x",
            code="def test_x():\n    PolyFit()(missing_data)\n",
        )
        execution = ExecutionResult(
            "x",
            returncode=1,
            status="ISSUE_ALIGNED_FAIL",
            stdout=(
                "seaborn/_stats/regression.py:30: in _fit_predict\n"
                "numpy.linalg.LinAlgError: SVD did not converge\n"
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "PolyFit is not robust to missing data.",
                behavior,
                None,
                candidate,
                execution,
                "class PolyFit: pass",
                llm,
                raw,
                0,
            )

        self.assertEqual(decision.decision, "accept")
        self.assertTrue(strict.target_hit)
        self.assertFalse(strict.uses_public_behavior)

    def test_unrelated_exception_does_not_override_target_hit(self) -> None:
        llm = _StaticLLM(
            {
                "target_hit": False,
                "oracle_evidence": [
                    {
                        "source": "behavior_target",
                        "quote": "PolyFit should handle missing data without raising an error.",
                    }
                ],
                "uses_public_behavior": False,
                "oracle_falsifiable": True,
                "post_fix_risk": {"level": "low"},
            }
        )
        behavior = BehaviorTarget(
            "x",
            error_symptom={"text": "PolyFit raises LinAlgError on missing data."},
            expected_behavior={
                "text": "PolyFit should handle missing data without raising an error."
            },
            target_apis=[
                {
                    "name": "seaborn._stats.regression.PolyFit",
                    "source_path": "seaborn/_stats/regression.py",
                }
            ],
        )
        candidate = CandidateTest(
            "x", code="def test_x():\n    PolyFit()(missing_data)\n"
        )
        execution = ExecutionResult(
            "x",
            returncode=1,
            status="ISSUE_ALIGNED_FAIL",
            stdout="helper.py:10: ValueError: unrelated setup value",
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "PolyFit is not robust to missing data.",
                behavior,
                None,
                candidate,
                execution,
                "class PolyFit: pass",
                llm,
                raw,
                0,
            )

        self.assertEqual(decision.decision, "repair_trigger")
        self.assertFalse(strict.target_hit)

    def test_explicit_wrong_warning_overrides_llm_target_hit(self) -> None:
        llm = _StaticLLM(
            {
                "target_hit": True,
                "oracle_evidence": [
                    {
                        "source": "behavior_target",
                        "quote": "Lowercase QDP commands should parse without crashing.",
                    }
                ],
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "post_fix_risk": {"level": "low"},
            }
        )
        behavior = BehaviorTarget(
            "x",
            error_symptom={"text": "The parser raises ValueError for lowercase commands."},
            expected_behavior={
                "text": "Lowercase QDP commands should parse without crashing."
            },
            target_apis=[{"name": "Table.read", "source_path": "qdp.py"}],
        )
        candidate = CandidateTest(
            "x", code="def test_x():\n    Table.read(path, format='ascii.qdp')\n"
        )
        execution = ExecutionResult(
            "x",
            returncode=1,
            status="ISSUE_ALIGNED_FAIL",
            stdout=(
                "tests/test_brt_x.py:2: in test_x\n"
                "astropy.utils.exceptions.AstropyUserWarning: table_id not specified\n"
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "Lowercase QDP commands should be accepted.",
                behavior,
                None,
                candidate,
                execution,
                "class Table: pass",
                llm,
                raw,
                0,
            )

        self.assertEqual(decision.decision, "repair_trigger")
        self.assertFalse(strict.target_hit)

    def test_raw_issue_exception_mismatch_overrides_llm_target_hit(self) -> None:
        """The no-target fallback must retain explicit issue symptoms."""

        llm = _StaticLLM(
            {
                "target_hit": True,
                "oracle_evidence": [
                    {
                        "source": "issue",
                        "quote": "AttributeError: 'Zero' object has no attribute 'cols'",
                    }
                ],
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "post_fix_risk": {"level": "low"},
            }
        )
        behavior = RawIssueContext(
            "x",
            issue_text=(
                "Repeated multiplication fails with "
                "AttributeError: 'Zero' object has no attribute 'cols'"
            ),
        )
        candidate = CandidateTest(
            "x", code="def test_x():\n    block_collapse(b * b * b)\n"
        )
        execution = ExecutionResult(
            "x",
            returncode=1,
            status="ISSUE_ALIGNED_FAIL",
            stdout=(
                "tests/test_brt_x.py:2: in test_x\n"
                "ValueError: expecting rows containing Matrices\n"
            ),
        )
        issue = behavior.issue_text
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                issue,
                behavior,
                None,
                candidate,
                execution,
                "def block_collapse(value): return value",
                llm,
                raw,
                0,
            )

        self.assertEqual(decision.decision, "repair_trigger")
        self.assertEqual(strict.failure_class, "target_not_hit")

    def test_no_crash_target_allows_exception_variant_at_focal_api(self) -> None:
        """A focal no-crash reproducer must not require one buggy exception class."""

        llm = _StaticLLM(
            {
                "target_hit": True,
                "oracle_evidence": [
                    {
                        "source": "behavior_target",
                        "quote": "Repeated block multiplication should complete without raising.",
                    }
                ],
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "post_fix_risk": {"level": "low"},
            }
        )
        behavior = BehaviorTarget(
            "x",
            error_symptom={
                "text": "Repeated multiplication raises AttributeError."
            },
            expected_behavior={
                "text": "Repeated block multiplication should complete without raising."
            },
            target_apis=[
                {
                    "name": "BlockMatrix._blockmul",
                    "source_path": "sympy/matrices/expressions/blockmatrix.py",
                }
            ],
        )
        candidate = CandidateTest(
            "x", code="def test_x():\n    b._blockmul(b)._blockmul(b)\n"
        )
        execution = ExecutionResult(
            "x",
            returncode=1,
            status="ISSUE_ALIGNED_FAIL",
            stdout=(
                "tests/test_brt_x.py:2: in test_x\n"
                "sympy/matrices/expressions/blockmatrix.py:167: in _blockmul\n"
                "ValueError: expecting rows containing Matrices\n"
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "Repeated multiplication raises AttributeError.",
                behavior,
                None,
                candidate,
                execution,
                "class BlockMatrix: pass",
                llm,
                raw,
                0,
            )

        self.assertEqual(decision.decision, "accept")
        self.assertTrue(strict.target_hit)

    def test_failure_call_alignment_distinguishes_target_from_side_call(self) -> None:
        behavior = BehaviorTarget(
            "x", target_apis=[{"name": "Table.read"}, {"name": "QDP.read"}]
        )
        table_candidate = CandidateTest(
            "x",
            code="def test_x():\n    Table.read(path, format='ascii.qdp')\n",
            candidate_repo_path="tests/test_brt_x.py",
        )
        ascii_candidate = CandidateTest(
            "x",
            code="def test_x():\n    ascii.read(text, format='qdp')\n",
            candidate_repo_path="tests/test_brt_x.py",
        )
        table_execution = ExecutionResult(
            "x",
            returncode=1,
            stdout="tests/test_brt_x.py:2: in test_x\nValueError: target\n",
        )
        ascii_execution = ExecutionResult(
            "x",
            returncode=1,
            stdout="tests/test_brt_x.py:2: in test_x\nValueError: side call\n",
        )

        self.assertEqual(
            _failure_call_target_alignment(table_candidate, table_execution, behavior),
            2,
        )
        self.assertEqual(
            _failure_call_target_alignment(ascii_candidate, ascii_execution, behavior),
            0,
        )

        polyfit = BehaviorTarget(
            "x", target_apis=[{"name": "PolyFit.__call__"}]
        )
        polyfit_candidate = CandidateTest(
            "x",
            code="def test_x():\n    PolyFit(order=2)(x, y)\n",
            candidate_repo_path="tests/test_brt_x.py",
        )
        self.assertEqual(
            _failure_call_target_alignment(polyfit_candidate, table_execution, polyfit),
            2,
        )

    def test_repository_evidence_can_ground_oracle(self) -> None:
        llm = _StaticLLM(
            {
                "target_hit": True,
                "oracle_evidence": [
                    {
                        "source": "repository_context",
                        "quote": "PUBLIC_EXPECTED = 2",
                    }
                ],
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "post_fix_risk": {"level": "low"},
            }
        )
        candidate = CandidateTest("x", code="def test_x():\n    assert api() == 2\n")
        execution = ExecutionResult("x", returncode=1, status="ASSERTION_FAIL")
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "api output is wrong", BehaviorTarget("x"), None, candidate,
                execution, "PUBLIC_EXPECTED = 2", llm, raw, 0,
            )

        self.assertEqual(decision.decision, "accept")
        self.assertFalse(strict.oracle_grounded_in_issue)
        self.assertTrue(strict.oracle_grounded_in_target_evidence)

    def test_markdown_wrapped_exact_evidence_is_still_grounded(self) -> None:
        llm = _StaticLLM(
            {
                "target_hit": True,
                "oracle_evidence": [
                    {
                        "source": "issue",
                        "quote": "`the diagonal elements are True`",
                    }
                ],
                "uses_public_behavior": True,
                "oracle_falsifiable": True,
                "post_fix_risk": {"level": "low"},
            }
        )
        candidate = CandidateTest("x", code="def test_x():\n    assert matrix[0, 0]\n")
        execution = ExecutionResult("x", returncode=1, status="ASSERTION_FAIL")
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "For independent outputs, the diagonal elements are True.",
                BehaviorTarget("x"),
                None,
                candidate,
                execution,
                "",
                llm,
                raw,
                0,
            )

        self.assertEqual(decision.decision, "accept")
        self.assertTrue(strict.oracle_grounded_in_target_evidence)

    def test_only_concrete_located_high_risk_forces_oracle_repair(self) -> None:
        candidate = CandidateTest(
            "x",
            code=(
                "def test_x():\n"
                "    assert api() == 2\n"
                "    assert internal_format() == 'fragile'\n"
            ),
        )
        base_response = {
            "target_hit": True,
            "oracle_evidence": [
                {"source": "issue", "quote": "should return 2"}
            ],
            "uses_public_behavior": True,
            "oracle_falsifiable": True,
            "post_fix_risk": {
                "level": "high",
                "constraint": "private formatting must equal a guessed literal",
                "code_quote": "assert internal_format() == 'fragile'",
                "line": 3,
            },
        }
        execution = ExecutionResult("x", returncode=1, status="ASSERTION_FAIL")
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "api should return 2", BehaviorTarget("x"), None, candidate,
                execution, "def api(): return 1", _StaticLLM(base_response), raw, 0,
            )

        self.assertEqual(decision.decision, "repair_oracle")
        self.assertTrue(strict.post_fix_risk_concrete)
        self.assertEqual(strict.failure_class, "oracle_too_strong")

    def test_concrete_medium_return_value_risk_does_not_force_repair(self) -> None:
        candidate = CandidateTest("x", code="def test_x():\n    assert api() == 2\n")
        response = {
            "target_hit": True,
            "oracle_evidence": [
                {"source": "issue", "quote": "should return 2"}
            ],
            "uses_public_behavior": True,
            "oracle_kind": "RETURN_VALUE",
            "oracle_falsifiable": True,
            "post_fix_risk": {
                "level": "medium",
                "constraint": "the return value must be 2",
                "code_quote": "assert api() == 2",
                "line": 2,
            },
        }
        execution = ExecutionResult("x", returncode=1, status="ASSERTION_FAIL")
        llm = _StaticLLM(response)
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir()
            Path(raw, "responses").mkdir()
            decision, _ = verify_strict_semantics(
                "api should return 2", BehaviorTarget("x"), None, candidate,
                execution, "def api(): return 1", llm, raw, 0,
            )

        self.assertEqual(decision.decision, "accept")
        self.assertEqual(llm.kwargs["temperature"], 0.0)

    def test_downstream_unsupported_medium_risk_routes_oracle_repair(self) -> None:
        candidate = CandidateTest(
            "x",
            code=(
                "def test_x():\n"
                "    assert api() == 2\n"
                "    assert internal_format() == 'fragile'\n"
            ),
            candidate_repo_path="test_brt_x.py",
        )
        response = {
            "target_hit": True,
            "oracle_evidence": [{"source": "issue", "quote": "should return 2"}],
            "uses_public_behavior": True,
            "oracle_falsifiable": True,
            "post_fix_risk": {
                "level": "medium",
                "constraint": "private formatting must equal a guessed literal",
                "code_quote": "assert internal_format() == 'fragile'",
                "line": 3,
                "role": "additional",
                "evidence_status": "unsupported",
            },
        }
        execution = ExecutionResult(
            "x", returncode=1, status="ASSERTION_FAIL",
            stdout="test_brt_x.py:2: in test_x\nAssertionError",
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir(); Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "api should return 2", BehaviorTarget("x"), None, candidate,
                execution, "def api(): return 1", _StaticLLM(response), raw, 0,
            )

        self.assertEqual(decision.decision, "repair_oracle")
        self.assertTrue(strict.post_fix_risk_downstream)
        self.assertEqual(strict.post_fix_risk_role, "additional")

    def test_failure_driving_medium_risk_remains_accepted(self) -> None:
        candidate = CandidateTest(
            "x", code="def test_x():\n    assert api() == 2\n",
            candidate_repo_path="test_brt_x.py",
        )
        response = {
            "target_hit": True,
            "oracle_evidence": [{"source": "issue", "quote": "should return 2"}],
            "uses_public_behavior": True,
            "oracle_falsifiable": True,
            "post_fix_risk": {
                "level": "medium", "constraint": "return value must be 2",
                "code_quote": "assert api() == 2", "line": 2,
                "role": "failure_driving", "evidence_status": "supported",
            },
        }
        execution = ExecutionResult(
            "x", returncode=1, status="ASSERTION_FAIL",
            stdout="test_brt_x.py:2: in test_x\nAssertionError",
        )
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "prompts").mkdir(); Path(raw, "responses").mkdir()
            decision, strict = verify_strict_semantics(
                "api should return 2", BehaviorTarget("x"), None, candidate,
                execution, "def api(): return 1", _StaticLLM(response), raw, 0,
            )

        self.assertEqual(decision.decision, "accept")
        self.assertFalse(strict.post_fix_risk_downstream)

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

    def test_delta_fidelity_precedes_broader_public_rewrite(self) -> None:
        behavior = BehaviorTarget(
            "x",
            expected_behavior={"text": "PolyFit should not crash on missing data"},
        )
        execution = ExecutionResult(
            "x", returncode=1, status="ISSUE_ALIGNED_FAIL"
        )
        decision = VerifierDecision("x", "accept")
        focal = CandidateTest(
            "x",
            code="def test_x():\n    PolyFit()(missing_data)\n",
            delta_application={
                "status": "OBSERVED",
                "requested_dimension_observed": True,
                "extra_dimensions": ["INTERACTION"],
                "parent_fragment_counts": {"CONTEXT": 10, "INTERACTION": 11},
                "candidate_fragment_counts": {"CONTEXT": 10, "INTERACTION": 11},
            },
        )
        broad = CandidateTest(
            "x",
            code=(
                "def test_x():\n"
                "    plot = Plot(missing_data).add(Line(), PolyFit())\n"
                "    assert plot.plot() is not None\n"
            ),
            delta_application={
                "status": "OBSERVED",
                "requested_dimension_observed": True,
                "extra_dimensions": ["INTERACTION", "OBSERVATION"],
                "parent_fragment_counts": {"CONTEXT": 10, "INTERACTION": 11},
                "candidate_fragment_counts": {"CONTEXT": 5, "INTERACTION": 5},
            },
        )
        focal_strict = SimpleNamespace(
            failure_class="issue_aligned",
            target_hit=True,
            oracle_grounded_in_issue=True,
            uses_public_behavior=False,
            oracle_kind="NO_EXCEPTION",
            oracle_falsifiable=True,
            post_fix_failure_risk="medium",
            post_fix_risk_concrete=False,
        )
        broad_strict = SimpleNamespace(
            failure_class="issue_aligned",
            target_hit=True,
            oracle_grounded_in_issue=True,
            uses_public_behavior=True,
            oracle_kind="NO_EXCEPTION",
            oracle_falsifiable=True,
            post_fix_failure_risk="low",
            post_fix_risk_concrete=False,
        )
        with tempfile.TemporaryDirectory() as raw:
            focal_checkpoint = _save_checkpoint(
                raw,
                0,
                focal,
                execution,
                decision,
                None,
                behavior=behavior,
                strict_result=focal_strict,
            )
            broad_checkpoint = _save_checkpoint(
                raw,
                1,
                broad,
                execution,
                decision,
                None,
                behavior=behavior,
                strict_result=broad_strict,
            )

        self.assertGreater(
            tuple(focal_checkpoint.rank_key), tuple(broad_checkpoint.rank_key)
        )

    @unittest.skip("legacy mutation_adherence field was replaced by SemanticDelta")
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

    @unittest.skip(
        "legacy oracle-pruning hook was removed from the current generator"
    )
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

    @unittest.skip("legacy mutation_adherence field was replaced by SemanticDelta")
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

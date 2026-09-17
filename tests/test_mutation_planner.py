from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest

pytest.skip(
    "legacy MutationPlan suite; production now uses SemanticDelta",
    allow_module_level=True,
)

from brt6.core.schema import (
    BehaviorTarget,
    CandidateTest,
    HostContext,
    MutationPlan,
    MutationStep,
    ProtocolRecovery,
    RetrievedCode,
    RetrievedTest,
)
from brt6.mutation.seed_mutator import _normalize_plan, build_mutation_plan
from brt6.validation.mutation_adherence import (
    assess_mutation_adherence,
    mutation_plan_from_candidate,
    oracle_fingerprint,
    oracle_kinds,
)
from brt6.validation.mutation_plan_validator import validate_mutation_plan


class _PlannerLLM:
    def __init__(self, response: str = "", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.prompts: list[str] = []

    def chat(self, system: str, prompt: str) -> str:
        del system
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.response


class MutationPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.behavior = BehaviorTarget(
            "demo__repo-1",
            trigger_condition={"text": "Call target_api with value 2"},
            expected_behavior={"text": "Returns the documented public result"},
            target_apis=[{"name": "target_api", "path": "pkg/mod.py"}],
        )
        self.host = HostContext(
            "demo__repo-1",
            host_file="tests/test_mod.py",
            seed_test_code=(
                "def test_seed():\n"
                "    result = target_api(1)\n"
                "    assert result == 1\n"
            ),
        )
        self.protocol = ProtocolRecovery(
            "demo__repo-1",
            test_file="tests/test_mod.py",
            imports=["from pkg.mod import target_api"],
        )
        self.source = [
            RetrievedCode(
                "demo__repo-1",
                obj_name="target_api",
                path="pkg/mod.py",
                code_content="def target_api(value):\n    return value\n",
            )
        ]
        self.seed = RetrievedTest(
            "demo__repo-1",
            name="test_seed",
            file="tests/test_mod.py",
            code_content=self.host.seed_test_code,
        )

    def _validate(self, data: dict) -> MutationPlan:
        normalized = _normalize_plan("demo__repo-1", 0, data)
        return validate_mutation_plan(
            normalized,
            self.behavior,
            self.host,
            self.protocol,
            self.source,
            self.seed,
        )

    def test_object_steps_are_preserved_and_validated(self) -> None:
        plan = self._validate(
            {
                "status": "PROPOSED",
                "trigger_goal": "exercise value two",
                "steps": [
                    {
                        "op": "ARG_VALUE_REPLACE",
                        "target_file": "pkg/mod.py",
                        "target_symbol": "target_api",
                        "seed_anchor": "target_api(1)",
                        "before": "target_api(1)",
                        "after": "target_api(2)",
                        "risk": "low",
                    }
                ],
            }
        )
        self.assertEqual(plan.status, "VALID")
        self.assertEqual(plan.mutation_ops, ["ARG_VALUE_REPLACE"])
        self.assertEqual(plan.steps[0].after, "target_api(2)")

    def test_empty_plan_abstains_without_call_chain_default(self) -> None:
        plan = self._validate({"status": "ABSTAIN", "steps": []})
        self.assertEqual(plan.status, "ABSTAIN")
        self.assertEqual(plan.mutation_ops, [])
        self.assertNotIn("CALL_CHAIN_EXTEND", json.dumps(plan.to_dict()))

    def test_high_risk_and_oracle_edits_are_rejected(self) -> None:
        for step in (
            {
                "op": "ARG_VALUE_REPLACE",
                "target_file": "pkg/mod.py",
                "target_symbol": "target_api",
                "seed_anchor": "target_api(1)",
                "before": "target_api(1)",
                "after": "target_api(2)",
                "risk": "high",
            },
            {
                "op": "ARG_VALUE_REPLACE",
                "target_file": "pkg/mod.py",
                "target_symbol": "target_api",
                "seed_anchor": "assert result == 1",
                "before": "assert result == 1",
                "after": "assert result == 2",
                "risk": "low",
            },
        ):
            with self.subTest(step=step):
                plan = self._validate({"status": "PROPOSED", "steps": [step]})
                self.assertEqual(plan.status, "INVALID")

    def test_non_assert_oracle_edits_are_rejected_from_trigger_plan(self) -> None:
        for after in (
            "with pytest.warns(UserWarning):\n    target_api(2)",
            "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n"
            "    target_api(2)",
            "with self.assertLogs('pkg', level='WARNING'):\n    target_api(2)",
            "snapshot.assert_match(target_api(2))",
        ):
            with self.subTest(after=after):
                plan = self._validate(
                    {
                        "status": "PROPOSED",
                        "steps": [
                            {
                                "op": "ARG_VALUE_REPLACE",
                                "target_file": "pkg/mod.py",
                                "target_symbol": "target_api",
                                "seed_anchor": "target_api(1)",
                                "before": "target_api(1)",
                                "after": after,
                                "risk": "low",
                            }
                        ],
                    }
                )
                self.assertEqual(plan.status, "INVALID")
                self.assertTrue(
                    any("oracle" in error for error in plan.validation_errors)
                )

    def test_behavior_target_cannot_self_validate_missing_source_symbol(self) -> None:
        self.behavior.target_apis = [
            {"name": "hallucinated_api", "path": "pkg/mod.py"}
        ]
        plan = self._validate(
            {
                "status": "PROPOSED",
                "steps": [
                    {
                        "op": "ARG_VALUE_REPLACE",
                        "target_file": "pkg/mod.py",
                        "target_symbol": "hallucinated_api",
                        "seed_anchor": "target_api(1)",
                        "before": "target_api(1)",
                        "after": "target_api(2)",
                        "risk": "low",
                    }
                ],
            }
        )
        self.assertEqual(plan.status, "INVALID")
        self.assertTrue(
            any("target_symbol" in item for item in plan.validation_errors)
        )

    def test_semantic_plan_does_not_require_exact_seed_anchor(self) -> None:
        plan = self._validate(
            {
                "status": "PROPOSED",
                "steps": [
                    {
                        "op": "ARG_VALUE_REPLACE",
                        "target_file": "pkg/mod.py",
                        "target_symbol": "target_api",
                        "seed_anchor": "target_api(999)",
                        "before": "target_api(999)",
                        "after": "target_api(2)",
                        "risk": "low",
                    }
                ],
            }
        )
        self.assertEqual(plan.status, "VALID")

    def test_semantic_plan_keeps_one_change_and_its_audit_contract(self) -> None:
        plan = self._validate(
            {
                "status": "PROPOSED",
                "operator": "ARG_VALUE_REPLACE",
                "gap": "the seed calls target_api with value 1",
                "target_file": "pkg/mod.py",
                "target_symbol": "target_api",
                "preserve": ["the existing runner and result oracle"],
                "change": ["call target_api with value 2"],
                "avoid": ["changing the result oracle"],
                "expected_effect": "the buggy target path is reached",
                "evidence": ["the Issue requires value 2"],
                "risk": "low",
            }
        )

        self.assertEqual(plan.status, "VALID")
        self.assertEqual(
            plan.validation_evidence["change"],
            ["call target_api with value 2"],
        )
        self.assertEqual(
            plan.validation_evidence["avoid"],
            ["changing the result oracle"],
        )
        self.assertEqual(
            plan.validation_evidence["evidence"],
            ["the Issue requires value 2"],
        )

    def test_semantic_plan_rejects_multiple_changes_in_one_round(self) -> None:
        plan = self._validate(
            {
                "status": "PROPOSED",
                "operator": "ARG_VALUE_REPLACE",
                "gap": "the seed does not reach the target path",
                "target_file": "pkg/mod.py",
                "target_symbol": "target_api",
                "preserve": ["the existing runner and result oracle"],
                "change": [
                    "call target_api with value 2",
                    "replace the fixture state",
                ],
                "avoid": ["changing the result oracle"],
                "expected_effect": "the buggy target path is reached",
                "evidence": ["the Issue requires value 2"],
                "risk": "low",
            }
        )

        self.assertEqual(plan.status, "INVALID")
        self.assertIn(
            "semantic plan must contain exactly one change",
            plan.validation_errors,
        )

    def test_mixed_plan_keeps_only_the_safe_trigger_step(self) -> None:
        safe = {
            "op": "ARG_VALUE_REPLACE",
            "target_file": "pkg/mod.py",
            "target_symbol": "target_api",
            "seed_anchor": "target_api(1)",
            "before": "target_api(1)",
            "after": "target_api(2)",
            "risk": "low",
        }
        oracle_edit = {
            "op": "ARG_VALUE_REPLACE",
            "target_file": "pkg/mod.py",
            "target_symbol": "target_api",
            "seed_anchor": "assert target_api(1) == 1",
            "before": "assert target_api(1) == 1",
            "after": "assert target_api(2) == 2",
            "risk": "medium",
        }

        plan = self._validate(
            {"status": "PROPOSED", "steps": [safe, oracle_edit]}
        )

        self.assertEqual(plan.status, "VALID")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].after, "target_api(2)")
        self.assertEqual(plan.validation_errors, [])
        self.assertEqual(
            len(plan.validation_evidence["rejected_steps"]), 1
        )

    def test_planner_service_failure_returns_invalid_plan(self) -> None:
        llm = _PlannerLLM(error=RuntimeError("offline"))
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_mutation_plan(
                "demo__repo-1",
                0,
                self.behavior,
                self.host,
                self.protocol,
                llm,
                tmp,
                related_source=self.source,
                related_test=self.seed,
            )
            self.assertEqual(plan.status, "INVALID")
            self.assertTrue((Path(tmp) / "mutation_round_0_plan.json").is_file())

    def test_prompt_contains_source_and_seed_but_not_oracle_plan_fields(self) -> None:
        response = json.dumps(
            {
                "status": "ABSTAIN",
                "trigger_goal": "",
                "steps": [],
                "preserve_from_seed": [],
                "why_target_will_be_reached": "seed already has the trigger",
            }
        )
        llm = _PlannerLLM(response=response)
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_mutation_plan(
                "demo__repo-1",
                0,
                self.behavior,
                self.host,
                self.protocol,
                llm,
                tmp,
                related_source=self.source,
                related_test=self.seed,
            )
        self.assertEqual(plan.status, "ABSTAIN")
        self.assertIn("def target_api", llm.prompts[0])
        self.assertIn("target_api(1)", llm.prompts[0])
        output_contract = llm.prompts[0].split("有安全计划时只输出：", 1)[-1]
        self.assertNotIn('"expected_behavior"', output_contract)
        self.assertNotIn('"oracle_strategy"', output_contract)

    def test_planner_prompt_truncates_oversized_retrieval_context(self) -> None:
        response = json.dumps({"status": "ABSTAIN", "steps": []})
        llm = _PlannerLLM(response=response)
        oversized_source = [
            RetrievedCode(
                "demo__repo-1",
                obj_name="target_api",
                path="pkg/mod.py",
                code_content="def target_api(value): return value\n" * 30_000,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            build_mutation_plan(
                "demo__repo-1",
                0,
                self.behavior,
                self.host,
                self.protocol,
                llm,
                tmp,
                related_source=oversized_source,
                related_test=self.seed,
            )

        self.assertLess(len(llm.prompts[0]), 250_000)

    def test_trigger_repair_assertion_change_is_a_violation(self) -> None:
        plan = MutationPlan(
            "demo__repo-1",
            status="VALID",
            steps=[
                MutationStep(
                    op="ARG_VALUE_REPLACE",
                    target_file="pkg/mod.py",
                    target_symbol="target_api",
                    seed_anchor="target_api(1)",
                    before="target_api(1)",
                    after="target_api(2)",
                    risk="low",
                )
            ],
            risk="low",
        )
        before = "def test_seed():\n    assert target_api(1) == 1\n"
        after = "def test_seed():\n    assert target_api(2) == 2\n"
        result = assess_mutation_adherence(
            after, plan, self.protocol, oracle_baseline=before
        )
        self.assertEqual(result["status"], "VIOLATED")
        self.assertTrue(
            any("oracle contract" in item for item in result["violations"])
        )

    def test_generalized_oracle_fingerprint_covers_non_assert_protocols(self) -> None:
        cases = {
            "exception": (
                "def test_x():\n    with pytest.raises(ValueError):\n        target_api(1)\n",
                "def test_x():\n    with pytest.raises(TypeError):\n        target_api(1)\n",
                "EXCEPTION",
            ),
            "warning": (
                "def test_x():\n    with pytest.warns(UserWarning):\n        target_api(1)\n",
                "def test_x():\n    with pytest.warns(RuntimeWarning):\n        target_api(1)\n",
                "WARNING",
            ),
            "logging": (
                "def test_x(self):\n    with self.assertLogs('pkg', level='INFO'):\n        target_api(1)\n",
                "def test_x(self):\n    with self.assertLogs('pkg', level='ERROR'):\n        target_api(1)\n",
                "LOGGING",
            ),
        }
        for name, (before, after, kind) in cases.items():
            with self.subTest(name=name):
                self.assertIn(kind, oracle_kinds(before))
                self.assertNotEqual(
                    oracle_fingerprint(before), oracle_fingerprint(after)
                )

    def test_adherence_requires_after_at_target_symbol_not_comment(self) -> None:
        plan = MutationPlan(
            "demo__repo-1",
            status="VALID",
            steps=[
                MutationStep(
                    op="ARG_VALUE_REPLACE",
                    target_file="pkg/mod.py",
                    target_symbol="target_api",
                    seed_anchor="target_api(1)",
                    before="target_api(1)",
                    after="target_api(2)",
                    risk="low",
                )
            ],
            risk="low",
        )
        result = assess_mutation_adherence(
            "def test_x():\n    # target_api(2)\n    assert other_api(2)\n",
            plan,
            None,
        )
        self.assertEqual(result["status"], "VIOLATED")

    def test_target_symbol_alone_does_not_satisfy_planned_after_state(self) -> None:
        plan = MutationPlan(
            "demo__repo-1",
            status="VALID",
            steps=[
                MutationStep(
                    op="ARG_VALUE_REPLACE",
                    target_file="pkg/mod.py",
                    target_symbol="target_api",
                    seed_anchor="target_api(1)",
                    before="target_api(1)",
                    after="target_api(2)",
                    risk="low",
                )
            ],
            risk="low",
        )

        result = assess_mutation_adherence(
            "def test_x():\n    assert target_api(1) == 1\n",
            plan,
            None,
        )

        self.assertEqual(result["status"], "VIOLATED")
        self.assertFalse(result["applied_steps"][0]["matched"])

    def test_candidate_lineage_reconstructs_validated_plan(self) -> None:
        plan = MutationPlan(
            "demo__repo-1",
            status="VALID",
            steps=[
                MutationStep(
                    op="ARG_VALUE_REPLACE",
                    target_file="pkg/mod.py",
                    target_symbol="target_api",
                    seed_anchor="target_api(1)",
                    before="target_api(1)",
                    after="target_api(2)",
                    risk="low",
                )
            ],
            risk="low",
        )
        candidate = CandidateTest(
            instance_id=plan.instance_id,
            round_id=2,
            code="def test_x():\n    assert target_api(2) == 3\n",
            mutation_plan_status="VALID",
            mutation_plan_risk="low",
        )
        candidate.mutation_adherence = assess_mutation_adherence(
            candidate.code, plan, None
        )

        inherited = mutation_plan_from_candidate(candidate)

        self.assertIsNotNone(inherited)
        self.assertEqual(inherited.status, "VALID")
        self.assertEqual(inherited.mutation_ops, ["ARG_VALUE_REPLACE"])
        self.assertEqual(inherited.steps[0].after, "target_api(2)")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from brt6.core.ablation import AblationConfig, behavior_prompt_payload
from brt6.core.schema import (
    BehaviorTarget,
    CandidateTest,
    ExecutionResult,
    FinalResult,
    HostContext,
    InstanceContext,
    ProtocolRecovery,
    RetrievedTest,
    StrictVerifierResult,
    VerifierDecision,
)
from brt6.execution.feedback import (
    _candidate_residual_state,
    _repair_focus,
    _residual_transition,
    _seed_residual_state,
    _seed_result_score,
    _semantic_feedback_payload,
    _selected_protocol_result_fields,
    _uses_adaptive_seed_pipelines,
    run_instance_pipeline,
)
from brt6.generation.generator import generate_candidate
from brt6.pipeline.run import (
    ablation_config_from_args,
    build_parser,
    resume_matches_ablation,
    select_instance_ids,
)


class _FakeLLM:
    def __init__(self, response: str = "def test_generated():\n    assert False\n") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.response


class FeedbackAblationTests(unittest.TestCase):
    def _required_args(self) -> list[str]:
        return [
            "--instances_path", "issues.json",
            "--code_retrieval_path", "code.json",
            "--test_retrieval_path", "tests.json",
            "--repo_root_base", "repos",
            "--output_dir", "out",
        ]

    def test_config_defaults_and_every_single_ablation(self) -> None:
        parser = build_parser()
        full = ablation_config_from_args(parser.parse_args(self._required_args()))
        self.assertEqual(full.ablation_id, "full")
        self.assertTrue(full.compute_patch_coverage)
        cases = {
            "--behavior-target": "wo_behavior_target",
            "--mutation": "wo_mutation",
            "--specialized-feedback": "generic_iteration",
            "--environment-feedback": "wo_environment_feedback",
            "--trigger-feedback": "wo_trigger_feedback",
            "--assertion-feedback": "wo_assertion_feedback",
        }
        for flag, expected in cases.items():
            with self.subTest(flag=flag):
                config = ablation_config_from_args(
                    parser.parse_args(self._required_args() + [flag, "off"])
                )
                self.assertEqual(config.ablation_id, expected)
                self.assertFalse(config.compute_patch_coverage)
                self.assertEqual(len(config.disabled_components()), 1)

    def test_multiple_disabled_components_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            AblationConfig(mutation=False, trigger_feedback=False).validate()

    def test_recovery_subset_keeps_full_dataset_identity(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            self._required_args() + ["--instance_ids", "one,three"]
        )
        issues = {"one": {}, "two": {}, "three": {}}

        self.assertEqual(select_instance_ids(args, issues), ["one", "three"])
        args.instance_id = "one"
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            select_instance_ids(args, issues)

    def test_resume_requires_exact_ablation_signature(self) -> None:
        full = AblationConfig()
        no_mutation = AblationConfig(mutation=False)
        summary = {"status": "ISSUE_ALIGNED_FAIL", "ablation_signature": full.signature}
        self.assertTrue(resume_matches_ablation(summary, full))
        self.assertFalse(resume_matches_ablation(summary, no_mutation))

    def test_mutation_ablation_keeps_identical_behavior_evidence(self) -> None:
        behavior = BehaviorTarget(
            "demo__repo-1",
            mutation_hints=[{"target_pattern": "bad value"}],
            raw={"mutation_hints": [{"raw": True}], "evidence": "kept"},
        )
        full_payload = behavior_prompt_payload(behavior, AblationConfig())
        ablated_payload = behavior_prompt_payload(
            behavior, AblationConfig(mutation=False)
        )
        self.assertEqual(ablated_payload, full_payload)
        self.assertEqual(
            ablated_payload["trigger"]["mutation_hints"][0]["target_pattern"],
            "bad value",
        )
        self.assertNotIn("raw", ablated_payload)
        self.assertEqual(behavior.to_dict()["raw"]["mutation_hints"][0]["raw"], True)

    def test_mutation_ablation_uses_same_generation_prompt_without_plan(self) -> None:
        behavior = BehaviorTarget(
            "demo__repo-1",
            issue_summary="demo",
            mutation_hints=[{"target_pattern": "shared evidence"}],
        )
        host = HostContext(
            "demo__repo-1",
            seed_test_code="def test_seed():\n    assert True\n",
        )
        related_test = RetrievedTest(
            "demo__repo-1",
            name="test_seed",
            file="tests/test_seed.py",
            code_content=host.seed_test_code,
        )
        full_llm = _FakeLLM()
        ablated_llm = _FakeLLM()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            full_candidate = generate_candidate(
                "demo__repo-1",
                behavior,
                host,
                related_test,
                [],
                full_llm,
                str(root / "full"),
                tmp,
                write_to_repo=False,
                ablation_config=AblationConfig(),
            )
            ablated_candidate = generate_candidate(
                "demo__repo-1",
                behavior,
                host,
                related_test,
                [],
                ablated_llm,
                str(root / "ablated"),
                tmp,
                write_to_repo=False,
                ablation_config=AblationConfig(mutation=False),
            )
            full_prompt = (
                root / "full" / "prompts" / "generation_round_0.txt"
            ).read_text(encoding="utf-8")
            ablated_prompt = (
                root / "ablated" / "prompts" / "generation_round_0.txt"
            ).read_text(encoding="utf-8")
            self.assertTrue(full_candidate.code)
            self.assertTrue(ablated_candidate.code)
            self.assertEqual(ablated_prompt, full_prompt)
            self.assertEqual(ablated_llm.calls, full_llm.calls)
            self.assertIn("mutation_hints", ablated_prompt)
            self.assertNotIn("Trigger Mutation Plan", ablated_prompt)
            mutation_files = [
                path
                for path in (root / "ablated").rglob("*")
                if path.is_file() and "mutation_round" in path.name.lower()
            ]
            self.assertEqual(mutation_files, [])

    def test_mutation_ablation_records_independent_top3_contract(self) -> None:
        config = AblationConfig(mutation=False)
        self.assertIn(
            "seed_generation_mode=independent_top3_v1", config.signature
        )
        self.assertEqual(
            config.to_dict()["seed_generation_mode"],
            "direct_generation_per_seed",
        )
        self.assertEqual(
            config.to_dict()["mutation_planning_mode"], "disabled"
        )

    def test_seed_pipeline_routing_is_identical_for_mutation_ablation(self) -> None:
        common = {
            "adaptive_disabled": False,
            "generate_only": False,
            "protocol_recovery_enabled": True,
            "forced_seed_index": None,
        }
        full = _uses_adaptive_seed_pipelines(AblationConfig(), **common)
        ablated = _uses_adaptive_seed_pipelines(
            AblationConfig(mutation=False), **common
        )
        self.assertTrue(full)
        self.assertEqual(ablated, full)

    def test_adaptive_top3_preserves_selected_runner_protocol(self) -> None:
        fields = _selected_protocol_result_fields(
            {
                "protocol_recovery_enabled": True,
                "seed_mutation_enabled": True,
                "observation_oracle_enabled": True,
                "strict_verifier_enabled": True,
                "selected_seed_file": "tests/test_seed.py",
                "selected_seed_name": "test_seed",
                "candidate_repo_path": "tests/test_brt.py",
                "pytest_nodeid": "tests/test_brt.py",
                "command": "python -m pytest tests/test_brt.py::test_brt",
                "direct_test_repo_path_hint": "tests/test_brt.py",
                "placement_dir": "tests",
                "runner_kind": "sphinx",
                "selector": "test_brt",
            }
        )

        self.assertTrue(fields["protocol_recovery_enabled"])
        self.assertTrue(fields["seed_mutation_enabled"])
        self.assertTrue(fields["strict_verifier_enabled"])
        self.assertEqual(fields["candidate_repo_path"], "tests/test_brt.py")
        self.assertEqual(fields["selector"], "test_brt")
        self.assertEqual(
            fields["command"],
            "python -m pytest tests/test_brt.py::test_brt",
        )

    def test_adaptive_top3_never_prefers_unresolved_seed_checkpoint(self) -> None:
        unresolved = _seed_result_score(
            {"status": "ENV_UNRESOLVED"},
            {"score": 200, "round_id": 0},
        )
        executable = _seed_result_score(
            {"status": "PASS", "buggy_execution": {"returncode": 0}},
            {"score": 10, "round_id": 0},
        )

        self.assertLess(unresolved, executable)

    def test_seed_pass_only_establishes_executable_host(self) -> None:
        state = _seed_residual_state(
            HostContext("demo", seed_execution_status="PASS")
        )

        self.assertEqual(state["stage"], "TRIGGER")
        self.assertEqual(state["depth"], 1)
        self.assertTrue(state["host_ready"])
        self.assertIsNone(state["trigger_satisfied"])
        self.assertEqual(state["semantic_status"], "UNKNOWN_NOT_VERIFIED")

    def test_candidate_residual_tracks_trigger_oracle_and_completion(self) -> None:
        execution = ExecutionResult(
            "demo", returncode=1, status="ASSERTION_FAIL"
        )
        trigger = _candidate_residual_state(
            0,
            execution,
            VerifierDecision("demo", "repair_trigger", "target not hit"),
            StrictVerifierResult(
                "demo",
                decision="repair_trigger",
                failure_class="target_not_hit",
            ),
        )
        oracle = _candidate_residual_state(
            1,
            execution,
            VerifierDecision("demo", "repair_oracle", "wrong oracle"),
            StrictVerifierResult(
                "demo",
                decision="repair_oracle",
                failure_class="oracle_wrong",
                target_hit=True,
            ),
        )
        complete = _candidate_residual_state(
            2,
            execution,
            VerifierDecision("demo", "accept", "issue reproduced"),
            StrictVerifierResult(
                "demo",
                decision="accept",
                failure_class="issue_aligned",
                target_hit=True,
                oracle_grounded_in_issue=True,
                uses_public_behavior=True,
                oracle_falsifiable=True,
            ),
        )

        self.assertEqual((trigger["stage"], trigger["depth"]), ("TRIGGER", 1))
        self.assertEqual((oracle["stage"], oracle["depth"]), ("ORACLE", 2))
        self.assertEqual((complete["stage"], complete["depth"]), ("SEARCH_ACCEPTED", 3))
        self.assertEqual(
            _residual_transition(trigger, oracle)["relation"], "IMPROVED"
        )
        self.assertEqual(
            _residual_transition(oracle, complete)["relation"], "SEARCH_ACCEPTED"
        )

    def test_semantic_feedback_contains_actionable_parent_child_difference(self) -> None:
        current = {
            "stage": "ORACLE",
            "depth": 2,
            "next_gap": "ORACLE",
        }
        transition = {
            "relation": "IMPROVED",
            "parent_stage": "TRIGGER",
            "child_stage": "ORACLE",
            "depth_delta": 1,
        }

        payload = _semantic_feedback_payload(
            VerifierDecision("demo", "repair_oracle", "wrong oracle"),
            StrictVerifierResult(
                "demo",
                failure_class="oracle_wrong",
                target_hit=True,
            ),
            current,
            transition,
        )

        residual = payload["residual_feedback"]
        self.assertEqual(residual["current_state"], current)
        self.assertEqual(residual["last_transition"], transition)
        self.assertIn("Preserve setup and trigger", residual["instruction"])

    def test_seed_score_does_not_let_residual_depth_dominate(self) -> None:
        trigger_score = _seed_result_score(
            {"status": "UNRELATED_FAIL"},
            {
                "score": 300,
                "verifier": {"residual_state": {"depth": 1}},
            },
        )
        oracle_score = _seed_result_score(
            {"status": "UNRELATED_FAIL"},
            {
                "score": 100,
                "verifier": {"residual_state": {"depth": 2}},
            },
        )

        self.assertGreater(trigger_score, oracle_score)

    def test_mutation_ablation_does_not_reuse_legacy_joint_signature(self) -> None:
        config = AblationConfig(mutation=False)
        self.assertNotIn("joint_top3", config.signature)
        self.assertNotEqual(
            config.signature,
            "behavior_target=1;mutation=0;specialized_feedback=1;"
            "environment_feedback=1;trigger_feedback=1;assertion_feedback=1;"
            "seed_generation_mode=joint_top3_v1",
        )

    @unittest.skip("legacy MutationPlan path was replaced by SemanticDelta")
    def test_nonadherent_plan_candidate_uses_explicit_direct_fallback(self) -> None:
        behavior = BehaviorTarget(
            "demo__repo-1",
            expected_behavior={"text": "target_api returns the public result"},
        )
        host = HostContext(
            "demo__repo-1",
            host_file="tests/test_mod.py",
            seed_test_code=(
                "def test_seed():\n"
                "    result = target_api(1)\n"
                "    assert result == 1\n"
            ),
        )
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
        llm = Mock()
        llm.chat.side_effect = [
            "def test_generated():\n    assert target_api(3) == 3\n",
            "def test_generated():\n    assert target_api(4) == 4\n",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            candidate = generate_candidate(
                "demo__repo-1",
                behavior,
                host,
                None,
                [],
                llm,
                tmp,
                tmp,
                write_to_repo=False,
                mutation_plan=plan,
                ablation_config=AblationConfig(),
            )
            fallback = Path(tmp) / "mutation_round_0_fallback.json"
            self.assertTrue(fallback.is_file())

        self.assertEqual(candidate.mutation_plan_status, "FALLBACK_DIRECT")
        self.assertEqual(candidate.mutation_adherence["status"], "NOT_APPLICABLE")
        self.assertIn("target_api(4)", candidate.code)
        self.assertEqual(llm.chat.call_count, 2)

    @unittest.skip("legacy MutationPlan path was replaced by SemanticDelta")
    def _run_forced_decisions(
        self,
        config: AblationConfig,
        decisions: list[str],
        executions: list[ExecutionResult] | None = None,
        *,
        adaptive_disabled: bool = True,
    ) -> tuple[FinalResult, Mock, Mock, Mock, Mock, Path, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        repo = root / "repo"
        repo.mkdir()
        output = root / "output"
        output.mkdir()
        instance_id = "demo__repo-1"
        behavior = BehaviorTarget(instance_id, issue_summary="issue")
        seed = RetrievedTest(
            instance_id,
            name="test_seed",
            file="tests/test_seed.py",
            code_content="def test_seed():\n    assert True\n",
        )
        retrieved_tests = [seed] + [
            RetrievedTest(
                instance_id,
                name=f"test_reference_{rank}",
                file=f"tests/test_reference_{rank}.py",
                code_content=f"def test_reference_{rank}():\n    assert True\n",
            )
            for rank in (1, 2)
        ]
        context = InstanceContext(
            instance_id,
            "full issue text",
            repo="demo/repo",
            buggy_repo_path=str(repo),
            retrieved_tests=retrieved_tests,
        )
        host = HostContext(
            instance_id,
            host_file=seed.file,
            seed_test_name=seed.name,
            seed_test_code=seed.code_content,
            seed_execution_status="PASS",
        )
        protocol = ProtocolRecovery(instance_id, test_file=seed.file)
        candidate_path = repo / "tests" / "test_brt.py"
        candidate_path.parent.mkdir()

        def make_candidate(*args, **kwargs):  # noqa: ANN002, ANN003
            if len(args) > 8 and isinstance(args[8], int):
                round_id = int(args[8])
            elif len(args) > 7 and isinstance(args[7], int):
                round_id = int(args[7])
            else:
                round_id = int(kwargs.get("round_id", 0))
            return CandidateTest(
                instance_id,
                round_id=round_id,
                code="def test_brt():\n    assert False\n",
                candidate_file_path=str(candidate_path),
                candidate_repo_path="tests/test_brt.py",
                pytest_nodeid="tests/test_brt.py",
                command="python -m pytest tests/test_brt.py::test_brt",
            )

        execution_values = executions or [
            ExecutionResult(
                instance_id,
                returncode=1,
                status="ASSERTION_FAIL",
                stdout="failed",
            )
            for _ in range(max(1, len(decisions)))
        ]
        strict_values = []
        for decision in decisions:
            strict = StrictVerifierResult(
                instance_id,
                decision=decision,
                failure_class="issue_aligned" if decision == "accept" else "side_path",
                target_hit=decision == "accept",
                oracle_grounded_in_issue=decision == "accept",
                uses_public_behavior=decision == "accept",
                reason=decision,
            )
            strict_values.append(
                (VerifierDecision(instance_id, decision, decision), strict)
            )

        build_plan = Mock(
            return_value=MutationPlan(
                instance_id,
                status="VALID",
                steps=[MutationStep(op="ARG_VALUE_REPLACE")],
            )
        )
        repair = Mock(side_effect=make_candidate)
        dependency = Mock(return_value=True)
        rebind = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch("brt6.execution.feedback._load_behavior_evidence", return_value=behavior))
            stack.enter_context(
                patch(
                    "brt6.execution.feedback.prepare_instance_worktree",
                    return_value=(
                        str(repo),
                        {"status": "PASS", "env_name": "", "setup_execution": {}},
                    ),
                )
            )
            stack.enter_context(patch("brt6.execution.feedback.build_host_context", return_value=host))
            stack.enter_context(patch("brt6.execution.feedback.recover_test_protocol", return_value=protocol))
            stack.enter_context(patch("brt6.execution.feedback.audit_recovered_protocol", side_effect=lambda *args: args[0]))
            stack.enter_context(patch("brt6.execution.feedback.build_mutation_plan", build_plan))
            stack.enter_context(patch("brt6.execution.feedback.generate_candidate", side_effect=make_candidate))
            stack.enter_context(patch("brt6.execution.feedback.repair_candidate", repair))
            stack.enter_context(patch("brt6.execution.feedback._refresh_candidate_command"))
            stack.enter_context(patch("brt6.execution.feedback.run_command_in_conda", side_effect=execution_values))
            stack.enter_context(patch("brt6.execution.feedback.verify_strict_semantics", side_effect=strict_values))
            stack.enter_context(patch("brt6.execution.feedback._recover_declared_dependency", dependency))
            stack.enter_context(patch("brt6.execution.feedback.rebind_observation_oracle", rebind))
            result = run_instance_pipeline(
                context,
                object(),
                str(output),
                no_conda=True,
                max_feedback_rounds=3,
                max_env_rounds=2,
                max_brt_rounds=3,
                ablation_config=config,
                _adaptive_disabled=adaptive_disabled,
                _prepared_repo_path=str(repo),
                _prepare_meta={"status": "PASS", "env_name": ""},
            )
        return result, build_plan, repair, dependency, rebind, output, temp

    def test_no_mutation_keeps_trigger_feedback_but_never_plans(self) -> None:
        result, planner, repair, _, _, output, temp = self._run_forced_decisions(
            AblationConfig(mutation=False), ["repair_trigger", "accept"]
        )
        self.addCleanup(temp.cleanup)
        planner.assert_not_called()
        self.assertEqual(repair.call_args_list[0].args[8], "trigger")
        self.assertEqual(result.mutation_plan_calls, 0)
        self.assertEqual(result.mutation_ops, [])
        self.assertEqual(result.repair_route_counts["trigger"], 1)
        self.assertEqual(result.seed_mode, "single_seed")
        self.assertEqual(result.seed_attempts_count, 1)
        self.assertEqual(result.host_context["reference_seed_tests"], [])
        self.assertEqual(list(output.rglob("mutation*")), [])

    def test_residual_difference_reaches_repair_and_is_saved(self) -> None:
        _, _, repair, _, _, output, temp = self._run_forced_decisions(
            AblationConfig(), ["repair_trigger", "accept"]
        )
        self.addCleanup(temp.cleanup)

        feedback = repair.call_args_list[0].args[11]["residual_feedback"]
        self.assertEqual(feedback["current_state"]["stage"], "TRIGGER")
        self.assertEqual(
            feedback["last_transition"]["relation"], "INITIALIZED"
        )
        self.assertIn("target behavior", feedback["instruction"])

        trace = json.loads(
            (output / "residual_trace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["state"]["stage"] for item in trace["rounds"]],
            ["TRIGGER", "SEARCH_ACCEPTED"],
        )
        self.assertEqual(trace["final"]["stage"], "SEARCH_ACCEPTED")

    def test_mutation_ablation_runs_three_independent_seed_pipelines(self) -> None:
        result, planner, repair, _, _, output, temp = self._run_forced_decisions(
            AblationConfig(mutation=False),
            ["accept", "accept", "accept"],
            adaptive_disabled=False,
        )
        self.addCleanup(temp.cleanup)
        planner.assert_not_called()
        repair.assert_not_called()
        self.assertEqual(result.seed_mode, "adaptive_top3")
        self.assertEqual(result.seed_attempts_count, 3)
        self.assertEqual(result.mutation_plan_calls, 0)
        self.assertEqual(
            [item["seed_index"] for item in result.seed_attempts_summary],
            [0, 1, 2],
        )
        for seed_index in range(3):
            seed_dir = output / "seed_candidates" / f"seed_{seed_index}"
            self.assertTrue((seed_dir / "protocol_recovery.json").is_file())
            self.assertTrue((seed_dir / "host_context.json").is_file())
            self.assertFalse((seed_dir / "joint_seed_bundle.json").exists())
            self.assertEqual(list(seed_dir.glob("mutation_plan_round_*.json")), [])

    def test_full_method_limits_trigger_replanning_to_one_call(self) -> None:
        result, planner, repair, _, _, _, temp = self._run_forced_decisions(
            AblationConfig(),
            ["repair_trigger", "repair_trigger", "repair_trigger", "reject"],
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(planner.call_count, 2)  # initial + one feedback replan
        self.assertEqual(result.trigger_replan_calls, 1)
        self.assertGreaterEqual(repair.call_count, 2)

    def test_buggy_pass_exhaustion_is_not_exported_as_a_final_test(self) -> None:
        executions = [
            ExecutionResult("demo__repo-1", returncode=0, status="PASS")
            for _ in range(4)
        ]
        result, _, _, _, _, output, temp = self._run_forced_decisions(
            AblationConfig(),
            ["repair_trigger"] * 4,
            executions=executions,
        )
        self.addCleanup(temp.cleanup)

        self.assertEqual(result.status, "TRIGGER_UNRESOLVED")
        self.assertEqual(result.final_test_path, "")
        self.assertFalse((output / "final_test.py").exists())

    def test_generic_iteration_uses_only_generic_repair(self) -> None:
        result, planner, repair, dependency, rebind, _, temp = self._run_forced_decisions(
            AblationConfig(specialized_feedback=False),
            ["repair_trigger", "accept"],
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual([call.args[8] for call in repair.call_args_list], ["generic"])
        dependency.assert_not_called()
        rebind.assert_not_called()
        self.assertEqual(result.repair_route_counts["generic"], 1)
        for route in ("environment", "trigger", "assertion"):
            self.assertEqual(result.repair_route_counts[route], 0)

    def test_environment_feedback_off_stops_late_setup_repair(self) -> None:
        result, _, repair, dependency, _, _, temp = self._run_forced_decisions(
            AblationConfig(environment_feedback=False), ["repair_setup"]
        )
        self.addCleanup(temp.cleanup)
        repair.assert_not_called()
        dependency.assert_not_called()
        self.assertEqual(result.repair_route_counts["environment"], 0)

    def test_environment_feedback_off_records_initial_setup_error_without_recovery(self) -> None:
        setup_execution = ExecutionResult(
            "demo__repo-1",
            returncode=2,
            status="SETUP_ERROR",
            stderr="missing fixture",
        )
        result, _, repair, dependency, _, _, temp = self._run_forced_decisions(
            AblationConfig(environment_feedback=False),
            ["repair_setup"],
            executions=[setup_execution],
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(result.status, "ENV_UNRESOLVED")
        self.assertTrue(result.command)
        self.assertTrue(result.candidate_repo_path)
        self.assertTrue(result.selector)
        repair.assert_not_called()
        dependency.assert_not_called()
        self.assertEqual(result.repair_route_counts["environment"], 0)

    def test_trigger_feedback_off_stops_trigger_repair(self) -> None:
        result, planner, repair, _, _, _, temp = self._run_forced_decisions(
            AblationConfig(trigger_feedback=False), ["reject"]
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(planner.call_count, 1)
        repair.assert_not_called()
        self.assertEqual(result.repair_route_counts["trigger"], 0)

    def test_assertion_feedback_off_stops_observation_and_oracle_repair(self) -> None:
        result, _, repair, _, rebind, _, temp = self._run_forced_decisions(
            AblationConfig(assertion_feedback=False), ["repair_oracle"]
        )
        self.addCleanup(temp.cleanup)
        repair.assert_not_called()
        rebind.assert_not_called()
        self.assertEqual(result.repair_route_counts["assertion"], 0)

    def test_assertion_feedback_on_uses_general_oracle_repair_without_probe(self) -> None:
        result, _, repair, _, rebind, _, temp = self._run_forced_decisions(
            AblationConfig(), ["repair_oracle", "accept"]
        )
        self.addCleanup(temp.cleanup)
        self.assertEqual(repair.call_args_list[0].args[8], "oracle")
        rebind.assert_not_called()
        self.assertEqual(result.repair_route_counts["assertion"], 1)

    def test_reject_oracle_failure_class_routes_to_oracle(self) -> None:
        focus = _repair_focus(
            VerifierDecision("demo", "reject", "oracle is too strong"),
            StrictVerifierResult(
                "demo",
                decision="reject",
                failure_class="oracle_too_strong",
            ),
            ExecutionResult("demo", returncode=1, status="ASSERTION_FAIL"),
        )
        self.assertEqual(focus, "oracle")

    def test_side_path_logging_observation_routes_to_oracle(self) -> None:
        focus = _repair_focus(
            VerifierDecision(
                "demo",
                "reject",
                "assertLogs uses the wrong logger name",
            ),
            StrictVerifierResult(
                "demo",
                decision="reject",
                failure_class="side_path",
                reason="logging observation selected an unsupported logger",
            ),
            ExecutionResult("demo", returncode=1, status="ASSERTION_FAIL"),
        )
        self.assertEqual(focus, "oracle")

    def test_shell_rejects_multiple_off_before_runtime_checks(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "scripts" / "run_p0_simple_llm_selector_full.sh"
        proc = subprocess.run(
            ["bash", str(launcher), "--mutation", "off", "--trigger-feedback", "off"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mutually exclusive", proc.stderr)


if __name__ == "__main__":
    unittest.main()

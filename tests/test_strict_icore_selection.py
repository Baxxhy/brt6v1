import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_generation_strict_icore", ROOT / "scripts" / "run_generation_strict_icore.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class StrictIcoreSelectionTests(unittest.TestCase):
    @staticmethod
    def _write_generation_seed(
        seed: Path,
        *,
        code: str,
        status: str = "ISSUE_ALIGNED_FAIL",
    ) -> None:
        seed.mkdir(parents=True)
        (seed / "final_test.py").write_text(code, encoding="utf-8")
        (seed / "summary.json").write_text(
            json.dumps({"status": status}), encoding="utf-8"
        )
        checkpoints = seed / "checkpoints"
        checkpoints.mkdir()
        (checkpoints / "candidate_attempt_0.py").write_text(
            code, encoding="utf-8"
        )
        (checkpoints / "candidate_attempt_0.json").write_text(
            json.dumps(
                {
                    "round_id": 0,
                    "execution": {
                        "returncode": 1,
                        "status": "ASSERTION_FAIL",
                        "stderr": "AssertionError",
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_freeze_follows_direct_fallback_route_selected_by_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            generation = root / "generation"
            output = root / "selection"
            instance_id = "project__repo-1"
            instance = generation / instance_id
            instance.mkdir(parents=True)
            (instance / "summary.json").write_text(
                json.dumps(
                    {
                        "selection_route": "direct_fallback_after_target_exhaustion",
                        "direct_fallback_used": True,
                    }
                ),
                encoding="utf-8",
            )
            # A target candidate exists but must not displace the generation
            # route that was frozen after target exhaustion.
            self._write_generation_seed(
                instance / "seed_candidates" / "seed_0",
                code="def test_target():\n    assert False\n",
            )
            fallback = instance / "direct_fallback"
            (fallback / "selected_seed_summary.json").parent.mkdir(
                parents=True, exist_ok=True
            )
            (fallback / "selected_seed_summary.json").write_text(
                json.dumps({"selected_seed_index": 1}), encoding="utf-8"
            )
            self._write_generation_seed(
                fallback / "seed_candidates" / "seed_1",
                code="def test_direct():\n    assert False\n",
            )

            manifest = MODULE.freeze_candidates(
                generation,
                output,
                {instance_id: {"problem_statement": "issue"}},
            )

            row = manifest["instances"][0]
            self.assertEqual(
                row["generation_route"],
                "direct_fallback_after_target_exhaustion",
            )
            self.assertEqual(
                row["component1_selected_candidate"], "direct_seed_1"
            )
            self.assertEqual(
                [candidate["candidate_id"] for candidate in row["candidates"]],
                ["direct_seed_1"],
            )
            frozen = output / "frozen" / instance_id / "direct_seed_1"
            self.assertIn(
                "test_direct", (frozen / "candidate.py").read_text(encoding="utf-8")
            )

    def test_only_frozen_strict_accepted_candidates_are_ranked(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            instance_id = "project__repo-1"
            for candidate_id, status in (
                ("seed_0", "UNRELATED_FAIL"),
                ("seed_1", "ISSUE_ALIGNED_FAIL"),
            ):
                frozen = output / "frozen" / instance_id / candidate_id
                frozen.mkdir(parents=True)
                (frozen / "candidate.py").write_text(
                    "def test_case():\n    assert False\n", encoding="utf-8"
                )
                (frozen / "buggy_execution.json").write_text(
                    json.dumps({"returncode": 1, "stderr": "AssertionError"}),
                    encoding="utf-8",
                )

            row = {
                "instance_id": instance_id,
                "component1_selected_candidate": "seed_0",
                "candidates": [
                    {"candidate_id": "seed_0", "component1_rank": 0, "strict_status": "UNRELATED_FAIL"},
                    {"candidate_id": "seed_1", "component1_rank": 1, "strict_status": "ISSUE_ALIGNED_FAIL"},
                ],
            }
            result = MODULE.process_instance(
                output,
                {instance_id: {"problem_statement": "The operation is incorrect."}},
                row,
            )

            self.assertEqual(result["selected"], "seed_1")
            self.assertEqual(result["accepted_candidates"], ["seed_1"])
            self.assertEqual(result["route"], "STRICT_ACCEPTED_THEN_ICORE_RANK")

    def test_existing_decision_does_not_override_current_candidates(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            instance_id = "project__repo-1"
            decision = output / "decisions" / instance_id / "decision.json"
            decision.parent.mkdir(parents=True)
            decision.write_text(json.dumps({"selected": "seed_0"}), encoding="utf-8")
            frozen = output / "frozen" / instance_id / "seed_1"
            frozen.mkdir(parents=True)
            (frozen / "candidate.py").write_text(
                "def test_case():\n    assert False\n", encoding="utf-8"
            )
            (frozen / "buggy_execution.json").write_text(
                json.dumps({"returncode": 1, "stderr": "AssertionError"}),
                encoding="utf-8",
            )

            result = MODULE.process_instance(
                output,
                {instance_id: {"problem_statement": "The operation is incorrect."}},
                {
                    "instance_id": instance_id,
                    "component1_selected_candidate": "seed_1",
                    "candidates": [
                        {
                            "candidate_id": "seed_1",
                            "component1_rank": 0,
                            "strict_status": "ISSUE_ALIGNED_FAIL",
                        }
                    ],
                },
            )

            self.assertEqual(result["selected"], "seed_1")

    def test_no_strict_accepted_candidate_means_no_submission(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            result = MODULE.process_instance(
                output,
                {"project__repo-1": {"problem_statement": "issue"}},
                {
                    "instance_id": "project__repo-1",
                    "component1_selected_candidate": None,
                    "candidates": [
                        {"candidate_id": "seed_0", "component1_rank": 0, "strict_status": "UNRELATED_FAIL"}
                    ],
                },
            )
            self.assertIsNone(result["selected"])
            self.assertEqual(result["route"], "NO_STRICT_ACCEPTED_CANDIDATE")


if __name__ == "__main__":
    unittest.main()

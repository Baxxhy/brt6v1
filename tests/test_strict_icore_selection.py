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

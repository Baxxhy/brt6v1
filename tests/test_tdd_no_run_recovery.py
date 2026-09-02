from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brt6.scripts.rerun_tdd_no_run_140 import (
    STATIC_CATEGORIES,
    audited_categories,
    generation_regeneration_ids,
    non_executable_generation_ids,
    prepare_resume_workers,
)


class TddNoRunRecoveryTests(unittest.TestCase):
    def test_audited_categories_are_disjoint_and_denominator_stable(self) -> None:
        results: dict[str, dict] = {}
        for values in STATIC_CATEGORIES.values():
            for instance_id in values:
                results[instance_id] = {"status": "FIXED_FAIL"}
        for index in range(83):
            results[f"django__django-dynamic-{index}"] = {
                "stderr": "ModuleNotFoundError: No module named 'test_sqlite'"
            }
        for index in range(35):
            results[f"sphinx-doc__sphinx-dynamic-{index}"] = {
                "stderr": "ModuleNotFoundError: No module named 'docutils'"
            }

        categories = audited_categories(results)

        self.assertEqual(sum(map(len, categories.values())), 140)
        self.assertEqual(len(categories["django_coverage_runner_path"]), 83)
        self.assertEqual(len(categories["sphinx_declared_dependencies"]), 35)

    def test_resume_workers_drop_only_recovery_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            for worker in range(2):
                path = source / f"worker_{worker}" / "results.json"
                path.parent.mkdir(parents=True)
                rows = {
                    f"keep-{worker}-{index}": {"status": "F2P_SUCCESS"}
                    for index in range(154 if worker == 0 else 155)
                }
                rows[f"rerun-{worker}"] = {"status": "FIXED_FAIL"}
                path.write_text(json.dumps(rows), encoding="utf-8")

            count = prepare_resume_workers(
                source, target, {"rerun-0", "rerun-1"}
            )

            self.assertEqual(count, 2)
            retained = {}
            for path in target.glob("worker_*/results.json"):
                retained.update(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(len(retained), 309)
            self.assertNotIn("rerun-0", retained)

    def test_non_executable_generation_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            generation = Path(raw)
            for instance_id, status, command in (
                ("valid", "ISSUE_ALIGNED_FAIL", "python -m pytest test.py"),
                ("env", "ENV_UNRESOLVED", ""),
                ("legacy", "PASS", ""),
            ):
                instance = generation / instance_id
                instance.mkdir()
                (instance / "final_test.py").write_text(
                    "def test_case(): assert True\n", encoding="utf-8"
                )
                (instance / "summary.json").write_text(
                    json.dumps(
                        {
                            "status": status,
                            "command": command,
                            "candidate_repo_path": "tests/test_case.py" if command else "",
                        }
                    ),
                    encoding="utf-8",
                )

            selected = generation_regeneration_ids(
                generation, {"valid", "env", "legacy", "missing"}, {"valid"}
            )

            self.assertEqual(selected, {"valid", "env", "legacy", "missing"})

    def test_post_generation_gate_requires_complete_runner_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            generation = Path(raw)
            for instance_id, status, command in (
                ("valid", "ISSUE_ALIGNED_FAIL", "python -m pytest test.py"),
                ("replayable_setup_failure", "ENV_UNRESOLVED", "python -m pytest test.py"),
                ("bad_status", "ENV_UNRESOLVED", ""),
                ("missing_protocol", "PASS", ""),
            ):
                instance = generation / instance_id
                instance.mkdir()
                (instance / "final_test.py").write_text(
                    "def test_case(): assert api()\n", encoding="utf-8"
                )
                (instance / "summary.json").write_text(
                    json.dumps(
                        {
                            "status": status,
                            "command": command,
                            "candidate_repo_path": "tests/test_case.py" if command else "",
                        }
                    ),
                    encoding="utf-8",
                )

            bad = non_executable_generation_ids(
                generation,
                {
                    "valid",
                    "replayable_setup_failure",
                    "bad_status",
                    "missing_protocol",
                    "missing",
                },
            )

            self.assertEqual(
                bad, {"bad_status", "missing_protocol", "missing"}
            )


if __name__ == "__main__":
    unittest.main()

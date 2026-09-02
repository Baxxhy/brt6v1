from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from brt6.evaluation.official_benchmarks import (
    export_official_predictions,
    generation_completeness,
    new_file_patch,
    resolve_generated_test_path,
)


class OfficialBenchmarkExportTests(unittest.TestCase):
    def test_generation_completeness_requires_every_nonempty_export(self) -> None:
        self.assertTrue(
            generation_completeness(
                {
                    "total_instances": 2,
                    "generated_instances": 2,
                    "missing_instances": 0,
                    "missing_ids": [],
                }
            )["complete"]
        )
        refused = generation_completeness(
            {
                "total_instances": 2,
                "generated_instances": 1,
                "missing_instances": 1,
                "missing_ids": ["repo__name-2"],
            }
        )
        self.assertFalse(refused["complete"])
        self.assertIn("generated=1/2", refused["reason"])

    def test_new_file_patch_is_official_model_patch_shape(self) -> None:
        patch = new_file_patch("testing/test_brt_demo.py", "def test_demo():\n    assert 1\n")
        self.assertIn("diff --git a/testing/test_brt_demo.py b/testing/test_brt_demo.py", patch)
        self.assertIn("--- /dev/null", patch)
        self.assertIn("+++ b/testing/test_brt_demo.py", patch)
        self.assertIn("+def test_demo():", patch)

    def test_selected_adaptive_seed_controls_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "seed_candidates" / "seed_2").mkdir(parents=True)
            (root / "summary.json").write_text(
                json.dumps({"selected_seed_index": 2}), encoding="utf-8"
            )
            (root / "seed_candidates" / "seed_2" / "summary.json").write_text(
                json.dumps({"candidate_repo_path": "tests/unit/test_brt_demo.py"}),
                encoding="utf-8",
            )
            path, _ = resolve_generated_test_path("demo__repo-1", root)
            self.assertEqual(path, "tests/unit/test_brt_demo.py")

    def test_missing_generation_stays_in_official_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outputs = root / "generation"
            present = outputs / "repo__name-1"
            present.mkdir(parents=True)
            (present / "final_test.py").write_text(
                "def test_issue():\n    assert False\n", encoding="utf-8"
            )
            (present / "summary.json").write_text(
                json.dumps({"candidate_repo_path": "tests/test_brt_repo__name_1.py"}),
                encoding="utf-8",
            )
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    [
                        {"instance_id": "repo__name-1"},
                        {"instance_id": "repo__name-2"},
                    ]
                ),
                encoding="utf-8",
            )
            destination = root / "predictions.json"
            manifest = export_official_predictions(outputs, dataset, destination)
            predictions = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(manifest["total_instances"], 2)
            self.assertEqual(manifest["generated_instances"], 1)
            self.assertEqual(len(predictions), 2)
            self.assertEqual(predictions[1]["model_patch"], "")

    def test_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            new_file_patch("../outside.py", "pass\n")

    def test_official_runner_refuses_incomplete_generation_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outputs = root / "generation"
            outputs.mkdir()
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps([{"instance_id": "repo__name-1"}]), encoding="utf-8"
            )
            evaluation = root / "evaluation"
            script = Path(__file__).resolve().parents[1] / "scripts" / "run_official_eval_after_generation.py"
            process = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--dataset",
                    "swt",
                    "--outputs-dir",
                    str(outputs),
                    "--dataset-file",
                    str(dataset),
                    "--evaluation-dir",
                    str(evaluation),
                    "--run-id",
                    "incomplete-test",
                    "--official-python",
                    "/does/not/exist",
                    "--swtbench-root",
                    "/does/not/exist",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 3, process.stderr)
            manifest = json.loads(
                (evaluation / "official_run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "refused_incomplete_generation")
            self.assertFalse(manifest["official_harness_invoked"])


if __name__ == "__main__":
    unittest.main()

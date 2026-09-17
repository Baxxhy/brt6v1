from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt6.evaluation.official_benchmarks import (
    export_official_predictions,
    generation_completeness,
    new_file_patch,
    resolve_generated_test_path,
)
from brt6.scripts.run_official_eval_after_generation import (
    _f2p_only_marker,
    _stop_running_official_containers,
)


class OfficialBenchmarkExportTests(unittest.TestCase):
    def test_interrupt_cleanup_preserves_preexisting_eval_containers(self) -> None:
        with mock.patch(
            "brt6.scripts.run_official_eval_after_generation._running_official_container_ids",
            return_value={"existing", "created"},
        ), mock.patch(
            "brt6.scripts.run_official_eval_after_generation.run_subprocess_tree"
        ) as run:
            run.return_value = subprocess.CompletedProcess([], 0, "created\n", "")
            _stop_running_official_containers(Path("/tmp"), {"existing"})

        run.assert_called_once_with(
            ["docker", "kill", "created"],
            "/tmp",
            600,
        )

    def test_run_scoped_f2p_marker_disables_only_its_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            evaluation = run / "evaluation" / "formal_f2p"
            evaluation.mkdir(parents=True)
            marker = run / "F2P_ONLY"
            marker.write_text("f2p only\n", encoding="utf-8")

            self.assertEqual(_f2p_only_marker(evaluation), marker)

    def test_swt_harness_defaults_to_the_vendored_project_copy(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        launcher = (project_root / "pipeline" / "run.py").read_text(
            encoding="utf-8"
        )
        evaluator = (
            project_root / "scripts" / "run_official_eval_after_generation.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("/root/Baxxhy/BugReproduce/swt-bench", launcher)
        self.assertIn("evaluation/vendor/swtbench", launcher)
        self.assertIn("evaluation/vendor/swtbench", evaluator)
        self.assertTrue(
            (project_root / "evaluation" / "vendor" / "swtbench" / "src" / "main.py").is_file()
        )

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

    def test_official_runner_allows_incomplete_generation_before_harness(self) -> None:
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
            self.assertEqual(process.returncode, 1, process.stderr)
            self.assertIn(
                "continuing with empty patches counted as F2P failures",
                process.stderr,
            )
            predictions_manifest = json.loads(
                (evaluation / "official_predictions.manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(predictions_manifest["total_instances"], 1)
            self.assertEqual(predictions_manifest["generated_instances"], 0)
            self.assertEqual(predictions_manifest["missing_instances"], 1)


if __name__ == "__main__":
    unittest.main()

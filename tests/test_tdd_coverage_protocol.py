from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt6.evaluation.direct_eval import (
    aggregate_tdd_bench_score,
    combine_tdd_coverage,
    ensure_tdd_optional_runtime_tools,
    tdd_changed_lines,
    tdd_test_command,
    parse_tdd_coverage_json,
)


class TddCoverageProtocolTests(unittest.TestCase):
    def test_changed_line_denominator_matches_tdd_text_policy(self) -> None:
        patch = (
            "diff --git a/pkg/module.py b/pkg/module.py\n"
            "--- a/pkg/module.py\n"
            "+++ b/pkg/module.py\n"
            "@@ -10,4 +10,4 @@\n"
            " context\n"
            "-old_value = 1\n"
            "-# removed comment\n"
            "+new_value = 2\n"
            "+   \n"
            " context\n"
            "diff --git a/docs/note.txt b/docs/note.txt\n"
            "--- a/docs/note.txt\n"
            "+++ b/docs/note.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        targets = tdd_changed_lines(patch)

        self.assertEqual(
            targets["before"],
            {"pkg/module.py": [11], "docs/note.txt": [1]},
        )
        self.assertEqual(
            targets["after"],
            {"pkg/module.py": [11], "docs/note.txt": [1]},
        )

    def test_coverage_json_includes_missing_branch_endpoints(self) -> None:
        payload = {
            "files": {
                "pkg/module.py": {
                    "missing_lines": [11],
                    "missing_branches": [[12, 13], [14, -1]],
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "coverage.json"
            output.write_text(json.dumps(payload), encoding="utf-8")
            result = parse_tdd_coverage_json(
                output,
                {
                    "pkg/module.py": [10, 11, 12, 13, 14],
                    "docs/note.txt": [1],
                },
                temporary_dir,
            )

        self.assertEqual(result["total_changed"], 6)
        self.assertEqual(result["total_missed"], 4)
        self.assertEqual(
            result["missed_changed_lines_by_file"]["pkg/module.py"],
            [11, 12, 13, 14],
        )
        # This preserves upstream TDD-Bench's text-report behavior: a file not
        # listed by coverage.py has no listed missing changed lines.
        self.assertEqual(result["files_absent_from_coverage_report"], ["docs/note.txt"])

    def test_changed_line_parser_preserves_upstream_context_marker_quirk(self) -> None:
        patch = (
            "diff --git a/pkg/module.py b/pkg/module.py\n"
            "--- a/pkg/module.py\n"
            "+++ b/pkg/module.py\n"
            "@@ -10,2 +10,3 @@\n"
            " value = (\n"
            "                     + 'context expression'\n"
            "+    'new expression'\n"
        )

        targets = tdd_changed_lines(patch)

        # TDD-Bench strips whitespace before checking '+', so it counts both
        # the actual addition and the context expression as changed.
        self.assertEqual(targets["after"], {"pkg/module.py": [11, 12]})

    def test_per_instance_score_is_coverage_times_f2p_gate(self) -> None:
        result = combine_tdd_coverage(
            "django__django-1",
            {"before": {"a.py": [1, 2]}, "after": {"a.py": [1, 2]}},
            {"total_changed": 2, "total_missed": 1},
            {"total_changed": 2, "total_missed": 0},
            {"failed": True},
            {"failed": False},
        )

        self.assertEqual(result["cov_score"], 0.75)
        self.assertEqual(result["fail_before"], 1)
        self.assertEqual(result["pass_after"], 1)
        self.assertEqual(result["final_score"], 0.75)

    def test_sympy_score_uses_only_f2p_gate(self) -> None:
        result = combine_tdd_coverage(
            "sympy__sympy-1",
            {"before": {"a.py": [1]}, "after": {"a.py": [1]}},
            {"total_changed": 1, "total_missed": 1},
            {"total_changed": 1, "total_missed": 1},
            {"failed": True},
            {"failed": False},
        )

        self.assertEqual(result["cov_score"], 0.0)
        self.assertTrue(result["sympy_coverage_exempt"])
        self.assertEqual(result["final_score"], 1.0)

    def test_global_score_uses_complete_dataset_denominator(self) -> None:
        aggregate = aggregate_tdd_bench_score(
            {
                "one": {"tdd_coverage": {"final_score": 0.75}},
                "two": {"status": "MISSING_GENERATION"},
                "three": {"tdd_coverage": {"final_score": 0.25}},
            },
            coverage_enabled=True,
        )

        self.assertEqual(aggregate["numerator"], 1.0)
        self.assertEqual(aggregate["denominator"], 3)
        self.assertAlmostEqual(aggregate["value"], 1.0 / 3.0)
        self.assertEqual(
            aggregate["zeroed_instances"], {"two": "MISSING_GENERATION"}
        )

    def test_tdd_and_swt_runners_are_not_shared(self) -> None:
        self.assertEqual(
            tdd_test_command(
                "django/django",
                "3.2",
                "tests/migrations/test_brt.py",
                "",
                with_coverage=True,
            ),
            "PYTHONPATH=tests:${PYTHONPATH:-} python -m coverage run "
            "./tests/runtests.py --verbosity 2 "
            "--settings=test_sqlite --parallel 1 migrations.test_brt",
        )
        self.assertEqual(
            tdd_test_command(
                "astropy/astropy",
                "1.3",
                "astropy/tests/test_brt.py",
                "",
                with_coverage=True,
            ),
            "python -m coverage run --branch -m pytest -rA -vv "
            "-o console_output_style=classic --tb=no astropy/tests/test_brt.py",
        )
        self.assertEqual(
            tdd_test_command(
                "sphinx-doc/sphinx",
                "5.1",
                "tests/test_brt.py",
                "",
                with_coverage=True,
            ),
            "tox --current-env -epy39 -v -- tests/test_brt.py",
        )

    def test_usetex_dependency_is_installed_only_for_matching_tdd_test(self) -> None:
        with mock.patch(
            "brt6.evaluation.direct_eval.run_shell",
            side_effect=[
                {"returncode": 1},
                {"returncode": 0},
                {"returncode": 0},
            ],
        ) as run:
            result = ensure_tdd_optional_runtime_tools(
                "matplotlib/matplotlib",
                'matplotlib.rcParams["text.usetex"] = True',
                "runtime",
                "/tmp/repo",
                120,
            )

        self.assertEqual(result["status"], "INSTALLED")
        self.assertIn("texlive-core", run.call_args_list[1].args[0])
        self.assertEqual(
            ensure_tdd_optional_runtime_tools(
                "django/django", "def test_x(): pass", "runtime", "/tmp", 120
            )["status"],
            "NOT_REQUIRED",
        )

    def test_tdd_output_does_not_emit_swt_delta_files(self) -> None:
        row = {
            "instance_id": "django__django-1",
            "repo": "django/django",
            "version": "3.2",
            "base_commit": "deadbeef",
            "patch": "",
        }
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset = root / "dataset.json"
            generated = root / "generated"
            repos = root / "repos"
            output = root / "evaluation"
            dataset.write_text(json.dumps([row]), encoding="utf-8")
            generated.mkdir()
            repos.mkdir()
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[2]
            )
            # The test fixture lives under /tmp in this environment, whose
            # mount reports zero free space even though the project volume is
            # healthy.  Keep the production preflight unchanged and disable
            # only its GB threshold for this tiny subprocess fixture.
            environment["BRT4_MIN_FREE_GB"] = "0"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brt6.evaluation.formal_eval",
                    "--instances_path",
                    str(dataset),
                    "--generated_dir",
                    str(generated),
                    "--repo_root_base",
                    str(repos),
                    "--output_dir",
                    str(output),
                    "--dataset_mode",
                    "tdd",
                    "--compute_patch_coverage",
                    "true",
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            metrics = json.loads((output / "metrics.json").read_text())
            self.assertEqual(metrics["dataset_mode"], "tdd")
            self.assertEqual(metrics["tdd_score"], 0.0)
            self.assertEqual(metrics["tdd_score_denominator"], 1)
            self.assertIsNone(metrics["delta_mean_change_coverage"])
            self.assertTrue((output / "tdd_coverage.json").is_file())
            self.assertTrue((output / "tdd_coverage_results.json").is_file())
            self.assertFalse((output / "delta_change_coverage.json").exists())
            self.assertFalse((output / "patch_coverage_results.json").exists())

    def test_f2p_only_output_emits_no_coverage_artifacts(self) -> None:
        row = {
            "instance_id": "django__django-1",
            "repo": "django/django",
            "version": "3.2",
            "base_commit": "deadbeef",
            "patch": "",
        }
        coverage_names = (
            "patch_coverage_results.json",
            "delta_change_coverage.json",
            "tdd_coverage_results.json",
            "tdd_coverage.json",
        )
        for dataset_mode in ("swt", "tdd"):
            with self.subTest(dataset_mode=dataset_mode), tempfile.TemporaryDirectory() as temporary_dir:
                root = Path(temporary_dir)
                dataset = root / "dataset.json"
                generated = root / "generated"
                repos = root / "repos"
                output = root / "evaluation"
                dataset.write_text(json.dumps([row]), encoding="utf-8")
                generated.mkdir()
                repos.mkdir()
                output.mkdir()
                for name in coverage_names:
                    (output / name).write_text("{}\n", encoding="utf-8")
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
                environment["BRT4_MIN_FREE_GB"] = "0"
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "brt6.evaluation.formal_eval",
                        "--instances_path",
                        str(dataset),
                        "--generated_dir",
                        str(generated),
                        "--repo_root_base",
                        str(repos),
                        "--output_dir",
                        str(output),
                        "--dataset_mode",
                        dataset_mode,
                        "--compute_patch_coverage",
                        "false",
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                metrics = json.loads((output / "metrics.json").read_text())
                self.assertFalse(metrics["patch_cov_enabled"])
                for name in coverage_names:
                    self.assertFalse((output / name).exists(), name)


if __name__ == "__main__":
    unittest.main()

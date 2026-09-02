from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt6.evaluation.direct_eval import (
    SWT_TRACE_PATH,
    _remove_untracked_patch_paths,
    adapt_generated_test_for_runner,
    aggregate_delta_change_coverage,
    collect_patch_side_coverage,
    combine_patch_coverage,
    direct_test_relpath,
    patch_paths_from_patch,
    patch_target_lines,
    parse_patch_coverage,
    runner_environment_error_category,
    test_command as build_test_command,
    test_command_for_directives as build_test_command_for_directives,
    test_directives_from_patch as extract_test_directives_from_patch,
    trace_test_command,
)


def _coverage_view(
    executable: dict[str, list[int]],
    hit_counts: dict[str, dict[int, int]],
    *,
    status: str = "OK",
) -> dict:
    return {
        "status": status,
        "executable_lines_by_file": executable,
        "hit_counts_by_file": {
            path: {str(line): count for line, count in counts.items()}
            for path, counts in hit_counts.items()
        },
    }


class PaperChangeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = {
            "buggy": {"pkg/module.py": [10, 11]},
            "fixed": {"pkg/module.py": [20, 21]},
        }
        self.pred_pre = _coverage_view(
            {"pkg/module.py": [10, 11]},
            {"pkg/module.py": {10: 2, 11: 0}},
        )
        self.pred_post = _coverage_view(
            {"pkg/module.py": [20, 21]},
            {"pkg/module.py": {20: 1, 21: 2}},
        )
        self.gold_pre = _coverage_view(
            {"pkg/module.py": [10, 11]},
            {"pkg/module.py": {10: 2, 11: 1}},
        )
        self.gold_post = _coverage_view(
            {"pkg/module.py": [20, 21]},
            {"pkg/module.py": {20: 1, 21: 1}},
        )
        self.base_pre = _coverage_view(
            {"pkg/module.py": [10, 11]},
            {"pkg/module.py": {10: 0, 11: 0}},
        )
        # The checked-in SWT-Bench implementation uses base_post for both
        # removed and added line deltas.  This fixture locks that compatibility
        # contract instead of silently substituting the semantic base_pre view.
        self.base_post = _coverage_view(
            {"pkg/module.py": [10, 11, 20, 21]},
            {"pkg/module.py": {10: 1, 11: 1, 20: 1, 21: 0}},
        )
        self.gold_base_pre = copy.deepcopy(self.base_pre)
        self.gold_base_post = copy.deepcopy(self.base_post)

    def _combined(self) -> dict:
        return combine_patch_coverage(
            self.targets,
            self.pred_pre,
            self.pred_post,
            self.gold_pre,
            self.gold_post,
            self.base_pre,
            self.base_post,
            self.gold_base_pre,
            self.gold_base_post,
        )

    def test_per_instance_delta_matches_swt_six_view_contract(self) -> None:
        result = self._combined()

        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["paper_metric_eligible"])
        self.assertEqual(result["removed_line_baseline_view"], "base_post")
        self.assertEqual(result["target_line_count"], 4)
        self.assertEqual(result["coverage_pred"], 0.75)
        self.assertEqual(result["coverage_delta_pred"], 0.5)
        self.assertEqual(result["coverage_delta_gold"], 0.5)
        self.assertEqual(result["gold_reference_coverage_delta"], 0.5)
        self.assertEqual(
            result["delta_covered_removed_lines"],
            [{"path": "pkg/module.py", "line": 10}],
        )
        self.assertEqual(
            result["delta_covered_added_lines"],
            [{"path": "pkg/module.py", "line": 21}],
        )

    def test_macro_delta_uses_only_gold_applicable_instances(self) -> None:
        first = self._combined()
        second = copy.deepcopy(first)
        second["coverage_delta_pred"] = 0.0
        no_executable = combine_patch_coverage(
            {"buggy": {}, "fixed": {}},
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
            _coverage_view({}, {}),
        )

        aggregate = aggregate_delta_change_coverage(
            {
                "instance-a": {"patch_coverage": first},
                "instance-b": {"patch_coverage": second},
                "instance-no-executable": {"patch_coverage": no_executable},
            },
            coverage_enabled=True,
        )

        self.assertTrue(aggregate["valid"])
        self.assertEqual(aggregate["denominator"], 2)
        self.assertEqual(aggregate["numerator"], 0.5)
        self.assertEqual(aggregate["value"], 0.25)
        self.assertEqual(
            aggregate["excluded_no_executable_ids"],
            ["instance-no-executable"],
        )

    def test_gold_denominator_is_independent_of_model_baseline(self) -> None:
        model_base_post = _coverage_view(
            {"pkg/module.py": [10, 11, 20, 21]},
            {"pkg/module.py": {10: 99, 11: 99, 20: 99, 21: 99}},
        )
        result = combine_patch_coverage(
            self.targets,
            self.pred_pre,
            self.pred_post,
            self.gold_pre,
            self.gold_post,
            self.base_pre,
            model_base_post,
            self.gold_base_pre,
            self.gold_base_post,
        )

        self.assertEqual(result["coverage_delta_pred"], 0.0)
        # SWT-Bench's per-prediction field uses the model run's base_post.
        self.assertEqual(result["coverage_delta_gold"], 0.0)
        # The separate gold run is retained only for paper macro eligibility.
        self.assertEqual(result["gold_reference_coverage_delta"], 0.5)
        self.assertTrue(result["gold_applicable"])
        self.assertEqual(
            result["gold_denominator_source"],
            "independent_gold_test_and_gold_baseline_views",
        )

    def test_missing_gold_applicability_is_excluded_like_swt_bench(self) -> None:
        aggregate = aggregate_delta_change_coverage(
            {
                "instance-a": {"patch_coverage": self._combined()},
                "instance-missing": {
                    "patch_coverage": {
                        "status": "MISSING_GENERATION",
                        "coverage_delta_pred": 0.0,
                    }
                },
            },
            coverage_enabled=True,
        )

        self.assertTrue(aggregate["valid"])
        self.assertEqual(aggregate["value"], 0.5)
        self.assertEqual(aggregate["denominator"], 1)
        self.assertEqual(
            aggregate["excluded_gold_unavailable"],
            {"instance-missing": "MISSING_GOLD_REFERENCE"},
        )

    def test_missing_model_view_contributes_zero_when_gold_is_applicable(self) -> None:
        model_missing = self._combined()
        model_missing["status"] = "INCOMPLETE_REFERENCE_COVERAGE"
        model_missing["paper_metric_eligible"] = False
        model_missing["coverage_delta_pred"] = None

        aggregate = aggregate_delta_change_coverage(
            {
                "instance-a": {"patch_coverage": self._combined()},
                "instance-model-missing": {"patch_coverage": model_missing},
            },
            coverage_enabled=True,
        )

        self.assertTrue(aggregate["valid"])
        self.assertEqual(aggregate["denominator"], 2)
        self.assertEqual(aggregate["numerator"], 0.5)
        self.assertEqual(aggregate["value"], 0.25)
        self.assertEqual(
            aggregate["zeroed_model_instances"],
            {"instance-model-missing": "INCOMPLETE_REFERENCE_COVERAGE"},
        )

    def test_coverage_command_can_run_the_complete_generated_file(self) -> None:
        complete_file = build_test_command(
            "astropy/astropy", "5.0", "tests/test_generated.py", ""
        )
        selected_test = build_test_command(
            "astropy/astropy",
            "5.0",
            "tests/test_generated.py",
            "GeneratedTests::test_one",
        )

        self.assertIn("tests/test_generated.py", complete_file)
        self.assertNotIn("::", complete_file)
        self.assertIn(
            "tests/test_generated.py::GeneratedTests::test_one", selected_test
        )

    def test_sympy_generic_test_path_is_mapped_below_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            instance_id = "sympy__sympy-12171"
            instance_dir = Path(temporary_dir) / instance_id
            instance_dir.mkdir(parents=True)
            (instance_dir / "summary.json").write_text(
                '{"candidate_repo_path": "tests/test_brt_sympy.py", '
                '"command": "bin/test -C tests/test_brt_sympy.py"}',
                encoding="utf-8",
            )
            self.assertEqual(
                direct_test_relpath(
                    instance_id,
                    temporary_dir,
                    "sympy/sympy",
                ),
                "sympy/tests/test_brt_sympy.py",
            )

    def test_django_module_function_gets_unittest_adapter(self) -> None:
        code = "def test_brt_case():\n    assert public_api() == 1\n"
        adapted, metadata = adapt_generated_test_for_runner(
            "django/django",
            code,
            "django__django-1",
        )
        self.assertTrue(metadata["applied"])
        self.assertIn("unittest.TestCase", adapted)
        self.assertIn("return test_brt_case()", adapted)

    def test_test_commands_match_checked_in_swt_bench_runners(self) -> None:
        self.assertEqual(
            build_test_command(
                "django/django", "3.2", "tests/migrations/test_brt.py", ""
            ),
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite "
            "--parallel 1 migrations.test_brt",
        )
        django_gis_command = build_test_command(
            "django/django",
            "4.2",
            "tests/gis_tests/gdal_tests/test_brt.py",
            "GeneratedTests::test_one",
        )
        self.assertIn("django_generated_test_runner.py", django_gis_command)
        self.assertIn(
            "--label gis_tests.gdal_tests.test_brt.GeneratedTests.test_one",
            django_gis_command,
        )
        self.assertEqual(
            build_test_command(
                "astropy/astropy", "5.1", "astropy/tests/test_brt.py", ""
            ),
            "pytest --no-header -rA --tb=no -p no:cacheprovider "
            "astropy/tests/test_brt.py",
        )
        self.assertEqual(
            build_test_command(
                "sphinx-doc/sphinx", "5.1", "tests/test_brt.py", ""
            ),
            "tox --current-env -epy39 -v -- tests/test_brt.py",
        )
        sympy_command = build_test_command(
            "sympy/sympy", "1.11", "sympy/tests/test_brt.py", ""
        )
        self.assertIn("runtime/legacy_sympy_compat", sympy_command)
        self.assertIn("ignore::DeprecationWarning", sympy_command)
        self.assertTrue(
            sympy_command.endswith(
                "bin/test -C --verbose sympy/tests/test_brt.py"
            )
        )
        self.assertEqual(
            build_test_command(
                "pytest-dev/pytest", "7.0", "testing/test_brt.py", ""
            ),
            "pytest -rA testing/test_brt.py",
        )
        self.assertEqual(
            build_test_command(
                "mwaskom/seaborn", "0.13", "tests/test_brt.py", ""
            ),
            "pytest --no-header -rA tests/test_brt.py",
        )

    def test_gold_directives_and_fixture_cleanup_have_separate_scopes(self) -> None:
        patch = (
            "diff --git a/tests/test_feature.py b/tests/test_feature.py\n"
            "--- a/tests/test_feature.py\n"
            "+++ b/tests/test_feature.py\n"
            "diff --git a/tests/static/config.toml b/tests/static/config.toml\n"
            "--- /dev/null\n"
            "+++ b/tests/static/config.toml\n"
            "diff --git a/tests/roots/example/index.rst "
            "b/tests/roots/example/index.rst\n"
            "--- /dev/null\n"
            "+++ b/tests/roots/example/index.rst\n"
        )

        self.assertEqual(
            patch_paths_from_patch(patch),
            [
                "tests/test_feature.py",
                "tests/static/config.toml",
                "tests/roots/example/index.rst",
            ],
        )
        # SWT-Bench filters TOML but historically leaves RST as a directive.
        self.assertEqual(
            extract_test_directives_from_patch(patch),
            ["tests/test_feature.py", "tests/roots/example/index.rst"],
        )
        self.assertEqual(
            build_test_command_for_directives(
                "sphinx-doc/sphinx",
                "5.1",
                extract_test_directives_from_patch(patch),
            ),
            "tox --current-env -epy39 -v -- tests/test_feature.py "
            "tests/roots/example/index.rst",
        )

    def test_tox_infrastructure_failure_is_not_a_model_fixed_fail(self) -> None:
        failure = {
            "returncode": 1,
            "stdout": "py39: packaging backend failed with FailedToStart",
            "stderr": "ModuleNotFoundError: No module named 'flit_core'",
        }

        self.assertEqual(
            runner_environment_error_category(
                "tox --current-env -epy39 -v -- tests/test_brt.py", failure
            ),
            "ENV_INCOMPLETE",
        )
        self.assertEqual(
            runner_environment_error_category(
                "pytest tests/test_brt.py", failure
            ),
            "",
        )

    def test_formal_eval_recovers_legacy_adaptive_seed_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            instance_id = "sphinx-doc__sphinx-1"
            instance_dir = Path(temporary_dir) / instance_id
            selected_dir = instance_dir / "seed_candidates" / "seed_2"
            selected_dir.mkdir(parents=True)
            (instance_dir / "summary.json").write_text(
                '{"selected_seed_index": 2, "seed_mode": "adaptive_top3"}',
                encoding="utf-8",
            )
            (selected_dir / "summary.json").write_text(
                '{"candidate_repo_path": "tests/test_brt.py", '
                '"command": "tox --current-env -epy39 -v -- tests/test_brt.py", '
                '"selector": "test_brt"}',
                encoding="utf-8",
            )

            self.assertEqual(
                direct_test_relpath(instance_id, temporary_dir),
                "tests/test_brt.py",
            )

    def test_reference_cleanup_removes_fixtures_but_preserves_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "coverage@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Coverage Test"],
                cwd=root,
                check=True,
            )
            tracked = root / "tests" / "test_feature.py"
            tracked.parent.mkdir(parents=True)
            tracked.write_text("def test_existing():\n    pass\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            fixture = root / "tests" / "static" / "config.toml"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("enabled = true\n", encoding="utf-8")

            removed = _remove_untracked_patch_paths(
                str(root),
                ["tests/test_feature.py", "tests/static/config.toml"],
            )

            self.assertTrue(tracked.is_file())
            self.assertFalse(fixture.exists())
            self.assertEqual(removed, ["tests/static/config.toml"])

    def test_changed_line_parser_matches_unified_diff_sides(self) -> None:
        patch = (
            "diff --git a/pkg/module.py b/pkg/module.py\n"
            "--- a/pkg/module.py\n"
            "+++ b/pkg/module.py\n"
            "@@ -10,3 +10,3 @@\n"
            " context\n"
            "-old_value = 1\n"
            "+new_value = 2\n"
            " context\n"
            "diff --git a/docs/note.txt b/docs/note.txt\n"
            "--- a/docs/note.txt\n"
            "+++ b/docs/note.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        targets = patch_target_lines(patch)

        self.assertEqual(targets["buggy"], {"pkg/module.py": [11]})
        self.assertEqual(targets["fixed"], {"pkg/module.py": [11]})

    def test_vendored_swt_tracer_captures_python_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            child = root / "child_module.py"
            child.write_text(
                "def child():\n"
                "    value = 1\n"
                "    return value\n"
                "\n"
                "child()\n",
                encoding="utf-8",
            )
            (root / "runner.py").write_text(
                "import subprocess\n"
                "import sys\n"
                "from pathlib import Path\n"
                "subprocess.run([sys.executable, str(Path(__file__).with_name('child_module.py'))], check=True)\n",
                encoding="utf-8",
            )
            coverage_output = root / "coverage.cover"
            command = trace_test_command(
                "python -m runner",
                str(coverage_output),
                str(root),
                {"child_module.py": [2]},
            )
            environment = dict(os.environ)
            environment["PATH"] = (
                str(Path(sys.executable).parent)
                + os.pathsep
                + environment.get("PATH", "")
            )
            environment["PYTHONPATH"] = (
                str(root)
                + os.pathsep
                + environment.get("PYTHONPATH", "")
            )

            completed = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            parsed = parse_patch_coverage(
                coverage_output,
                {"child_module.py": [2]},
                str(root),
            )

            self.assertTrue(SWT_TRACE_PATH.is_file())
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(parsed["status"], "OK")
            self.assertEqual(parsed["executable_lines_by_file"]["child_module.py"], [2])
            self.assertGreater(
                parsed["hit_counts_by_file"]["child_module.py"]["2"], 0
            )

    def test_trace_command_preserves_leading_environment_assignment(self) -> None:
        command = trace_test_command(
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
            "bin/test -C sympy/printing/tests/test_ccode.py",
            "/tmp/coverage.cover",
            "/tmp/repo",
            {"sympy/printing/ccode.py": [1]},
        )

        self.assertTrue(command.startswith("PYTHONWARNINGS="))
        self.assertEqual(command.count("PYTHONWARNINGS="), 1)
        self.assertIn("swt_trace.py", command)
        self.assertIn("bin/test -C sympy/printing/tests/test_ccode.py", command)

    def test_trace_command_matches_swt_pytest_and_tox_wrapping(self) -> None:
        pytest_command = trace_test_command(
            "pytest --no-header -rA --tb=no -p no:cacheprovider tests/test_a.py",
            "/tmp/coverage.cover",
            "/tmp/repo",
            {"pkg/module.py": [1]},
        )
        tox_command = trace_test_command(
            "tox -epy39 -v -- tests/test_a.py",
            "/tmp/coverage.cover",
            "/tmp/repo",
            {"pkg/module.py": [1]},
        )

        self.assertIn("-m pytest", pytest_command)
        self.assertNotIn("--tb=no", pytest_command)
        self.assertIn("-m tox -epy39 -v -- tests/test_a.py", tox_command)

    def test_missing_trace_is_empty_for_every_swt_view(self) -> None:
        run = {"returncode": 0, "stdout": "", "stderr": "", "timeout": False}
        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch(
            "brt6.evaluation.direct_eval.run_shell", return_value=run
        ):
            base = collect_patch_side_coverage(
                "demo__demo-1",
                "base_pre",
                {"pkg/module.py": [1]},
                "python missing_test.py",
                temporary_dir,
                "demo-env",
                temporary_dir,
                30,
            )
            prediction = collect_patch_side_coverage(
                "demo__demo-1",
                "pred_pre",
                {"pkg/module.py": [1]},
                "python missing_test.py",
                temporary_dir,
                "demo-env",
                temporary_dir,
                30,
            )

        self.assertEqual(base["status"], "NO_COVERAGE_FILES")
        self.assertTrue(base["swt_empty_coverage"])
        self.assertEqual(prediction["status"], "NO_COVERAGE_FILES")
        self.assertTrue(prediction["swt_empty_coverage"])

    def test_timeout_is_diagnostic_but_missing_swt_trace_is_empty_coverage(self) -> None:
        run = {
            "returncode": 124,
            "stdout": "",
            "stderr": "",
            "timeout": True,
        }
        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch(
            "brt6.evaluation.direct_eval.run_shell", return_value=run
        ):
            result = collect_patch_side_coverage(
                "demo__demo-1",
                "gold_post",
                {"pkg/module.py": [1]},
                "python missing_test.py",
                temporary_dir,
                "demo-env",
                temporary_dir,
                30,
            )

        self.assertEqual(result["status"], "NO_COVERAGE_FILES")
        self.assertEqual(result["execution_status"], "TIMEOUT")
        self.assertTrue(result["execution_timeout"])


if __name__ == "__main__":
    unittest.main()

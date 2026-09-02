import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProjectLayoutTests(unittest.TestCase):
    def test_generate_launcher_initializes_default_run_dir_before_temp_paths(self):
        runtime_tests = PROJECT_ROOT / ".runtime" / "tests"
        runtime_tests.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime_tests) as tmp:
            fixture_root = Path(tmp) / "brt6"
            scripts_dir = fixture_root / "scripts"
            fake_bin = Path(tmp) / "bin"
            scripts_dir.mkdir(parents=True)
            fake_bin.mkdir()
            shutil.copy2(PROJECT_ROOT / "scripts" / "run_generate.sh", scripts_dir)

            fake_python = fake_bin / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_PYTHON_ARGS\"\n"
            )
            fake_python.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["RUN_NAME"] = "launcher_default_dir_test"
            env["BEHAVIOR_TARGET_CACHE"] = "/root/frozen-behavior-cache"
            env["FAKE_PYTHON_ARGS"] = str(Path(tmp) / "python.args")
            for name in ("RUN_DIR", "TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME"):
                env.pop(name, None)

            completed = subprocess.run(
                ["bash", str(scripts_dir / "run_generate.sh")],
                cwd=fixture_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            run_dir = fixture_root / "results" / "runs" / env["RUN_NAME"]
            self.assertTrue((run_dir / "generation.done").is_file())
            self.assertTrue((run_dir / "tmp").is_dir())
            self.assertTrue((run_dir / "cache").is_dir())
            arguments = Path(env["FAKE_PYTHON_ARGS"]).read_text().splitlines()
            self.assertIn("--behavior-target-cache", arguments)
            cache_index = arguments.index("--behavior-target-cache")
            self.assertEqual(
                arguments[cache_index + 1], env["BEHAVIOR_TARGET_CACHE"]
            )

    def test_checkout_is_single_layer_brt6_root(self):
        self.assertEqual(PROJECT_ROOT.name, "brt6")
        self.assertFalse((PROJECT_ROOT / "brt6").is_dir())
        self.assertTrue((PROJECT_ROOT / "__init__.py").is_file())


    def test_active_launchers_use_brt6_module(self):
        self.assertIn(
            "python -m brt6.run",
            (PROJECT_ROOT / "scripts/run_generate.sh").read_text(),
        )
        self.assertIn(
            "python -m brt6.run_issue_rewrite",
            (PROJECT_ROOT / "scripts/run_issue_rewrite.sh").read_text(),
        )
        self.assertIn(
            "python -m brt6.direct_eval",
            (PROJECT_ROOT / "scripts/run_evaluate.sh").read_text(),
        )


    def test_run_guide_points_to_brt6_root(self):
        run_guide = (PROJECT_ROOT / "README_RUN.md").read_text()
        structure = (PROJECT_ROOT / "README_STRUCTURE.md").read_text()
        self.assertIn("cd /root/Baxxhy/BugReproduce/brt6", run_guide)
        self.assertIn("python -m brt6.run", run_guide)
        self.assertIn("BRT6 Structure", structure)

    def test_current_module_docs_use_brt6_identity(self):
        current_modules = (
            "core/config.py",
            "core/schema.py",
            "core/utils.py",
            "execution/feedback.py",
            "evaluation/direct_eval.py",
            "io/io_utils.py",
            "pipeline/run_issue_rewrite.py",
        )
        for relative_path in current_modules:
            first_line = (PROJECT_ROOT / relative_path).read_text().splitlines()[0]
            self.assertIn("BRT6", first_line, relative_path)
        evaluator = (PROJECT_ROOT / "evaluation/direct_eval.py").read_text()
        self.assertIn("BRT6 outputs", evaluator)

        migration = (PROJECT_ROOT / "MIGRATION_IMPLEMENTATION_GUIDE.md").read_text()
        self.assertNotIn("仍导入 `brt5.*`", migration)

    def test_redundant_forwarding_modules_are_removed(self):
        forwarding_modules = (
            "api_pool.py",
            "config.py",
            "dual_version.py",
            "executor.py",
            "feedback.py",
            "generator.py",
            "host_context.py",
            "icore_env_constants.py",
            "icore_env_utils.py",
            "icore_exec_spec.py",
            "icore_runtime.py",
            "io_utils.py",
            "issue_rewriter.py",
            "llm_client.py",
            "observation_oracle.py",
            "oracle.py",
            "patch_utils.py",
            "prompts.py",
            "schema.py",
            "semantic_guard.py",
            "strict_semantic_verifier.py",
            "utils.py",
            "verifier.py",
            "core/api_pool.py",
            "core/llm_client.py",
            "generation/patch_utils.py",
            "pipeline/feedback.py",
            "pipeline/host_context.py",
            "pipeline/issue_rewriter.py",
            "runtime/dual_version.py",
            "runtime/executor.py",
            "runtime/icore_env_constants.py",
            "runtime/icore_env_utils.py",
            "runtime/icore_exec_spec.py",
            "runtime/icore_runtime.py",
        )
        for relative_path in forwarding_modules:
            self.assertFalse(
                (PROJECT_ROOT / relative_path).exists(), relative_path
            )

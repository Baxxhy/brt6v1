from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from brt6.llm import api_pool
from brt6.scripts.prewarm_swt_environments import (
    attempt_diagnostics,
    failure_diagnostic,
    prepare_templates,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReproducibleBootstrapTests(unittest.TestCase):
    def test_api_pool_filters_deepseek_and_gpt_entries(self) -> None:
        payload = {
            "apis": [
                {
                    "provider": "deepseek",
                    "api_key": "deepseek-key",
                    "base_url": "https://deepseek.invalid",
                    "model": "deepseek-v3",
                },
                {
                    "provider": "gpt",
                    "api_key": "gpt-key",
                    "base_url": "https://gpt.invalid/v1",
                    "model": "gpt-5.4-mini",
                },
            ]
        }
        with mock.patch.dict(
            os.environ,
            {
                "BRT_API_POOL_JSON": json.dumps(payload),
                "BRT_API_POOL_FILE": "/does/not/exist",
            },
            clear=True,
        ), mock.patch.object(api_pool, "_configured_file", return_value=None):
            deepseek = api_pool.configured_apis("deepseek")
            gpt = api_pool.configured_apis("gpt")
        self.assertEqual(deepseek[0][0], "deepseek-key")
        self.assertEqual(gpt[0], ("gpt-key", "https://gpt.invalid/v1", "gpt-5.4-mini"))

    def test_configuring_gpt_preserves_existing_deepseek_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "api_pool.json"
            output.write_text(
                json.dumps(
                    {
                        "apis": [
                            {
                                "name": "deepseek-1",
                                "provider": "deepseek",
                                "api_key": "deepseek-key",
                                "base_url": "https://deepseek.invalid",
                                "model": "deepseek-v3",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "GPT_API_KEY": "gpt-key",
                "GPT_BASE_URL": "https://gpt.invalid/v1",
                "GPT_MODEL": "gpt-5.4-mini",
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "configure_api_keys.py"),
                    "--output",
                    str(output),
                    "--provider",
                    "gpt",
                    "--from-env",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            [_entry["provider"] for _entry in payload["apis"]],
            ["deepseek", "gpt"],
        )

    def test_api_pool_json_environment_supports_multiple_keys(self) -> None:
        payload = {
            "apis": [
                {"api_key": "test-key-one", "base_url": "https://one.invalid", "model": "m1"},
                {"api_key": "test-key-two", "base_url": "https://two.invalid", "model": "m2"},
            ]
        }
        with mock.patch.dict(
            os.environ,
            {
                "BRT_API_POOL_JSON": json.dumps(payload),
                "BRT_API_POOL_FILE": "/does/not/exist",
            },
            clear=False,
        ), mock.patch.object(api_pool, "_configured_file", return_value=None):
            entries = api_pool.configured_apis()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][1:], ("https://one.invalid", "m1"))

    def test_api_pool_file_has_priority_and_is_not_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.json"
            path.write_text(
                json.dumps(
                    {
                        "apis": [
                            {
                                "api_key": "test-file-key",
                                "base_url": "https://file.invalid",
                                "model": "file-model",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"BRT_API_POOL_FILE": str(path)}, clear=False
            ):
                entries = api_pool.configured_apis()
        self.assertEqual(entries, [("test-file-key", "https://file.invalid", "file-model")])

    def test_api_pool_supports_single_key_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "test-single-key",
                "DEEPSEEK_BASE_URL": "https://single.invalid",
                "DEEPSEEK_MODEL": "single-model",
                "BRT_API_POOL_FILE": "/does/not/exist",
            },
            clear=True,
        ), mock.patch.object(api_pool, "_configured_file", return_value=None):
            entries = api_pool.configured_apis()
        self.assertEqual(
            entries,
            [("test-single-key", "https://single.invalid", "single-model")],
        )

    def test_reproduction_files_exist(self) -> None:
        required = [
            "requirements.txt",
            "requirements-framework.txt",
            "README_REPRODUCE.md",
            ".env.example",
            "config/api_pool.example.json",
            "scripts/bootstrap_machine.sh",
            "scripts/bootstrap_fresh_swt_server.sh",
            "scripts/bootstrap_repositories.py",
            "scripts/configure_api_keys.py",
            "scripts/export_clean_repo.py",
            "scripts/check_repository_secrets.py",
            "scripts/run_swt_experiment.sh",
        ]
        for relative in required:
            self.assertTrue((PROJECT_ROOT / relative).is_file(), relative)

    def test_framework_requirements_exclude_benchmark_environments(self) -> None:
        requirements = {
            line.strip()
            for line in (PROJECT_ROOT / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(
            requirements,
            {
                "datasets==5.0.0",
                "packaging==26.0",
                "requests==2.34.2",
                'tomli>=2.0; python_version < "3.11"',
            },
        )
        self.assertEqual(
            (PROJECT_ROOT / "requirements-framework.txt")
            .read_text(encoding="utf-8")
            .splitlines()[-1],
            "-r requirements.txt",
        )

    def test_launcher_has_no_fixed_workspace_root(self) -> None:
        launcher = (
            PROJECT_ROOT / "scripts" / "run_p0_simple_llm_selector_full.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('PROJECT_ROOT=${PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}', launcher)
        self.assertIn('REPO_ROOT=${REPO_ROOT:-$PACKAGE_ROOT/swe_repos}', launcher)
        self.assertNotIn("PROJECT_ROOT=${PROJECT_ROOT:-/root/Baxxhy", launcher)
        self.assertIn('--model {deepseek|gpt}', launcher)
        self.assertIn('--llm-provider "$LLM_PROVIDER"', launcher)

    def test_fresh_swt_bootstrap_reuses_existing_conda_and_prewarms(self) -> None:
        bootstrap = (
            PROJECT_ROOT / "scripts" / "bootstrap_fresh_swt_server.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("must already be installed", bootstrap)
        self.assertNotIn("Miniconda3-", bootstrap)
        self.assertNotIn("Miniforge3-", bootstrap)
        self.assertIn("prewarm_swt_environments.py", bootstrap)
        self.assertIn("CONDA_ENVS_PATH", bootstrap)
        self.assertIn("CONDA_PKGS_DIRS", bootstrap)
        self.assertIn("template_environment_gate=52/52_ready", bootstrap)
        self.assertIn("validate_behavior_target_cache.py", bootstrap)
        self.assertIn("bootstrap_repositories.py", bootstrap)
        self.assertIn('PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)', bootstrap)
        self.assertIn("--controller-python", bootstrap)
        self.assertIn("reused_controller_python=", bootstrap)
        self.assertIn("sys.version_info >= (3, 10)", bootstrap)
        self.assertGreaterEqual(
            bootstrap.count('rmdir "$PROBE_PARENT" >/dev/null 2>&1 || true'),
            2,
        )
        self.assertNotIn("EXPECTED_PROJECT_ROOT", bootstrap)
        self.assertNotIn("/root/Baxxhy/BugReproduce/brt5", bootstrap)

    def test_swt_wrapper_loads_generated_runtime_contract(self) -> None:
        wrapper = (
            PROJECT_ROOT / "scripts" / "run_swt_experiment.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(".bootstrap/use_fresh_swt_server.sh", wrapper)
        self.assertIn("--dataset swt", wrapper)
        self.assertIn("run_p0_simple_llm_selector_full.sh", wrapper)
        self.assertIn('PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)', wrapper)
        self.assertNotIn("EXPECTED_PROJECT_ROOT", wrapper)
        self.assertNotIn("/root/Baxxhy/BugReproduce/brt5", wrapper)

    def test_swt_failure_diagnostic_identifies_shell_redirection(self) -> None:
        diagnostic = failure_diagnostic(
            {
                "status": "CREATE_ERROR",
                "returncode": 1,
                "stderr": (
                    "/tmp/brt3_icore_env_setup.sh: line 6: "
                    "3: No such file or directory"
                ),
            }
        )
        self.assertEqual(diagnostic["category"], "SHELL_REDIRECTION")
        self.assertEqual(diagnostic["returncode"], 1)

    def test_swt_failure_diagnostic_identifies_proxy_configuration(self) -> None:
        diagnostic = failure_diagnostic(
            {
                "status": "CREATE_ERROR",
                "returncode": 1,
                "stderr": (
                    "ProxyError: Conda cannot proceed due to an error in your "
                    "proxy configuration."
                ),
            }
        )
        self.assertEqual(diagnostic["category"], "PROXY_CONFIGURATION")
        self.assertEqual(diagnostic["returncode"], 1)

    def test_swt_failure_diagnostic_prefers_health_category(self) -> None:
        diagnostic = failure_diagnostic(
            {
                "status": "ENV_HEALTH_ERROR",
                "returncode": 1,
                "health": {
                    "ok": False,
                    "category": "ENV_NOT_FOUND",
                    "reason": "conda environment not found",
                },
                "stderr": "generic failure",
            }
        )
        self.assertEqual(diagnostic["category"], "ENV_NOT_FOUND")
        self.assertEqual(diagnostic["error_line"], "conda environment not found")

    def test_swt_attempt_diagnostics_preserves_initial_root_cause(self) -> None:
        diagnostic = attempt_diagnostics(
            [
                {
                    "attempt": 1,
                    "elapsed_seconds": 7.1,
                    "result": {
                        "status": "CREATE_ERROR",
                        "returncode": 1,
                        "stderr": (
                            "/tmp/brt3_icore_env_setup.sh: line 6: "
                            "3: No such file or directory"
                        ),
                    },
                },
                {
                    "attempt": 2,
                    "elapsed_seconds": 1.0,
                    "result": {
                        "status": "ENV_HEALTH_ERROR",
                        "returncode": 1,
                        "health": {
                            "ok": False,
                            "category": "ENV_NOT_FOUND",
                        },
                    },
                },
            ]
        )
        self.assertEqual(
            diagnostic["root_cause"]["category"], "SHELL_REDIRECTION"
        )
        self.assertEqual(len(diagnostic["attempt_history"]), 2)

    def test_swt_template_prewarm_uses_bounded_parallelism(self) -> None:
        templates = [
            {
                "env_name": f"env_{index}",
                "instance_id": f"instance_{index}",
                "repo": "owner/repo",
                "version": str(index),
                "base_commit": f"base_{index}",
                "environment_setup_commit": f"setup_{index}",
            }
            for index in range(4)
        ]
        active = 0
        maximum_active = 0
        counter_lock = threading.Lock()

        def fake_prepare(template, work_root, timeout, retries):
            nonlocal active, maximum_active
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with counter_lock:
                active -= 1
            return {
                **template,
                "status": "READY",
                "attempts": [],
                "result": {"returncode": 0},
            }

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "brt6.scripts.prewarm_swt_environments.prepare_template",
            side_effect=fake_prepare,
        ):
            results = prepare_templates(
                templates,
                Path(tmp),
                timeout=10,
                retries=1,
                workers=3,
            )

        self.assertGreater(maximum_active, 1)
        self.assertLessEqual(maximum_active, 3)
        self.assertEqual(
            [item["env_name"] for item in results],
            [item["env_name"] for item in templates],
        )


if __name__ == "__main__":
    unittest.main()

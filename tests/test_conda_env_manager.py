from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from brt6.retrieval import icore_env_utils, icore_exec_spec, icore_runtime
from brt6.retrieval.icore_env_constants import (
    MAP_VERSION_TO_INSTALL_ASTROPY,
    MAP_VERSION_TO_INSTALL_MATPLOTLIB,
    MAP_VERSION_TO_INSTALL_SPHINX,
)
from brt6.evaluation.direct_eval import setup_command as formal_setup_command
from brt6.retrieval.icore_runtime import icore_setup_command
from brt6.runtime import conda_env_manager as envm


def issue(instance_id: str = "django__django-12184") -> dict:
    return {
        "instance_id": instance_id,
        "repo": "django/django",
        "version": "3.1",
        "base_commit": "abc123",
        "environment_setup_commit": "setup789",
    }


def write_instance(root: Path, instance_id: str, repo_prepare: dict | None = None, summary: dict | None = None) -> None:
    inst = root / instance_id
    inst.mkdir(parents=True, exist_ok=True)
    if repo_prepare is not None:
        (inst / "repo_prepare.json").write_text(json.dumps(repo_prepare), encoding="utf-8")
    if summary is not None:
        (inst / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


class CondaEnvManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        envm._INVENTORY_CACHE.clear()
        envm._DEPENDENCY_COMPAT_CACHE.clear()
        envm._HEALTH_CACHE.clear()
        envm._MANIFEST_CACHE.clear()
        self.tmp.cleanup()

    def test_run_script_preserves_validated_environment_without_login_profile(self) -> None:
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(
            icore_runtime,
            "run_subprocess_tree",
            return_value=completed,
        ) as runner:
            result = icore_runtime._run_script("printf ok", str(self.root), 30)

        runner.assert_called_once_with(
            ["bash", "-c", "printf ok"],
            cwd=str(self.root),
            timeout=30,
        )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "ok")

    def patch_envs(self, inventory: dict[str, str], unhealthy: set[str] | None = None):
        unhealthy = unhealthy or set()

        def fake_health(name: str, timeout: int = 60, refresh: bool = False) -> dict:
            if name not in inventory:
                return {"ok": False, "category": "ENV_NOT_FOUND", "env_name": name}
            if name in unhealthy:
                return {"ok": False, "category": "ENV_INCOMPLETE", "env_name": name, "env_path": inventory[name]}
            return {"ok": True, "category": "", "env_name": name, "env_path": inventory[name], "version": "3.10.0"}

        return mock.patch.multiple(
            envm,
            conda_env_inventory=mock.Mock(return_value=inventory),
            env_health_check=mock.Mock(side_effect=fake_health),
        )

    def test_new_run_uses_recorded_run_prefix_env(self) -> None:
        iid = "django__django-12184"
        recorded = "run_x_setup_django_django__3.1"
        write_instance(self.root, iid, {"env_name": recorded})
        with self.patch_envs({recorded: "/envs/run"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, recorded)
        self.assertEqual(res.resolution_source, "repo_prepare.json:env_name")

    def test_sphinx_uses_current_isolated_conda_runtime_for_tox(self) -> None:
        packages = MAP_VERSION_TO_INSTALL_SPHINX["5.1"]["pip_packages"]
        self.assertIn("tox==4.30.3", packages)
        self.assertIn("tox-current-env==0.0.17", packages)
        self.assertEqual(
            icore_runtime.icore_test_command(
                "sphinx-doc/sphinx",
                "5.1",
                "tests/test_brt.py",
                "test_brt",
            ),
            "tox --current-env -epy39 -v -- tests/test_brt.py::test_brt",
        )

    def test_old_run_uses_recorded_unprefixed_env(self) -> None:
        iid = "django__django-12184"
        recorded = "setup_django_django__3.1"
        write_instance(self.root, iid, {"env_name": recorded})
        with self.patch_envs({recorded: "/envs/legacy"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, recorded)
        self.assertFalse(res.legacy_fallback_used)

    def test_mixed_run_resolves_each_instance_from_own_metadata(self) -> None:
        write_instance(self.root, "django__django-1", {"env_name": "setup_django_django__3.1"})
        write_instance(self.root, "django__django-2", {"env_name": "run_x_setup_django_django__3.1"})
        with self.patch_envs({"setup_django_django__3.1": "/envs/a", "run_x_setup_django_django__3.1": "/envs/b"}):
            a = envm.resolve_eval_env(issue("django__django-1"), str(self.root), run_prefix="run_x_")
            b = envm.resolve_eval_env(issue("django__django-2"), str(self.root), run_prefix="run_x_")
        self.assertEqual(a.resolved_env, "setup_django_django__3.1")
        self.assertEqual(b.resolved_env, "run_x_setup_django_django__3.1")

    def test_environment_nested_metadata_is_used(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"environment": {"env_name": "recorded_env", "status": "READY"}})
        with self.patch_envs({"recorded_env": "/envs/recorded"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root))
        self.assertEqual(res.resolved_env, "recorded_env")

    def test_template_environment_metadata_precedes_disposable_runtime(self) -> None:
        iid = "django__django-12184"
        write_instance(
            self.root,
            iid,
            {
                "env_name": "brt5i_generation_runtime",
                "template_env_name": "setup_django_django__3.1__setup789",
                "runtime_environment": {
                    "env_name": "brt5i_generation_runtime",
                    "template_env_name": "setup_django_django__3.1__setup789",
                },
            },
        )
        records = envm.extract_env_records(iid, str(self.root))
        self.assertEqual(
            records[0]["env_name"], "setup_django_django__3.1__setup789"
        )
        self.assertEqual(records[0]["environment_role"], "dependency_template")

    def test_environment_manifest_probe_supports_python36_templates(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"python":"3.6.15","executable":"/env/bin/python",'
                '"prefix":"/env","packages":{},"pip_check_returncode":0,'
                '"pip_check_stdout":"","pip_check_stderr":""}\n'
            ),
            stderr="",
        )
        with mock.patch.object(
            envm, "conda_env_inventory", return_value={"legacy": "/env"}
        ), mock.patch.object(envm.subprocess, "run", return_value=completed) as run:
            manifest = envm.environment_manifest("legacy", refresh=True)

        injected_script = run.call_args.args[0][-1]
        self.assertNotIn("text=True", injected_script)
        self.assertIn("universal_newlines=True", injected_script)
        self.assertTrue(manifest["ok"])

    def test_missing_metadata_uses_legacy_exact_fallback(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {})
        with self.patch_envs({"setup_django_django__3.1": "/envs/legacy"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="missing_")
        self.assertEqual(res.resolved_env, "setup_django_django__3.1")
        self.assertTrue(res.legacy_fallback_used)

    def test_recorded_missing_env_does_not_fallback(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "missing_recorded"})
        with self.patch_envs({"setup_django_django__3.1": "/envs/legacy"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, "missing_recorded")
        self.assertFalse(res.env_exists)
        self.assertIn("refusing fallback", res.errors[0])

    def test_recorded_env_wins_when_prefix_and_legacy_both_exist(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "setup_django_django__3.1"})
        with self.patch_envs({"setup_django_django__3.1": "/envs/legacy", "run_x_setup_django_django__3.1": "/envs/run"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertEqual(res.resolved_env, "setup_django_django__3.1")

    def test_existing_env_with_failed_health_is_reported(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "bad_env"})
        with self.patch_envs({"bad_env": "/envs/bad"}, unhealthy={"bad_env"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root))
        self.assertTrue(res.env_exists)
        self.assertFalse(res.env_health["ok"])
        self.assertEqual(res.env_health["category"], "ENV_INCOMPLETE")

    def test_setup_interrupted_status_is_extracted(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"environment": {"env_name": "env_a", "status": "creating"}})
        records = envm.extract_env_records(iid, str(self.root))
        self.assertEqual(records[0]["setup_status"], "creating")

    def test_two_workers_request_same_env_deterministically(self) -> None:
        self.assertEqual(
            envm.default_env_name(issue(), prefix="run_x_"),
            envm.default_env_name(issue(), prefix="run_x_"),
        )

    def test_dependency_env_identity_uses_setup_commit_not_source_commit(self) -> None:
        first = envm.default_env_name(issue(), prefix="")
        second_issue = issue()
        second_issue["base_commit"] = "def456"
        second = envm.default_env_name(second_issue, prefix="")
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("__setup789"))

    def test_dependency_env_identity_changes_with_setup_commit(self) -> None:
        first = envm.default_env_name(issue(), prefix="")
        second_issue = issue()
        second_issue["environment_setup_commit"] = "other456"
        self.assertNotEqual(first, envm.default_env_name(second_issue, prefix=""))

    def test_dependency_seed_candidates_use_only_exact_legacy_schemes(self) -> None:
        row = issue()
        canonical = envm.default_env_name(row, prefix="")
        legacy = envm.legacy_env_name(row, prefix="")
        target = envm.default_env_name(row, prefix="run_x_")
        inventory = {
            canonical: "/envs/canonical",
            legacy: "/envs/legacy",
            f"direct_brt_we2_{legacy}": "/envs/direct2",
            f"direct_brt_we0_{legacy}": "/envs/direct0",
            f"other_run_{legacy}": "/envs/other",
            f"direct_brt_weX_{legacy}": "/envs/malformed",
        }
        self.assertEqual(
            envm.dependency_seed_candidates(row, inventory, target),
            [
                canonical,
                legacy,
                f"direct_brt_we0_{legacy}",
                f"direct_brt_we2_{legacy}",
            ],
        )

    def test_dependency_seed_candidates_never_reuse_target(self) -> None:
        row = issue()
        target = envm.default_env_name(row, prefix="")
        self.assertNotIn(
            target,
            envm.dependency_seed_candidates(row, {target: "/envs/target"}, target),
        )

    def test_editable_scrub_uses_postcondition_not_pip_returncode(self) -> None:
        payload = {
            "uninstall": {"returncode": 1, "stderr": "stale editable path"},
            "remaining_editable_bindings": [],
        }
        command_result = {
            "returncode": 0,
            "stdout": json.dumps(payload) + "\n",
            "stderr": "",
        }
        with mock.patch.object(
            icore_runtime, "_run_script", return_value=command_result
        ):
            result = icore_runtime._scrub_cloned_editable_installs(
                "env_a", str(self.root), 120
            )
        self.assertEqual(result["returncode"], 0)

    def test_editable_scrub_rejects_remaining_external_binding(self) -> None:
        payload = {
            "uninstall": {"returncode": 0},
            "remaining_editable_bindings": ["/env/site-packages/project.pth"],
        }
        command_result = {
            "returncode": 0,
            "stdout": json.dumps(payload) + "\n",
            "stderr": "",
        }
        with mock.patch.object(
            icore_runtime, "_run_script", return_value=command_result
        ):
            result = icore_runtime._scrub_cloned_editable_installs(
                "env_a", str(self.root), 120
            )
        self.assertEqual(result["returncode"], 1)

    def test_editable_scrub_receives_project_distribution(self) -> None:
        payload = {
            "project_distribution": "matplotlib",
            "uninstall": {"returncode": 0},
            "remaining_editable_bindings": [],
        }
        command_result = {
            "returncode": 0,
            "stdout": json.dumps(payload) + "\n",
            "stderr": "",
        }
        with mock.patch.object(
            icore_runtime, "_run_script", return_value=command_result
        ) as run:
            result = icore_runtime._scrub_cloned_editable_installs(
                "env_a", str(self.root), 120, project_distribution="matplotlib"
            )
        self.assertEqual(result["returncode"], 0)
        self.assertIn("matplotlib", run.call_args.args[0])

    def test_environment_state_round_trip_is_atomic(self) -> None:
        with mock.patch.dict("os.environ", {"BRT4_ENV_CACHE_DIR": str(self.root)}):
            path = envm.write_environment_state("env_a", "key_a", {"status": "ready"})
            state = envm.read_environment_state("env_a", "key_a")
        self.assertTrue(path.is_file())
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["cache_key"], "key_a")

    def test_failed_old_fingerprint_allows_partial_environment_recovery(self) -> None:
        with mock.patch.dict("os.environ", {"BRT4_ENV_CACHE_DIR": str(self.root)}):
            envm.write_environment_state(
                "env_a", "old_fingerprint", {"status": "failed"}
            )
            self.assertEqual(
                envm.read_environment_state("env_a", "new_fingerprint"), {}
            )
            self.assertTrue(envm.has_recoverable_environment_state("env_a"))

    def test_ready_old_fingerprint_does_not_authorize_cleanup(self) -> None:
        with mock.patch.dict("os.environ", {"BRT4_ENV_CACHE_DIR": str(self.root)}):
            envm.write_environment_state(
                "env_a", "old_fingerprint", {"status": "ready"}
            )
            self.assertFalse(envm.has_recoverable_environment_state("env_a"))

    def test_environment_identity_round_trip_is_atomic(self) -> None:
        with mock.patch.dict("os.environ", {"BRT4_ENV_CACHE_DIR": str(self.root)}):
            path = envm.write_environment_identity(
                "env_a", {"repo": "django/django", "environment_setup_commit": "abc"}
            )
            state = envm.read_environment_identity("env_a")
        self.assertTrue(path.is_file())
        self.assertEqual(state["repo"], "django/django")

    def test_environment_replacement_invalidates_process_local_observations(self) -> None:
        envm._HEALTH_CACHE["env_a"] = {"ok": False}
        envm._HEALTH_CACHE["env_b"] = {"ok": True}
        envm._DEPENDENCY_COMPAT_CACHE[("env_a", "one")] = {"ok": False}
        envm._DEPENDENCY_COMPAT_CACHE[("env_b", "two")] = {"ok": True}
        envm.invalidate_environment_runtime_cache("env_a")
        self.assertNotIn("env_a", envm._HEALTH_CACHE)
        self.assertNotIn(("env_a", "one"), envm._DEPENDENCY_COMPAT_CACHE)
        self.assertIn("env_b", envm._HEALTH_CACHE)
        self.assertIn(("env_b", "two"), envm._DEPENDENCY_COMPAT_CACHE)

    def test_dependency_compatibility_requires_declared_versions(self) -> None:
        snapshot = {
            "python": "3.9.19",
            "packages": {"numpy": "1.25.2", "pytest": "8.2.0"},
            "marker_environment": {
                "python_version": "3.9",
                "python_full_version": "3.9.19",
                "sys_platform": "linux",
                "os_name": "posix",
                "platform_machine": "x86_64",
                "platform_release": "",
                "platform_system": "Linux",
                "platform_version": "",
                "platform_python_implementation": "CPython",
                "implementation_name": "cpython",
                "implementation_version": "3.9.19",
                "extra": "",
            },
        }
        proc = mock.Mock(returncode=0, stdout=json.dumps(snapshot) + "\n", stderr="")
        with mock.patch.object(
            envm, "conda_env_inventory", return_value={"legacy": "/envs/legacy"}
        ), mock.patch.object(envm.subprocess, "run", return_value=proc):
            compatible = envm.dependency_env_compatibility(
                "legacy", {"python": "3.9", "pip_packages": ["numpy==1.25.2", "pytest"]}
            )
            incompatible = envm.dependency_env_compatibility(
                "legacy", {"python": "3.9", "pip_packages": ["numpy==1.24.0"]}
            )
        self.assertTrue(compatible["ok"])
        self.assertFalse(incompatible["ok"])
        self.assertEqual(incompatible["version_mismatches"][0]["installed"], "1.25.2")

    def test_dependency_compatibility_rejects_duplicate_metadata(self) -> None:
        snapshot = {
            "python": "3.10.14",
            "packages": {"numpy": "1.23.0"},
            "duplicate_metadata": {
                "numpy": [
                    {"version": "1.23.0", "metadata_path": "/env/numpy-a.dist-info"},
                    {"version": "2.0.0", "metadata_path": "/env/numpy-b.dist-info"},
                ]
            },
            "runtime_probes": {
                "numpy": {
                    "ok": True,
                    "module": "numpy",
                    "version": "1.23.0",
                    "module_file": "/env/numpy/__init__.py",
                }
            },
            "marker_environment": {},
        }
        proc = mock.Mock(returncode=0, stdout=json.dumps(snapshot) + "\n", stderr="")
        with mock.patch.object(
            envm, "conda_env_inventory", return_value={"legacy": "/envs/legacy"}
        ), mock.patch.object(envm.subprocess, "run", return_value=proc):
            result = envm.dependency_env_compatibility(
                "legacy", {"python": "3.10", "pip_packages": ["numpy==1.23.0"]}
            )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["requirement_checks"][0]["reason"], "duplicate_metadata"
        )
        self.assertEqual(len(result["duplicate_metadata"]["numpy"]), 2)

    def test_dependency_compatibility_rejects_runtime_import_failure(self) -> None:
        snapshot = {
            "python": "3.10.14",
            "packages": {"numpy": "1.23.0", "pandas": "1.5.3"},
            "duplicate_metadata": {},
            "runtime_probes": {
                "numpy": {
                    "ok": True,
                    "module": "numpy",
                    "version": "1.23.0",
                    "module_file": "/env/numpy/__init__.py",
                },
                "pandas": {
                    "ok": False,
                    "module": "pandas",
                    "version": "",
                    "module_file": "",
                    "error": "ValueError('numpy.dtype size changed')",
                },
            },
            "marker_environment": {},
        }
        proc = mock.Mock(returncode=0, stdout=json.dumps(snapshot) + "\n", stderr="")
        with mock.patch.object(
            envm, "conda_env_inventory", return_value={"legacy": "/envs/legacy"}
        ), mock.patch.object(envm.subprocess, "run", return_value=proc):
            result = envm.dependency_env_compatibility(
                "legacy",
                {
                    "python": "3.10",
                    "pip_packages": ["numpy==1.23.0", "pandas==1.5.3"],
                },
            )
        checks = {check["name"]: check for check in result["requirement_checks"]}
        self.assertFalse(result["ok"])
        self.assertTrue(checks["numpy"]["ok"])
        self.assertEqual(checks["pandas"]["reason"], "runtime_import_error")

    def test_dependency_compatibility_checks_imported_version(self) -> None:
        snapshot = {
            "python": "3.11.9",
            "packages": {"pyparsing": "3.0.9"},
            "duplicate_metadata": {},
            "runtime_probes": {
                "pyparsing": {
                    "ok": True,
                    "module": "pyparsing",
                    "version": "3.1.4",
                    "module_file": "/env/pyparsing/__init__.py",
                }
            },
            "marker_environment": {},
        }
        proc = mock.Mock(returncode=0, stdout=json.dumps(snapshot) + "\n", stderr="")
        with mock.patch.object(
            envm, "conda_env_inventory", return_value={"legacy": "/envs/legacy"}
        ), mock.patch.object(envm.subprocess, "run", return_value=proc):
            result = envm.dependency_env_compatibility(
                "legacy", {"python": "3.11", "pip_packages": ["pyparsing==3.0.9"]}
            )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["requirement_checks"][0]["reason"],
            "runtime_version_mismatch",
        )
        self.assertEqual(result["requirement_checks"][0]["imported"], "3.1.4")

    def test_dependency_compatibility_checks_cython_numpy_interface(self) -> None:
        snapshot = {
            "python": "3.9.19",
            "packages": {"cython": "0.29.33", "numpy": "1.23.0"},
            "duplicate_metadata": {},
            "runtime_probes": {
                "cython": {
                    "ok": True,
                    "module": "Cython",
                    "version": "0.29.33",
                    "module_file": "/env/Cython/__init__.py",
                },
                "numpy": {
                    "ok": True,
                    "module": "numpy",
                    "version": "1.23.0",
                    "module_file": "/env/numpy/__init__.py",
                },
            },
            "interface_probes": {
                "cython_numpy_pxd": {
                    "ok": False,
                    "returncode": 1,
                    "stderr": "'int_t' is not a type identifier",
                }
            },
            "marker_environment": {},
        }
        proc = mock.Mock(returncode=0, stdout=json.dumps(snapshot) + "\n", stderr="")
        with mock.patch.object(
            envm, "conda_env_inventory", return_value={"legacy": "/envs/legacy"}
        ), mock.patch.object(envm.subprocess, "run", return_value=proc):
            result = envm.dependency_env_compatibility(
                "legacy",
                {
                    "python": "3.9",
                    "pip_packages": ["cython==0.29.33", "numpy==1.23"],
                },
            )
        checks = {check["name"]: check for check in result["requirement_checks"]}
        self.assertFalse(result["ok"])
        self.assertEqual(checks["cython"]["reason"], "runtime_interface_error")
        self.assertEqual(checks["numpy"]["reason"], "runtime_interface_error")

    def test_dependency_compatibility_supports_pep440_and_target_markers(self) -> None:
        packages = {"numpy": "1.25.2", "asgiref": "3.2.10"}
        marker_environment = {
            "python_version": "3.6",
            "python_full_version": "3.6.15",
            "sys_platform": "linux",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_release": "",
            "platform_system": "Linux",
            "platform_version": "",
            "platform_python_implementation": "CPython",
            "implementation_name": "cpython",
            "implementation_version": "3.6.15",
            "extra": "",
        }
        lower_bound = envm._requirement_compatibility(
            "asgiref>=3.2", packages, marker_environment
        )
        upper_bound = envm._requirement_compatibility(
            "numpy<2", packages, marker_environment
        )
        skipped = envm._requirement_compatibility(
            "colorama; sys_platform == 'win32'", packages, marker_environment
        )
        self.assertTrue(lower_bound["ok"])
        self.assertTrue(upper_bound["ok"])
        self.assertFalse(skipped["applicable"])
        self.assertTrue(skipped["ok"])

    def test_dependency_install_spec_extracts_heredoc_requirements(self) -> None:
        script = """cat <<'EOF_123' > $HOME/requirements.txt
asgiref >= 3.2
astroid==2.12.13  # Pinned for tests
pylibmc; sys.platform != 'win32'
EOF_123
python -m pip install -r $HOME/requirements.txt
"""
        spec = envm.dependency_install_spec(
            {"python": "3.6", "packages": "requirements.txt"}, script
        )
        self.assertEqual(
            spec["pip_packages"],
            [
                "asgiref >= 3.2",
                "astroid==2.12.13",
                "pylibmc; sys.platform != 'win32'",
            ],
        )

    def test_requirements_script_is_worktree_local_and_environment_specific(self) -> None:
        spec = SimpleNamespace(
            install={"python": "3.9", "packages": "requirements.txt"},
            repo="pylint-dev/pylint",
            environment_setup_commit="abc123",
            env_name="setup_pylint_2_15",
        )
        with mock.patch.object(
            icore_exec_spec,
            "get_requirements_by_commit",
            return_value="astroid==2.12.13\n",
        ):
            commands = icore_exec_spec.ExecSpec.req_install_commands.fget(spec)
        rendered = "\n".join(commands)
        self.assertIn(
            '"$PWD/.brt-env/requirements-setup_pylint_2_15.txt"', rendered
        )
        self.assertIn("rm -f", rendered)
        self.assertNotIn("$HOME/requirements.txt", rendered)
        self.assertNotIn("/root/requirements.txt", rendered)

    def test_requirements_lookup_continues_after_first_path_is_missing(self) -> None:
        missing = RuntimeError("first requirements path returned status 404")
        available = SimpleNamespace(
            status_code=200,
            text="pytest\npython-dateutil\n",
        )
        icore_env_utils.get_requirements_by_commit.cache_clear()
        with mock.patch.object(
            icore_env_utils,
            "raw_get",
            side_effect=[missing, available],
        ) as raw_get:
            requirements = icore_env_utils.get_requirements_by_commit(
                "matplotlib/matplotlib", "old-matplotlib-commit"
            )
        self.assertEqual(raw_get.call_count, 2)
        self.assertIn("pytest", requirements)
        self.assertIn("python-dateutil", requirements)

    def test_django_python35_requirements_use_conda_libffi_headers(self) -> None:
        spec = SimpleNamespace(
            install={"python": "3.5", "packages": "requirements.txt"},
            repo="django/django",
            environment_setup_commit="abc123",
            env_name="setup_django_django__1.11",
        )
        with mock.patch.object(
            icore_exec_spec,
            "get_requirements_by_commit",
            return_value="argon2-cffi >= 16.1.0\n",
        ):
            commands = icore_exec_spec.ExecSpec.req_install_commands.fget(spec)
        rendered = "\n".join(commands)
        self.assertIn('CFLAGS="-I$CONDA_PREFIX/include ${CFLAGS:-}"', rendered)
        self.assertIn('LDFLAGS="-L$CONDA_PREFIX/lib ${LDFLAGS:-}"', rendered)

    def test_pylint_30_removes_stale_setuptools_startup_hook(self) -> None:
        spec = icore_exec_spec.ExecSpec(
            instance_id="pylint-dev__pylint-8898",
            repo="pylint-dev/pylint",
            version="3.0",
            environment_setup_commit="abc123",
            patch_list=[],
            arch="x86_64",
            base_commit="def456",
            test_directives=[],
            coverage_files=[],
            env_name="setup_pylint-dev_pylint__3.0",
        )
        with mock.patch.object(
            icore_exec_spec,
            "get_requirements_by_commit",
            return_value="setuptools==41.6.0\n",
        ):
            rendered = "\n".join(spec.env_script_list)
        self.assertIn("distutils-precedence.pth", rendered)
        self.assertIn("-delete", rendered)

    def test_legacy_shared_requirements_path_is_rewritten_before_execution(self) -> None:
        script = """cat <<'EOF' > $HOME/requirements.txt
pytest==7.4.0
EOF
python -m pip install -r $HOME/requirements.txt
rm $HOME/requirements.txt
"""
        rendered, path = icore_runtime._isolate_environment_script_files(
            script, str(self.root), "f" * 64
        )
        self.assertEqual(
            path, str(self.root / ".brt-env" / "requirements-ffffffffffffffff.txt")
        )
        self.assertNotIn("$HOME/requirements.txt", rendered)
        self.assertIn(path, rendered)
        self.assertIn("rm -f", rendered)

    def test_dependency_install_spec_ignores_pip_options(self) -> None:
        script = """cat <<'EOF_123' > $HOME/requirements.txt
--prefer-binary
numpy==1.25.2
EOF_123
python -m pip install -r $HOME/requirements.txt
"""
        spec = envm.dependency_install_spec(
            {"python": "3.8", "packages": "requirements.txt"}, script
        )
        self.assertEqual(spec["pip_packages"], ["numpy==1.25.2"])

    def test_explicit_pip_pin_overrides_requirements_file_pin(self) -> None:
        script = """cat <<'EOF_123' > $HOME/requirements.txt
markupsafe==2.1.2
click==8.1.3
EOF_123
python -m pip install -r $HOME/requirements.txt
"""
        spec = envm.dependency_install_spec(
            {
                "python": "3.11",
                "packages": "requirements.txt",
                "pip_packages": ["MarkupSafe==2.1.1"],
            },
            script,
        )
        self.assertEqual(
            spec["pip_packages"], ["click==8.1.3", "MarkupSafe==2.1.1"]
        )

    def test_dependency_install_spec_keeps_direct_package_contract(self) -> None:
        spec = envm.dependency_install_spec(
            {"python": "3.9", "packages": "pytest"}, ""
        )
        self.assertEqual(spec["pip_packages"], ["pytest"])

    def test_dependency_inventory_script_has_python36_fallback(self) -> None:
        proc = mock.Mock(returncode=1, stdout="", stderr="expected")
        with mock.patch.object(
            envm, "conda_env_inventory", return_value={"legacy": "/envs/legacy"}
        ), mock.patch.object(envm.subprocess, "run", return_value=proc) as run:
            envm.dependency_env_compatibility("legacy", {"pip_packages": []})
        script = run.call_args.args[0][-1]
        self.assertIn("except ImportError", script)
        self.assertIn("pkg_resources.working_set", script)

    def test_project_install_does_not_mutate_shared_dependency_versions(self) -> None:
        spec = SimpleNamespace(
            repo="mwaskom/seaborn",
            version="0.12",
            install={"pre_install": [], "install": "python -m pip install -e .[dev]", "eval_commands": []},
        )
        command = icore_setup_command(spec)
        self.assertIn("python -m pip install --no-deps -e .[dev]", command)

    def test_sphinx_runtime_installs_declared_test_dependencies(self) -> None:
        spec = SimpleNamespace(
            repo="sphinx-doc/sphinx",
            version="5.1",
            install={
                "pre_install": [],
                "install": 'python -m pip install -e ."[test]"',
                "eval_commands": [],
            },
        )

        command = icore_setup_command(spec)

        self.assertIn('python -m pip install -e ."[test]"', command)
        self.assertNotIn('--no-deps -e ."[test]"', command)
        self.assertIn("conda install -y -c conda-forge graphviz", command)
        self.assertIn("fi && python -m pip install", command)

    def test_legacy_matplotlib_runtime_dependencies_are_explicit(self) -> None:
        for version in ("3.0", "3.1"):
            packages = MAP_VERSION_TO_INSTALL_MATPLOTLIB[version]["pip_packages"]
            self.assertIn("kiwisolver==1.4.5", packages)
            self.assertIn("pyparsing==2.4.7", packages)
            self.assertTrue(any(item.startswith("numpy==") for item in packages))

    def test_isolated_clone_retries_after_cleaning_partial_prefix(self) -> None:
        failed = {"returncode": 124, "timeout": True}
        ready = {"returncode": 0, "timeout": False}
        with mock.patch.object(
            icore_runtime,
            "_run_script",
            side_effect=[failed, ready],
        ) as run, mock.patch.object(
            icore_runtime,
            "_remove_environment",
            return_value={"returncode": 0},
        ) as remove, mock.patch.object(
            icore_runtime,
            "_scrub_cloned_editable_installs",
            return_value={"returncode": 0},
        ):
            result = icore_runtime._clone_validated_dependency_environment(
                "template", "target", str(self.root), 120
            )

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(len(result["clone_attempts"]), 2)
        self.assertEqual(run.call_args_list[0].args[2], 1800)
        self.assertEqual(run.call_args_list[1].args[2], 3600)
        remove.assert_called_once()

    def test_isolated_runtime_name_changes_by_workspace_and_purpose(self) -> None:
        first = icore_runtime.isolated_runtime_env_name(
            "template", "django__django-1", str(self.root / "one"), "generation"
        )
        second = icore_runtime.isolated_runtime_env_name(
            "template", "django__django-1", str(self.root / "two"), "generation"
        )
        evaluation = icore_runtime.isolated_runtime_env_name(
            "template", "django__django-1", str(self.root / "one"), "formal_eval"
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, evaluation)
        self.assertTrue(first.startswith("brt5i_generation_"))

    def test_isolated_runtime_environment_clones_validated_template(self) -> None:
        template_manifest = {"ok": True, "fingerprint": "template-fingerprint"}
        runtime_manifest = {"ok": True, "fingerprint": "runtime-fingerprint"}
        with mock.patch.object(
            icore_runtime,
            "environment_manifest",
            side_effect=[template_manifest, runtime_manifest],
        ), mock.patch.object(
            icore_runtime,
            "conda_env_inventory",
            return_value={"template": "/envs/template"},
        ), mock.patch.object(
            icore_runtime,
            "_clone_validated_dependency_environment",
            return_value={"returncode": 0},
        ) as clone, mock.patch.object(
            icore_runtime,
            "env_health_check",
            return_value={"ok": True},
        ), mock.patch.object(
            icore_runtime,
            "write_environment_identity",
        ) as write_identity, mock.patch.object(
            icore_runtime,
            "invalidate_environment_runtime_cache",
        ), mock.patch.object(
            icore_runtime,
            "environment_lock_path",
            return_value=self.root / "isolated.lock",
        ):
            result = icore_runtime.ensure_isolated_runtime_environment(
                "template",
                "django__django-1",
                str(self.root),
                120,
                "generation",
            )
        self.assertEqual(result["returncode"], 0)
        self.assertNotEqual(result["env_name"], "template")
        clone.assert_called_once_with(
            "template",
            result["env_name"],
            str(self.root),
            120,
            project_distribution="",
        )
        self.assertEqual(
            write_identity.call_args.args[1]["kind"], "isolated_runtime"
        )

    def test_existing_template_is_repaired_before_reuse(self) -> None:
        row = issue()
        env_name = envm.default_env_name(row, prefix="")
        spec = SimpleNamespace(
            instance_id=row["instance_id"],
            repo=row["repo"],
            version=row["version"],
            base_commit=row["base_commit"],
            environment_setup_commit=row["environment_setup_commit"],
            env_script="create env",
            install={"python": "3.10", "pip_packages": ["numpy==1.23.0"]},
        )
        incompatible = {
            "ok": False,
            "python_ok": True,
            "requirement_checks": [
                {
                    "requirement": "numpy==1.23.0",
                    "name": "numpy",
                    "applicable": True,
                    "ok": False,
                    "reason": "duplicate_metadata",
                }
            ],
        }
        compatible = {
            "ok": True,
            "python_ok": True,
            "requirement_checks": [],
        }
        with mock.patch.object(
            icore_runtime,
            "environment_lock_path",
            return_value=self.root / "prepare.lock",
        ), mock.patch.object(
            icore_runtime,
            "conda_env_inventory",
            return_value={env_name: "/env"},
        ), mock.patch.object(
            icore_runtime,
            "conda_env_exists",
            return_value=True,
        ), mock.patch.object(
            icore_runtime,
            "dependency_env_compatibility",
            return_value=incompatible,
        ), mock.patch.object(
            icore_runtime,
            "_restore_runtime_dependency_contract",
            return_value={"status": "RESTORED", "returncode": 0, "after": compatible},
        ) as repair, mock.patch.object(
            icore_runtime,
            "read_environment_state",
            return_value={},
        ), mock.patch.object(
            icore_runtime,
            "read_environment_identity",
            return_value={},
        ), mock.patch.object(
            icore_runtime,
            "has_recoverable_environment_state",
            return_value=False,
        ), mock.patch.object(
            icore_runtime,
            "env_health_check",
            return_value={"ok": True, "version": "3.10.14"},
        ), mock.patch.object(
            icore_runtime,
            "write_environment_state",
        ), mock.patch.object(
            icore_runtime,
            "_remove_environment",
        ) as remove:
            result = icore_runtime.ensure_icore_environment(
                spec, env_name, str(self.root), 120
            )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["dependency_repair"]["status"], "RESTORED")
        repair.assert_called_once()
        remove.assert_not_called()

    def test_runtime_clone_restores_only_drifted_dependency_pins(self) -> None:
        before = {
            "ok": False,
            "python_ok": True,
            "requirement_checks": [
                {
                    "requirement": "setuptools==68.0.0",
                    "name": "setuptools",
                    "applicable": True,
                    "ok": False,
                    "reason": "version_mismatch",
                },
                {
                    "requirement": "numpy==1.23.5",
                    "name": "numpy",
                    "applicable": True,
                    "ok": True,
                    "reason": "compatible",
                },
            ],
        }
        after = {"ok": True, "python_ok": True, "requirement_checks": []}
        with mock.patch.object(
            icore_runtime,
            "dependency_env_compatibility",
            side_effect=[before, after],
        ), mock.patch.object(
            icore_runtime,
            "_run_script",
            return_value={"returncode": 0, "stdout": "", "stderr": ""},
        ) as run, mock.patch.object(
            icore_runtime,
            "invalidate_environment_runtime_cache",
        ):
            result = icore_runtime._restore_runtime_dependency_contract(
                "runtime", {"python": "3.9", "pip_packages": []}, str(self.root), 120
            )
        self.assertEqual(result["status"], "RESTORED")
        self.assertEqual(result["requirements"], ["setuptools==68.0.0"])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            sum("pip uninstall -y setuptools" in command for command in commands),
            3,
        )
        self.assertIn("setuptools==68.0.0", commands[-1])
        self.assertNotIn("numpy==1.23.5", commands[-1])

    def test_runtime_contract_repair_reinstalls_abi_pair(self) -> None:
        before = {
            "ok": False,
            "python_ok": True,
            "requirement_checks": [
                {
                    "requirement": "numpy==1.23.0",
                    "name": "numpy",
                    "applicable": True,
                    "ok": True,
                    "reason": "compatible",
                },
                {
                    "requirement": "pandas==1.5.3",
                    "name": "pandas",
                    "applicable": True,
                    "ok": False,
                    "reason": "runtime_import_error",
                },
            ],
        }
        after = {"ok": True, "python_ok": True, "requirement_checks": []}
        with mock.patch.object(
            icore_runtime,
            "dependency_env_compatibility",
            side_effect=[before, after],
        ), mock.patch.object(
            icore_runtime,
            "_run_script",
            return_value={"returncode": 0, "stdout": "", "stderr": ""},
        ) as run, mock.patch.object(
            icore_runtime,
            "invalidate_environment_runtime_cache",
        ):
            result = icore_runtime._restore_runtime_dependency_contract(
                "runtime", {"python": "3.10"}, str(self.root), 120
            )
        self.assertEqual(result["status"], "RESTORED")
        self.assertEqual(
            result["requirements"], ["numpy==1.23.0", "pandas==1.5.3"]
        )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("--no-cache-dir --force-reinstall --no-deps", commands[-1])
        self.assertIn("numpy==1.23.0", commands[-1])
        self.assertIn("pandas==1.5.3", commands[-1])

    def test_project_health_check_imports_abi_sensitive_dependencies(self) -> None:
        proc = SimpleNamespace(returncode=0, stdout='{"module":"xarray"}\n', stderr="")
        with mock.patch.object(envm.subprocess, "run", return_value=proc) as run:
            result = envm.project_health_check(
                "runtime", "pydata/xarray", str(self.root), 90
            )
        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertIn("numpy", command)
        self.assertIn("pandas", command)
        self.assertIn("xarray", command)

    def test_legacy_matplotlib_uses_warning_compatible_setuptools(self) -> None:
        spec = SimpleNamespace(
            repo="matplotlib/matplotlib",
            version="3.3",
            install=MAP_VERSION_TO_INSTALL_MATPLOTLIB["3.3"],
        )
        command = icore_setup_command(spec)
        self.assertIn("setuptools==65.5.1", command)
        self.assertNotIn("setuptools==75.1.0", command)
        self.assertIn("setuptools-scm==7.1.0", spec.install["pip_packages"])
        self.assertIn(
            "setuptools-scm-git-archive==1.4.1",
            spec.install["pip_packages"],
        )
        self.assertIn("freetype2/ft2build.h", command)
        self.assertIn("conda install -y -c conda-forge freetype pkg-config", command)
        self.assertIn("fi && python setup.py build_ext --inplace", command)

    def test_runtime_cleanup_never_removes_dependency_template(self) -> None:
        with mock.patch.object(
            icore_runtime,
            "read_environment_identity",
            return_value={"kind": "dependency_template"},
        ), mock.patch.object(
            icore_runtime,
            "_remove_environment",
        ) as remove:
            result = icore_runtime.remove_isolated_runtime_environment(
                "setup_django_django__3.1", str(self.root), 120
            )
        self.assertEqual(result["status"], "SKIPPED_NOT_ISOLATED_RUNTIME")
        remove.assert_not_called()

    def test_pep621_runtime_dependencies_are_installed_before_no_deps_project(self) -> None:
        (self.root / "pyproject.toml").write_text(
            '[project]\nname="demo"\nversion="1"\ndependencies=["dill>=0.2", "tomli; python_version < \'3.11\'"]\n',
            encoding="utf-8",
        )
        spec = SimpleNamespace(
            repo="pylint-dev/pylint",
            version="2.15",
            install={"pre_install": [], "install": "python -m pip install -e .", "eval_commands": []},
        )
        command = icore_setup_command(spec, str(self.root))
        self.assertIn("dill>=0.2", command)
        self.assertLess(command.index("dill>=0.2"), command.index("--no-deps -e ."))

    def test_setup_cfg_runtime_dependencies_precede_no_deps_project(self) -> None:
        (self.root / "setup.cfg").write_text(
            """[options]
install_requires =
    astroid>=2.11.6,<=2.12.0-dev0
    dill>=0.2
""",
            encoding="utf-8",
        )
        spec = SimpleNamespace(
            repo="pylint-dev/pylint",
            version="2.15",
            install={"pre_install": [], "install": "python -m pip install -e .", "eval_commands": []},
        )
        command = icore_setup_command(spec, str(self.root))
        self.assertIn("astroid>=2.11.6,<=2.12.0-dev0", command)
        self.assertLess(command.index("astroid>="), command.index("--no-deps -e ."))

    def test_astropy_13_uses_legacy_editable_install(self) -> None:
        spec = SimpleNamespace(
            repo="astropy/astropy",
            version="1.3",
            install={
                "pre_install": [],
                "install": "python -m pip install --no-build-isolation -e .[test]",
                "eval_commands": [],
            },
        )
        command = icore_setup_command(spec)
        self.assertIn("python setup.py develop --no-deps", command)
        self.assertNotIn(" -e .[test]", command)
        self.assertNotIn("numpy==", command)

    def test_astropy_13_legacy_build_versions_are_in_dependency_contract(self) -> None:
        requirements = MAP_VERSION_TO_INSTALL_ASTROPY["1.3"]["pip_packages"]
        self.assertIn("numpy==1.23.5", requirements)
        self.assertIn("cython<3", requirements)
        self.assertNotIn("numpy==1.25.2", requirements)

    def test_astropy_13_upper_bound_is_shell_quoted(self) -> None:
        spec = icore_exec_spec.make_exec_spec(
            {
                "instance_id": "astropy__astropy-6938",
                "repo": "astropy/astropy",
                "version": "1.3",
                "base_commit": "base123",
                "environment_setup_commit": "setup123",
                "test_patch": "",
            }
        )
        command = next(
            line
            for line in spec.env_script_list
            if line.startswith("python -m pip install attrs==")
        )
        self.assertIn("'cython<3'", command)
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        self.assertIn("cython<3", tokens)
        self.assertNotIn("<", tokens)

    def test_environment_yml_python_is_pinned_before_create(self) -> None:
        source = """name: original
channels:
  - conda-forge
dependencies:
  - numpy
  - pip:
    - pytest
"""
        spec = SimpleNamespace(
            install={"python": "3.11", "packages": "environment.yml"},
            repo="matplotlib/matplotlib",
            environment_setup_commit="abc123",
            env_name="setup_matplotlib",
        )
        with mock.patch.object(
            icore_exec_spec,
            "get_environment_yml_by_commit",
            return_value=source,
        ):
            commands = icore_exec_spec.ExecSpec.req_install_commands.fget(spec)
        rendered = "\n".join(commands)
        self.assertIn("  - python=3.11", rendered)
        self.assertIn("conda env create --file environment.yml", rendered)
        self.assertNotIn("conda install python=3.11", rendered)

    def test_environment_yml_existing_python_pin_is_replaced(self) -> None:
        source = """name: original
dependencies:
  - python=3.14
  - numpy
"""
        rendered = icore_exec_spec._pin_python_in_environment_yml(source, "3.11")
        self.assertIn("  - python=3.11", rendered)
        self.assertNotIn("python=3.14", rendered)

    def test_matplotlib_setup_avoids_scm_git_describe_and_build_isolation(self) -> None:
        spec = SimpleNamespace(
            repo="matplotlib/matplotlib",
            version="3.5",
            install={"pre_install": [], "install": "python -m pip install -e .", "eval_commands": []},
        )
        command = icore_setup_command(spec)
        self.assertIn("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MATPLOTLIB", command)
        self.assertIn("-fno-lto", command)
        self.assertIn("pip install --no-deps --no-build-isolation -e .", command)

    def test_formal_matplotlib_setup_avoids_scm_git_describe(self) -> None:
        command = formal_setup_command("matplotlib/matplotlib", "3.5")
        self.assertIn("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MATPLOTLIB", command)
        self.assertIn("-fno-lto", command)
        self.assertIn("--no-build-isolation --no-deps -e .", command)

    def test_formal_astropy_13_uses_legacy_editable_install(self) -> None:
        command = formal_setup_command("astropy/astropy", "1.3")
        self.assertEqual(command, "python setup.py develop --no-deps")

    def test_formal_modern_astropy_disables_build_isolation(self) -> None:
        command = formal_setup_command("astropy/astropy", "5.1")
        self.assertIn("--no-build-isolation", command)
        self.assertIn("--no-deps -e .\"[test]\"", command)

    def test_icore_env_name_is_keyed_by_dependency_setup_commit(self) -> None:
        first = icore_runtime.env_name_for(
            "astropy/astropy", "5.1", "base-a", "setup-a"
        )
        different_base = icore_runtime.env_name_for(
            "astropy/astropy", "5.1", "base-b", "setup-a"
        )
        different_setup = icore_runtime.env_name_for(
            "astropy/astropy", "5.1", "base-a", "setup-b"
        )
        self.assertEqual(first, different_base)
        self.assertNotEqual(first, different_setup)

    def test_formal_scikit_learn_13_disables_build_isolation(self) -> None:
        command = formal_setup_command("scikit-learn/scikit-learn", "1.3")
        self.assertIn("--no-build-isolation", command)
        self.assertIn("-e .", command)

    def test_formal_pylint_215_preserves_template_astroid_contract(self) -> None:
        command = formal_setup_command("pylint-dev/pylint", "2.15")
        self.assertNotIn("astroid==", command)
        self.assertIn("--no-deps -e .", command)

    def test_editable_project_cache_is_workspace_specific(self) -> None:
        state = {
            "status": "ready",
            "editable_install": True,
            "prepared_source_path": str(self.root / "first"),
        }
        health = {"ok": True}
        self.assertTrue(
            envm.project_cache_ready(state, str(self.root / "first"), health)
        )
        self.assertFalse(
            envm.project_cache_ready(state, str(self.root / "second"), health)
        )

    def test_noneditable_project_cache_can_cross_clean_workspaces(self) -> None:
        state = {
            "status": "ready",
            "editable_install": False,
            "prepared_source_path": str(self.root / "first"),
        }
        self.assertTrue(
            envm.project_cache_ready(
                state, str(self.root / "second"), {"ok": True}
            )
        )

    def test_legacy_project_cache_without_source_is_not_trusted(self) -> None:
        self.assertFalse(
            envm.project_cache_ready(
                {"status": "ready"}, str(self.root), {"ok": True}
            )
        )

    def test_formal_only_uses_generation_metadata(self) -> None:
        self.test_new_run_uses_recorded_run_prefix_env()

    def test_completed_only_uses_generation_metadata(self) -> None:
        self.test_old_run_uses_recorded_unprefixed_env()

    def test_resume_generation_keeps_recorded_identity(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {"env_name": "env_resume"}, {"repo_prepare": {"env_name": "env_other"}})
        records = envm.extract_env_records(iid, str(self.root))
        self.assertEqual(records[0]["env_name"], "env_resume")

    def test_env_name_sanitizes_special_chars_and_long_prefix(self) -> None:
        name = envm.default_env_name({"repo": "owner weird/name weird", "version": "3.1 / x"}, prefix="run " * 50)
        self.assertLessEqual(len(name), 260)
        self.assertNotRegex(name, r"\\s|/")

    def test_cache_paths_do_not_collapse_distinct_environment_names(self) -> None:
        first = envm.environment_identity_path("setup_owner_repo__1.0")
        second = envm.environment_identity_path("setup_owner_repo_1.0")
        self.assertNotEqual(first, second)

    def test_preflight_disk_space_failure(self) -> None:
        usage = mock.Mock(total=100, used=99, free=1)
        stat = mock.Mock(f_favail=1)
        with mock.patch.object(envm.shutil, "disk_usage", return_value=usage), mock.patch.object(envm.os, "statvfs", return_value=stat), mock.patch.object(envm.Path, "exists", return_value=True):
            data = envm.preflight_system([str(self.root)], min_free_gb=1, min_free_inodes=10)
        self.assertFalse(data["ok"])

    def test_preflight_uses_configurable_conda_probe_timeout(self) -> None:
        usage = mock.Mock(total=20 * 1024**3, used=1, free=10 * 1024**3)
        stat = mock.Mock(f_favail=100_000)
        completed = SimpleNamespace(
            returncode=0,
            stdout="conda 25.9.1\n",
            stderr="",
        )
        with mock.patch.dict(
            envm.os.environ,
            {"BRT_CONDA_PROBE_TIMEOUT_SECONDS": "180"},
        ), mock.patch.object(
            envm.shutil, "disk_usage", return_value=usage
        ), mock.patch.object(
            envm.os, "statvfs", return_value=stat
        ), mock.patch.object(
            envm.Path, "exists", return_value=True
        ), mock.patch.object(
            envm.subprocess, "run", return_value=completed
        ) as run:
            data = envm.preflight_system([str(self.root)])

        self.assertTrue(data["ok"])
        self.assertEqual(data["conda_probe_timeout_seconds"], 180.0)
        self.assertEqual(run.call_args.kwargs["timeout"], 180.0)

    def test_conda_nonzero_health_is_incomplete(self) -> None:
        with self.patch_envs({"env_a": "/envs/a"}, unhealthy={"env_a"}):
            health = envm.env_health_check("env_a")
        self.assertFalse(health["ok"])
        self.assertEqual(health["category"], "ENV_INCOMPLETE")

    def test_inventory_ignores_environment_registered_by_other_conda_prefix(self) -> None:
        env_list = {
            "envs": [
                "/root/miniconda3/envs/stale",
                "/root/brt5-conda/envs/current",
                "/root/.conda/envs/user-env",
            ]
        }
        info = {
            "root_prefix": "/root/brt5-conda",
            "envs_dirs": [
                "/root/brt5-conda/envs",
                "/root/.conda/envs",
            ],
        }
        responses = [
            SimpleNamespace(returncode=0, stdout=json.dumps(env_list), stderr=""),
            SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr=""),
        ]
        with mock.patch.object(envm.subprocess, "run", side_effect=responses):
            inventory = envm.conda_env_inventory(refresh=True)
        self.assertEqual(
            inventory,
            {
                "current": "/root/brt5-conda/envs/current",
                "user-env": "/root/.conda/envs/user-env",
            },
        )

    def test_inventory_prefers_active_prefix_for_duplicate_environment_name(self) -> None:
        env_list = {
            "envs": [
                "/root/brt5-conda/envs/same-name",
                "/root/miniconda3/envs/same-name",
            ]
        }
        info = {
            "root_prefix": "/root/brt5-conda",
            "envs_dirs": [
                "/root/brt5-conda/envs",
                "/root/.conda/envs",
            ],
        }
        responses = [
            SimpleNamespace(returncode=0, stdout=json.dumps(env_list), stderr=""),
            SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr=""),
        ]
        with mock.patch.object(envm.subprocess, "run", side_effect=responses):
            inventory = envm.conda_env_inventory(refresh=True)
        self.assertEqual(
            inventory,
            {"same-name": "/root/brt5-conda/envs/same-name"},
        )

    def test_health_check_uses_inventory_prefix_instead_of_global_name(self) -> None:
        response = SimpleNamespace(
            returncode=0,
            stdout='{"executable":"/active/env/bin/python","version":"3.9.0","purelib":"/active/env/lib"}\n',
            stderr="",
        )
        with mock.patch.object(
            envm,
            "conda_env_inventory",
            return_value={"same-name": "/active/env"},
        ), mock.patch.object(envm.subprocess, "run", return_value=response) as run:
            health = envm.env_health_check("same-name", refresh=True)
        self.assertTrue(health["ok"])
        self.assertEqual(run.call_args.args[0][2:4], ["-p", "/active/env"])

    def test_no_suffix_fallback_to_other_run_env(self) -> None:
        iid = "django__django-12184"
        write_instance(self.root, iid, {})
        with self.patch_envs({"other_run_setup_django_django__3.1": "/envs/other"}):
            res = envm.resolve_eval_env(issue(iid), str(self.root), run_prefix="run_x_")
        self.assertFalse(res.env_exists)
        self.assertNotEqual(res.resolved_env, "other_run_setup_django_django__3.1")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brt6.core.schema import ExecutionResult, InstanceContext
from brt6.execution import executor, feedback
from brt6.pipeline import run as pipeline_run
from brt6.runtime.official_docker_runtime import (
    OFFICIAL_RUNTIME_BACKEND,
    OfficialDockerRuntime,
    docker_storage_preflight,
    safe_runtime_request,
)
from brt6.runtime.official_container_registry import (
    ContainerRegistryError,
    OfficialContainerRegistry,
)
from brt6.scripts.official_generation_container import (
    _normalize_swt_repo_commands,
    _remove_container_by_name,
)


def issue_row() -> dict:
    return {
        "instance_id": "astropy__astropy-12907",
        "repo": "astropy/astropy",
        "version": "4.3",
        "base_commit": "base123",
        "environment_setup_commit": "setup456",
        "patch": "SECRET CODE PATCH",
        "test_patch": "SECRET TEST PATCH",
        "golden_code_patch": "SECRET GOLD CODE",
        "golden_test_patch": "SECRET GOLD TEST",
        "FAIL_TO_PASS": "SECRET F2P",
        "PASS_TO_PASS": "SECRET P2P",
    }


class OfficialRuntimeContractTests(unittest.TestCase):
    def test_registry_reserves_the_exact_official_name(self) -> None:
        class NotFound(Exception):
            pass

        client = mock.Mock()
        client.containers.get.side_effect = NotFound()
        client.containers.list.return_value = []
        image = "exec.eval.x86_64.environment.instance:latest"
        name = "exec.eval.x86_64.environment.instance.12345"
        with tempfile.TemporaryDirectory() as tmp:
            registry = OfficialContainerRegistry(Path(tmp) / "registry.json")
            resolution = registry.resolve(
                client,
                instance_id="owner__repo-1",
                expected_image=image,
                preferred_name=name,
            )
            stored = json.loads(registry.path.read_text(encoding="utf-8"))

        self.assertEqual(resolution.name, name)
        self.assertFalse(resolution.reused)
        self.assertEqual(
            stored["instances"]["owner__repo-1"],
            {"container_name": name, "image": image},
        )

    def test_registry_adopts_one_compatible_official_container(self) -> None:
        class NotFound(Exception):
            pass

        image = "exec.eval.x86_64.environment.instance:latest"
        container = mock.Mock()
        container.name = "exec.eval.x86_64.environment.instance.777"
        container.attrs = {
            "Config": {"Image": image},
            "State": {"Status": "running", "Dead": False},
        }
        client = mock.Mock()
        client.containers.get.side_effect = NotFound()
        client.containers.list.return_value = [container]
        with tempfile.TemporaryDirectory() as tmp:
            resolution = OfficialContainerRegistry(
                Path(tmp) / "registry.json"
            ).resolve(
                client,
                instance_id="owner__repo-1",
                expected_image=image,
                preferred_name="exec.eval.x86_64.environment.instance.12345",
            )

        self.assertIs(resolution.container, container)
        self.assertTrue(resolution.reused)
        self.assertTrue(resolution.adopted)

    def test_registry_rejects_ambiguous_official_containers(self) -> None:
        image = "exec.eval.x86_64.environment.instance:latest"
        containers = []
        for suffix in ("111", "222"):
            container = mock.Mock()
            container.name = f"exec.eval.x86_64.environment.instance.{suffix}"
            container.attrs = {
                "Config": {"Image": image},
                "State": {"Status": "running", "Dead": False},
            }
            containers.append(container)
        client = mock.Mock()
        client.containers.list.return_value = containers
        with tempfile.TemporaryDirectory() as tmp:
            registry = OfficialContainerRegistry(Path(tmp) / "registry.json")
            with self.assertRaisesRegex(
                ContainerRegistryError, "multiple compatible official containers"
            ):
                registry.resolve(
                    client,
                    instance_id="owner__repo-1",
                    expected_image=image,
                    preferred_name="exec.eval.x86_64.environment.instance.12345",
                )

    def test_official_setup_normalization_is_narrow_and_auditable(self) -> None:
        sklearn, sklearn_changes = _normalize_swt_repo_commands(
            {"repo": "scikit-learn/scikit-learn"},
            [
                "python -m pip install -v --no-use-pep517 "
                "--no-build-isolation -e ."
            ],
        )
        self.assertEqual(
            sklearn,
            [
                "python -m pip install -v --no-use-pep517 "
                "--no-build-isolation -e ."
            ],
        )
        self.assertEqual(sklearn_changes, [])

        pylint, pylint_changes = _normalize_swt_repo_commands(
            {"repo": "pylint-dev/pylint"},
            ["python -m pip install -e ."],
        )
        self.assertIn("'pip==21.2.4'", pylint[0])
        self.assertTrue(pylint[0].endswith("python -m pip install -e ."))
        self.assertEqual(len(pylint_changes), 1)

        ordinary, ordinary_changes = _normalize_swt_repo_commands(
            {"repo": "django/django"}, ["python setup.py install"]
        )
        self.assertEqual(ordinary, ["python setup.py install"])
        self.assertEqual(ordinary_changes, [])

    def test_official_apt_commands_gain_transport_retries_only(self) -> None:
        commands, changes = _normalize_swt_repo_commands(
            {"repo": "matplotlib/matplotlib"},
            ["apt-get -y update && apt-get -y upgrade && apt-get install -y texlive"],
        )
        self.assertIn("Acquire::Retries=5", commands[0])
        self.assertEqual(commands[0].count("Acquire::Retries=5"), 3)
        self.assertIn("install -y texlive", commands[0])
        self.assertEqual(len(changes), 1)

    def test_unknown_create_outcome_is_removed_by_exact_name(self) -> None:
        client = mock.Mock()
        container = client.containers.get.return_value
        result = _remove_container_by_name(client, "exec.eval.exact")
        client.containers.get.assert_called_once_with("exec.eval.exact")
        container.remove.assert_called_once_with(force=True)
        self.assertEqual(result["returncode"], 0)

    def test_storage_preflight_records_overlay2_capacity(self) -> None:
        completed = subprocess.CompletedProcess(
            ["docker"], 0, "/root/docker-data\toverlay2\t28.5.1\n", ""
        )
        usage = mock.Mock(total=500 * 1024**3, free=200 * 1024**3)
        with mock.patch(
            "brt6.runtime.official_docker_runtime._run", return_value=completed
        ), mock.patch(
            "brt6.runtime.official_docker_runtime.shutil.disk_usage",
            return_value=usage,
        ), mock.patch.dict(
            "os.environ",
            {"BRT_DOCKER_MIN_FREE_GB": "120", "BRT_REJECT_DOCKER_VFS": "1"},
            clear=False,
        ):
            result = docker_storage_preflight(force_refresh=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["docker_root_dir"], "/root/docker-data")
        self.assertEqual(result["docker_storage_driver"], "overlay2")
        self.assertEqual(result["docker_free_gib"], 200.0)

    def test_storage_preflight_rejects_vfs_and_low_space(self) -> None:
        completed = subprocess.CompletedProcess(
            ["docker"], 0, "/var/lib/docker\tvfs\t28.5.1\n", ""
        )
        usage = mock.Mock(total=500 * 1024**3, free=10 * 1024**3)
        with mock.patch(
            "brt6.runtime.official_docker_runtime._run", return_value=completed
        ), mock.patch(
            "brt6.runtime.official_docker_runtime.shutil.disk_usage",
            return_value=usage,
        ), mock.patch.dict(
            "os.environ",
            {"BRT_DOCKER_MIN_FREE_GB": "120", "BRT_REJECT_DOCKER_VFS": "1"},
            clear=False,
        ):
            result = docker_storage_preflight(force_refresh=True)
        self.assertFalse(result["ok"])
        self.assertTrue(any("below the generation gate" in e for e in result["errors"]))
        self.assertTrue(any("vfs is forbidden" in e for e in result["errors"]))

    def test_storage_preflight_retries_timeout_then_caches_stable_info(self) -> None:
        completed = subprocess.CompletedProcess(
            ["docker"], 0, "/root/docker-data\toverlay2\t28.5.1\n", ""
        )
        usage = mock.Mock(total=500 * 1024**3, free=200 * 1024**3)
        with mock.patch(
            "brt6.runtime.official_docker_runtime._run",
            side_effect=[subprocess.TimeoutExpired(["docker", "info"], 1), completed],
        ) as run, mock.patch(
            "brt6.runtime.official_docker_runtime.shutil.disk_usage",
            return_value=usage,
        ), mock.patch(
            "brt6.runtime.official_docker_runtime.time.sleep"
        ), mock.patch.dict(
            "os.environ",
            {
                "BRT_DOCKER_INFO_ATTEMPTS": "2",
                "BRT_DOCKER_INFO_TIMEOUT_SECONDS": "1",
                "BRT_DOCKER_INFO_BACKOFF_SECONDS": "0",
            },
            clear=False,
        ):
            refreshed = docker_storage_preflight(force_refresh=True)
            cached = docker_storage_preflight()
        self.assertTrue(refreshed["ok"])
        self.assertEqual(refreshed["docker_info_source"], "daemon")
        self.assertEqual(len(refreshed["docker_info_failures"]), 1)
        self.assertEqual(cached["docker_info_source"], "cache")
        self.assertEqual(run.call_count, 2)

    def test_safe_request_drops_every_gold_field(self) -> None:
        request = safe_runtime_request(issue_row())
        self.assertEqual(
            set(request),
            {
                "instance_id",
                "repo",
                "version",
                "base_commit",
                "environment_setup_commit",
            },
        )
        self.assertFalse(any("SECRET" in value for value in request.values()))

    def test_pipeline_accepts_only_official_docker(self) -> None:
        args = argparse.Namespace(
            dataset_mode="tdd",
            runtime_backend=OFFICIAL_RUNTIME_BACKEND,
            no_conda=False,
            official_harness_python="/opt/official/bin/python",
            swtbench_root="/bench/swt",
            tddbench_root="/bench/tdd",
            conda_env="",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            contract = pipeline_run.configure_runtime_contract(args)
        self.assertTrue(contract["docker_harness_invoked"])
        self.assertFalse(contract["host_project_environment_created"])
        self.assertFalse(contract["host_execution_fallback_allowed"])
        self.assertEqual(contract["gold_fields_allowed"], [])
        args.runtime_backend = "local_conda"
        with self.assertRaisesRegex(ValueError, "official_docker"):
            pipeline_run.configure_runtime_contract(args)

    def test_explicit_host_project_environment_is_rejected(self) -> None:
        args = argparse.Namespace(
            dataset_mode="swt",
            runtime_backend=OFFICIAL_RUNTIME_BACKEND,
            no_conda=False,
            conda_env="setup_astropy__astropy_4_3",
            official_harness_python="/opt/official/bin/python",
            swtbench_root="/bench/swt",
            tddbench_root="/bench/tdd",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            with self.assertRaisesRegex(ValueError, "--conda_env is forbidden"):
                pipeline_run.configure_runtime_contract(args)

    def test_executor_dispatches_without_host_conda(self) -> None:
        expected = ExecutionResult(instance_id="i", status="PASS")
        runtime = mock.Mock()
        runtime.execute.return_value = expected
        with mock.patch(
            "brt6.runtime.official_docker_runtime.active_runtime",
            return_value=runtime,
        ), mock.patch.object(
            executor, "run_subprocess_tree"
        ) as host_subprocess:
            actual = executor.run_command_in_conda(
                "python -m pytest test_brt.py",
                "/host/worktree",
                "must_not_activate",
                123,
                False,
                None,
                "i",
            )
        self.assertIs(actual, expected)
        runtime.execute.assert_called_once_with(
            "python -m pytest test_brt.py", "/host/worktree", 123, None
        )
        host_subprocess.assert_not_called()

    def test_required_official_runtime_cannot_fall_back_to_host(self) -> None:
        with mock.patch.dict(
            "os.environ", {"BRT_REQUIRE_OFFICIAL_DOCKER": "1"}, clear=False
        ), mock.patch(
            "brt6.runtime.official_docker_runtime.active_runtime",
            return_value=None,
        ), mock.patch.object(
            executor, "run_subprocess_tree"
        ) as host_subprocess:
            with self.assertRaisesRegex(RuntimeError, "no instance runtime"):
                executor.run_command_in_conda(
                    "python -m pytest", "/host", "forbidden", 60, False, None, "i"
                )
        host_subprocess.assert_not_called()

    def test_worktree_preparation_skips_all_environment_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            output = Path(tmp) / "output"
            runtime = mock.Mock()
            runtime.manifest_path = output / "official_generation_runtime.json"
            context = InstanceContext(
                instance_id="i",
                issue_text="issue",
                repo="owner/repo",
                base_commit="base123",
                buggy_repo_path=str(source),
                metadata={"version": "1"},
            )
            with mock.patch.object(
                feedback, "_run_local", return_value={"returncode": 0}
            ), mock.patch(
                "brt6.runtime.official_docker_runtime.active_runtime",
                return_value=runtime,
            ), mock.patch.object(
                feedback, "ensure_icore_environment"
            ) as ensure_env, mock.patch.object(
                feedback, "ensure_isolated_runtime_environment"
            ) as isolate_env, mock.patch.object(
                feedback, "run_command_in_conda"
            ) as execute_setup:
                repo, meta = feedback.prepare_instance_worktree(
                    context, str(output), "forbidden", 120, False
                )
            self.assertEqual(repo, str(output / "worktree"))
            self.assertEqual(meta["runtime_backend"], OFFICIAL_RUNTIME_BACKEND)
            self.assertFalse(meta["host_project_environment_created"])
            ensure_env.assert_not_called()
            isolate_env.assert_not_called()
            execute_setup.assert_not_called()

    def test_container_command_uses_official_testbed_and_maps_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = OfficialDockerRuntime(
                dataset_mode="swt",
                issue_row=issue_row(),
                official_python="/opt/official/bin/python",
                harness_root="/bench/swt",
                source_repo=tmp,
                output_dir=tmp,
                startup_timeout=1800,
            )
            runtime.container_id = "container123"
            runtime.repo_directory = "/testbed"
            runtime.env_name = "testbed"
            completed = __import__("subprocess").CompletedProcess(
                ["docker"], 0, "1 passed\n", ""
            )
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return completed

            with mock.patch(
                "brt6.runtime.official_docker_runtime._run", side_effect=fake_run
            ), mock.patch.object(
                runtime, "_host_delta", return_value=([], [])
            ):
                result = runtime.execute(
                    f"python -m pytest {tmp}/test_brt.py",
                    tmp,
                    60,
                    None,
                )
            self.assertEqual(result.status, "PASS")
            joined = "\n".join(" ".join(call) for call in calls)
            self.assertIn("git -C /testbed reset --hard base123", joined)
            self.assertIn("conda activate testbed", joined)
            self.assertIn("python -m pytest /testbed/test_brt.py", joined)

    def test_close_removes_container_then_exact_instance_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = OfficialDockerRuntime(
                dataset_mode="swt",
                issue_row=issue_row(),
                official_python="/opt/official/bin/python",
                harness_root="/bench/swt",
                source_repo=tmp,
                output_dir=tmp,
                startup_timeout=1800,
            )
            runtime.container_id = "container123"
            runtime.image = "exec.eval.x86_64.example:latest"
            runtime.manifest = {"status": "RUNNING"}
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "removed", "")

            with mock.patch(
                "brt6.runtime.official_docker_runtime._run", side_effect=fake_run
            ):
                runtime.close()
            self.assertEqual(
                calls,
                [
                    ["docker", "rm", "-f", "container123"],
                    [
                        "docker",
                        "image",
                        "rm",
                        "-f",
                        "exec.eval.x86_64.example:latest",
                    ],
                ],
            )
            self.assertEqual(runtime.manifest["status"], "CLEANED")
            self.assertEqual(runtime.manifest["instance_image_cleanup_returncode"], 0)
            self.assertEqual(runtime.container_id, "")
            self.assertEqual(runtime.image, "")

    def test_close_preserves_cached_instance_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = OfficialDockerRuntime(
                dataset_mode="swt",
                issue_row=issue_row(),
                official_python="/opt/official/bin/python",
                harness_root="/bench/swt",
                source_repo=tmp,
                output_dir=tmp,
                startup_timeout=1800,
            )
            runtime.container_id = "container123"
            runtime.image = "exec.eval.x86_64.cached:latest"
            runtime.image_owned = False
            runtime.manifest = {"status": "RUNNING"}
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "removed", "")

            with mock.patch(
                "brt6.runtime.official_docker_runtime._run", side_effect=fake_run
            ):
                runtime.close()

            self.assertEqual(calls, [["docker", "rm", "-f", "container123"]])
            self.assertFalse(runtime.manifest["instance_image_cleanup_attempted"])
            self.assertEqual(
                runtime.manifest["instance_image_cache_policy"],
                "preserve_cached_image",
            )

    def test_close_preserves_persistent_official_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = OfficialDockerRuntime(
                dataset_mode="swt",
                issue_row=issue_row(),
                official_python="/opt/official/bin/python",
                harness_root="/bench/swt",
                source_repo=tmp,
                output_dir=tmp,
                startup_timeout=1800,
            )
            runtime.container_id = "container123"
            runtime.container_name = "exec.eval.x86_64.environment.instance.123"
            runtime.image = "exec.eval.x86_64.environment.instance:latest"
            runtime.image_owned = False
            runtime.container_persistent = True
            runtime.manifest = {"status": "RUNNING"}

            with mock.patch(
                "brt6.runtime.official_docker_runtime._run"
            ) as docker_command:
                runtime.close()

            docker_command.assert_not_called()
            self.assertEqual(runtime.manifest["status"], "PERSISTENT_READY")
            self.assertFalse(runtime.manifest["container_cleanup_attempted"])
            self.assertFalse(runtime.manifest["instance_image_cleanup_attempted"])

    def test_close_waits_for_daemon_then_retries_timed_out_image_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = OfficialDockerRuntime(
                dataset_mode="swt",
                issue_row=issue_row(),
                official_python="/opt/official/bin/python",
                harness_root="/bench/swt",
                source_repo=tmp,
                output_dir=tmp,
                startup_timeout=1800,
            )
            runtime.container_id = "container123"
            runtime.image = "exec.eval.x86_64.example:latest"
            runtime.manifest = {"status": "RUNNING"}
            docker_info = subprocess.CompletedProcess(
                ["docker"], 0, "/root/docker-data\toverlay2\t28.5.1\n", ""
            )
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if (
                    command[:3] == ["docker", "image", "rm"]
                    and calls.count(command) == 1
                ):
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                if command[:2] == ["docker", "info"]:
                    return docker_info
                if command[:3] == ["docker", "image", "rm"]:
                    return subprocess.CompletedProcess(
                        command, 1, "", "Error: No such image"
                    )
                return subprocess.CompletedProcess(command, 0, "removed", "")

            with mock.patch(
                "brt6.runtime.official_docker_runtime._run", side_effect=fake_run
            ), mock.patch.dict(
                "os.environ",
                {
                    "BRT_DOCKER_IMAGE_CLEANUP_TIMEOUT_SECONDS": "120",
                    "BRT_DOCKER_RECOVERY_TIMEOUT_SECONDS": "5",
                    "BRT_DOCKER_RECOVERY_PROBE_SECONDS": "1",
                    "BRT_DOCKER_RECOVERY_BACKOFF_SECONDS": "0",
                },
                clear=False,
            ):
                runtime.close()
            self.assertEqual(runtime.manifest["status"], "CLEANED")
            self.assertEqual(
                runtime.manifest["docker_recovery_after_cleanup"]["status"],
                "READY",
            )
            self.assertEqual(
                len(runtime.manifest["instance_image_cleanup_attempts"]), 2
            )

    def test_failed_startup_cleanup_uses_only_lifecycle_owned_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "instance"
            context = output / ".official_build_context_12_abc"
            context.mkdir(parents=True)
            runtime = OfficialDockerRuntime(
                dataset_mode="swt",
                issue_row=issue_row(),
                official_python="/opt/official/bin/python",
                harness_root="/bench/swt",
                source_repo=tmp,
                output_dir=str(output),
                startup_timeout=1800,
            )
            runtime.lifecycle_path.write_text(
                json.dumps(
                    {
                        "container_names": ["exec.eval.owned.1"],
                        "image": "exec.eval.owned:latest",
                        "context_root": str(context),
                    }
                ),
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch(
                "brt6.runtime.official_docker_runtime._run",
                side_effect=fake_run,
            ):
                cleanup = runtime._cleanup_failed_startup()
            self.assertEqual(
                calls,
                [
                    ["docker", "rm", "-f", "exec.eval.owned.1"],
                    [
                        "docker",
                        "image",
                        "rm",
                        "-f",
                        "exec.eval.owned:latest",
                    ],
                ],
            )
            self.assertFalse(context.exists())
            self.assertEqual(cleanup["status"], "ATTEMPTED")

    def test_failed_startup_cleanup_preserves_unowned_cached_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "instance"
            output.mkdir()
            runtime = OfficialDockerRuntime(
                dataset_mode="swt",
                issue_row=issue_row(),
                official_python="/opt/official/bin/python",
                harness_root="/bench/swt",
                source_repo=tmp,
                output_dir=str(output),
                startup_timeout=1800,
            )
            runtime.lifecycle_path.write_text(
                json.dumps(
                    {
                        "container_names": ["exec.eval.cached.1"],
                        "image": "exec.eval.x86_64.cached:latest",
                        "image_owned": False,
                    }
                ),
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch(
                "brt6.runtime.official_docker_runtime._run",
                side_effect=fake_run,
            ):
                runtime._cleanup_failed_startup()

            self.assertEqual(calls, [["docker", "rm", "-f", "exec.eval.cached.1"]])


if __name__ == "__main__":
    unittest.main()

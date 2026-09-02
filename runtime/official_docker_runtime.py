"""Official benchmark Docker runtime for generation-time feedback.

The controller stays in BRT6, while every dynamic test command runs against
the official harness's buggy/base-commit instance image.  This module has no
dependency on the Docker Python package so the BRT framework environment is
not modified; image construction is delegated to the official interpreter.
"""

from __future__ import annotations

import atexit
import io
import json
import math
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..core.behavior_evidence import BehaviorEvidence
from ..core.schema import ExecutionResult
from ..core.utils import safe_json_dump
from .official_container_registry import InstanceLock


OFFICIAL_RUNTIME_BACKEND = "official_docker"
DEFAULT_DOCKER_MIN_FREE_GIB = 120.0
DEFAULT_DOCKER_HOST = "unix:///run/mutate-docker.sock"
_DOCKER_INFO_FORMAT = "{{.DockerRootDir}}\t{{.Driver}}\t{{.ServerVersion}}"
SAFE_RUNTIME_KEYS = (
    "instance_id",
    "repo",
    "version",
    "base_commit",
    "environment_setup_commit",
)
FORBIDDEN_GOLD_KEYS = {
    "patch",
    "test_patch",
    "golden_code_patch",
    "golden_test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
}

_ACTIVE: dict[str, "OfficialDockerRuntime"] = {}
_ACTIVE_LOCK = threading.RLock()
_DOCKER_INFO_CACHE: dict[str, str] = {}
_DOCKER_INFO_LOCK = threading.RLock()


def safe_runtime_request(issue_row: dict[str, Any]) -> dict[str, str]:
    """Reduce a dataset row to the only fields allowed in generation runtime."""

    request = {
        key: str(issue_row.get(key) or "")
        for key in SAFE_RUNTIME_KEYS
    }
    request["environment_setup_commit"] = (
        request["environment_setup_commit"] or request["base_commit"]
    )
    missing = [
        key
        for key in ("instance_id", "repo", "version", "base_commit")
        if not request[key]
    ]
    if missing:
        raise ValueError(f"official Docker runtime metadata missing: {missing}")
    if set(request) & FORBIDDEN_GOLD_KEYS:
        raise AssertionError("gold fields reached the generation runtime request")
    return request


def active_runtime(instance_id: str) -> "OfficialDockerRuntime | None":
    with _ACTIVE_LOCK:
        return _ACTIVE.get(instance_id)


def _run(
    command: list[str],
    *,
    timeout: int,
    cwd: str | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.setdefault("DOCKER_HOST", DEFAULT_DOCKER_HOST)
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        text=input_bytes is None,
        env=environment,
    )


def _env_flag(name: str, default: bool) -> tuple[bool, str]:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True, ""
    if raw in {"0", "false", "no", "off"}:
        return False, ""
    return default, f"{name} must be a boolean, got {raw!r}"


def _docker_info_with_retry() -> tuple[subprocess.CompletedProcess | None, list[str]]:
    try:
        attempts = max(1, int(os.environ.get("BRT_DOCKER_INFO_ATTEMPTS", "5")))
    except ValueError:
        attempts = 5
    try:
        timeout_seconds = max(
            1, int(os.environ.get("BRT_DOCKER_INFO_TIMEOUT_SECONDS", "30"))
        )
    except ValueError:
        timeout_seconds = 30
    try:
        backoff_seconds = max(
            0.0, float(os.environ.get("BRT_DOCKER_INFO_BACKOFF_SECONDS", "5"))
        )
    except ValueError:
        backoff_seconds = 5.0

    failures: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            info = _run(
                ["docker", "info", "--format", _DOCKER_INFO_FORMAT],
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            failures.append(
                f"attempt {attempt}/{attempts}: docker info timed out after "
                f"{timeout_seconds} seconds"
            )
        else:
            if info.returncode == 0:
                return info, failures
            failures.append(
                f"attempt {attempt}/{attempts}: docker info returned "
                f"{info.returncode}: {str(info.stderr or '').strip()[-500:]}"
            )
        if attempt < attempts and backoff_seconds:
            time.sleep(min(backoff_seconds * attempt, 30.0))
    return None, failures


def _cache_docker_info(
    info: subprocess.CompletedProcess,
) -> tuple[str, str, str] | None:
    parts = str(info.stdout or "").strip().split("\t")
    if len(parts) != 3 or not all(parts):
        return None
    docker_root, storage_driver, server_version = parts
    _DOCKER_INFO_CACHE.update(
        {
            "docker_root": docker_root,
            "storage_driver": storage_driver,
            "server_version": server_version,
        }
    )
    return docker_root, storage_driver, server_version


def _wait_for_docker_ready() -> dict[str, Any]:
    try:
        recovery_timeout = max(
            1,
            int(os.environ.get("BRT_DOCKER_RECOVERY_TIMEOUT_SECONDS", "900")),
        )
    except ValueError:
        recovery_timeout = 900
    try:
        probe_timeout = max(
            1, int(os.environ.get("BRT_DOCKER_RECOVERY_PROBE_SECONDS", "30"))
        )
    except ValueError:
        probe_timeout = 30
    try:
        backoff_seconds = max(
            0.0,
            float(os.environ.get("BRT_DOCKER_RECOVERY_BACKOFF_SECONDS", "5")),
        )
    except ValueError:
        backoff_seconds = 5.0

    started = time.monotonic()
    attempts = 0
    failures: list[str] = []
    with _DOCKER_INFO_LOCK:
        _DOCKER_INFO_CACHE.clear()
        while True:
            elapsed = time.monotonic() - started
            remaining = recovery_timeout - elapsed
            if remaining <= 0:
                break
            attempts += 1
            timeout_seconds = max(1, min(probe_timeout, math.ceil(remaining)))
            try:
                info = _run(
                    ["docker", "info", "--format", _DOCKER_INFO_FORMAT],
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                failures.append(
                    f"attempt {attempts}: timed out after {timeout_seconds} seconds"
                )
            else:
                parsed = _cache_docker_info(info) if info.returncode == 0 else None
                if parsed is not None:
                    return {
                        "status": "READY",
                        "attempts": attempts,
                        "elapsed_seconds": time.monotonic() - started,
                        "failures": failures,
                    }
                failures.append(
                    f"attempt {attempts}: returncode={info.returncode} "
                    f"stderr={str(info.stderr or '').strip()[-500:]}"
                )
            remaining = recovery_timeout - (time.monotonic() - started)
            if remaining > 0 and backoff_seconds:
                time.sleep(min(backoff_seconds, remaining))
    return {
        "status": "TIMEOUT",
        "attempts": attempts,
        "elapsed_seconds": time.monotonic() - started,
        "failures": failures,
    }


def docker_storage_preflight(*, force_refresh: bool = False) -> dict[str, Any]:
    """Validate Docker once per batch while refreshing free space per instance."""

    errors: list[str] = []
    docker_root = ""
    storage_driver = ""
    server_version = ""
    docker_info_source = "cache"
    docker_info_failures: list[str] = []
    free_bytes: int | None = None
    total_bytes: int | None = None
    try:
        minimum_free_gib = float(
            os.environ.get(
                "BRT_DOCKER_MIN_FREE_GB", str(DEFAULT_DOCKER_MIN_FREE_GIB)
            )
        )
        if minimum_free_gib < 0:
            raise ValueError("must be non-negative")
    except ValueError as exc:
        minimum_free_gib = DEFAULT_DOCKER_MIN_FREE_GIB
        errors.append(f"BRT_DOCKER_MIN_FREE_GB is invalid: {exc}")
    reject_vfs, flag_error = _env_flag("BRT_REJECT_DOCKER_VFS", True)
    if flag_error:
        errors.append(flag_error)

    with _DOCKER_INFO_LOCK:
        if force_refresh:
            _DOCKER_INFO_CACHE.clear()
        if not _DOCKER_INFO_CACHE:
            docker_info_source = "daemon"
            info, docker_info_failures = _docker_info_with_retry()
            if info is None:
                errors.append("Docker daemon is unavailable after retry")
            elif _cache_docker_info(info) is None:
                errors.append(
                    "Docker info did not report data-root, driver, and version"
                )
        if _DOCKER_INFO_CACHE:
            docker_root = _DOCKER_INFO_CACHE["docker_root"]
            storage_driver = _DOCKER_INFO_CACHE["storage_driver"]
            server_version = _DOCKER_INFO_CACHE["server_version"]

    if docker_root:
        try:
            usage = shutil.disk_usage(docker_root)
        except OSError as exc:
            errors.append(
                f"cannot inspect Docker data-root capacity at {docker_root}: {exc}"
            )
        else:
            free_bytes = int(usage.free)
            total_bytes = int(usage.total)
            required = int(minimum_free_gib * (1024 ** 3))
            if free_bytes < required:
                errors.append(
                    "Docker data-root free space is below the generation gate: "
                    f"{free_bytes / (1024 ** 3):.1f} GiB available at "
                    f"{docker_root}, {minimum_free_gib:.1f} GiB required"
                )
        if reject_vfs and storage_driver.lower() == "vfs":
            errors.append(
                "Docker storage driver vfs is forbidden for official generation; "
                "use overlay2 or set BRT_REJECT_DOCKER_VFS=0 only for diagnostics"
            )

    return {
        "ok": not errors,
        "docker_server_version": server_version,
        "docker_info_source": docker_info_source,
        "docker_info_failures": docker_info_failures,
        "docker_root_dir": docker_root,
        "docker_storage_driver": storage_driver,
        "docker_free_bytes": free_bytes,
        "docker_free_gib": (
            free_bytes / (1024 ** 3) if free_bytes is not None else None
        ),
        "docker_total_bytes": total_bytes,
        "docker_minimum_free_gib": minimum_free_gib,
        "docker_reject_vfs": reject_vfs,
        "errors": errors,
    }


def official_docker_preflight(
    dataset_mode: str,
    official_python: str,
    swtbench_root: str,
    tddbench_root: str,
    required_paths: list[str],
) -> dict[str, Any]:
    harness_root = Path(swtbench_root if dataset_mode == "swt" else tddbench_root)
    harness_marker = (
        harness_root / "src" / "docker_build.py"
        if dataset_mode == "swt"
        else harness_root / "tddbench" / "harness" / "docker_build.py"
    )
    storage = docker_storage_preflight(force_refresh=True)
    missing_paths = [path for path in required_paths if not Path(path).exists()]
    errors: list[str] = list(storage["errors"])
    if not Path(official_python).is_file():
        errors.append(f"official harness Python is missing: {official_python}")
    if not harness_marker.is_file():
        errors.append(f"official harness checkout is missing: {harness_root}")
    if missing_paths:
        errors.append("required paths are missing: " + ", ".join(missing_paths))
    return {
        "ok": not errors,
        "runtime_backend": OFFICIAL_RUNTIME_BACKEND,
        "dataset_mode": dataset_mode,
        **{key: value for key, value in storage.items() if key not in {"ok", "errors"}},
        "official_python": str(Path(official_python).resolve()),
        "harness_root": str(harness_root.resolve()),
        "host_project_environment_created": False,
        "errors": errors,
    }


class OfficialDockerRuntime:
    def __init__(
        self,
        *,
        dataset_mode: str,
        issue_row: dict[str, Any],
        official_python: str,
        harness_root: str,
        source_repo: str,
        output_dir: str,
        startup_timeout: int,
    ) -> None:
        self.dataset_mode = dataset_mode
        self.request = safe_runtime_request(issue_row)
        self.instance_id = self.request["instance_id"]
        self.base_commit = self.request["base_commit"]
        self.official_python = str(Path(official_python).resolve())
        self.harness_root = str(Path(harness_root).resolve())
        self.source_repo = str(Path(source_repo).resolve())
        self.output_dir = Path(output_dir)
        self.startup_timeout = max(int(startup_timeout), 1800)
        self.container_id = ""
        self.container_name = ""
        self.image = ""
        self.image_owned = True
        self.container_persistent = False
        self.repo_directory = "/testbed"
        self.env_name = "testbed"
        self.manifest: dict[str, Any] = {}
        self._execution_index = 0
        self._exec_lock = threading.RLock()

    @property
    def request_path(self) -> Path:
        return self.output_dir / "official_generation_runtime_request.json"

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / "official_generation_runtime.json"

    @property
    def lifecycle_path(self) -> Path:
        return self.output_dir / "official_generation_startup_lifecycle.json"

    def _cleanup_failed_startup(self) -> dict[str, Any]:
        """Clean exact helper-owned objects even when the helper was killed."""

        try:
            lifecycle = json.loads(
                self.lifecycle_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            lifecycle = {}
        names = [
            str(name)
            for name in lifecycle.get(
                "owned_container_names", lifecycle.get("container_names", [])
            )
            if str(name)
        ]
        commands: list[dict[str, Any]] = []
        for name in names:
            result = _run(["docker", "rm", "-f", name], timeout=120)
            commands.append(
                {
                    "command": ["docker", "rm", "-f", name],
                    "returncode": result.returncode,
                    "stderr": str(result.stderr or "")[-2000:],
                }
            )
        image = str(lifecycle.get("image") or "")
        image_owned = bool(lifecycle.get("image_owned", True))
        if image_owned and image.startswith(("exec.eval.", "sweb.eval.")):
            result = _run(
                ["docker", "image", "rm", "-f", image], timeout=120
            )
            commands.append(
                {
                    "command": ["docker", "image", "rm", "-f", image],
                    "returncode": result.returncode,
                    "stderr": str(result.stderr or "")[-2000:],
                }
            )
        context_root = str(lifecycle.get("context_root") or "")
        if context_root:
            context = Path(context_root).resolve()
            output = self.output_dir.resolve()
            try:
                context.relative_to(output)
            except ValueError:
                pass
            else:
                if context.name.startswith(".official_build_context_"):
                    shutil.rmtree(context, ignore_errors=True)
        cleanup = {
            "lifecycle": lifecycle,
            "commands": commands,
            "status": "ATTEMPTED",
        }
        safe_json_dump(
            cleanup,
            str(self.output_dir / "official_generation_startup_cleanup.json"),
        )
        return cleanup

    def start(self) -> None:
        storage = docker_storage_preflight()
        if not storage["ok"]:
            raise RuntimeError(
                "official Docker storage preflight failed: "
                + "; ".join(storage["errors"])
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        safe_json_dump(self.request, str(self.request_path))
        helper = Path(__file__).resolve().parents[1] / "scripts" / "official_generation_container.py"
        build_log = self.output_dir / "official_generation_container_build.log"
        command = [
            self.official_python,
            str(helper),
            "--dataset",
            self.dataset_mode,
            "--request",
            str(self.request_path),
            "--harness-root",
            self.harness_root,
            "--source-repo",
            self.source_repo,
            "--log-path",
            str(self.output_dir / "official_harness_build.log"),
            "--lifecycle-path",
            str(self.lifecycle_path),
        ]
        try:
            proc = _run(
                command,
                timeout=self.startup_timeout,
                cwd=self.harness_root,
            )
        except subprocess.TimeoutExpired as exc:
            output = f"{exc.stdout or ''}\n{exc.stderr or ''}"
            build_log.write_text(output, encoding="utf-8")
            self._cleanup_failed_startup()
            raise RuntimeError(
                "official harness generation-container startup timed out after "
                f"{self.startup_timeout} seconds; see {build_log}"
            ) from exc
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        build_log.write_text(output, encoding="utf-8")
        marker = "BRT_OFFICIAL_CONTAINER="
        payload_line = next(
            (line for line in reversed(output.splitlines()) if line.startswith(marker)),
            "",
        )
        if proc.returncode != 0 or not payload_line:
            self._cleanup_failed_startup()
            raise RuntimeError(
                "official harness could not start generation container; "
                f"see {build_log}"
            )
        payload = json.loads(payload_line[len(marker) :])
        self.container_id = str(payload["container_id"])
        self.container_name = str(payload.get("container_name") or "")
        self.image = str(payload.get("image") or "")
        if not self.image:
            raise RuntimeError("official harness did not report its instance image")
        self.image_owned = bool(payload.get("image_owned", True))
        self.container_persistent = bool(
            payload.get("container_persistent", False)
        )
        self.repo_directory = str(payload.get("repo_directory") or "/testbed")
        self.env_name = str(payload.get("env_name") or "testbed")
        self.manifest = {
            "schema_version": 1,
            "runtime_backend": OFFICIAL_RUNTIME_BACKEND,
            "dataset_mode": self.dataset_mode,
            "harness": payload.get("harness"),
            "harness_root": self.harness_root,
            "official_python": self.official_python,
            "instance_id": self.instance_id,
            "repo": self.request["repo"],
            "version": self.request["version"],
            "base_commit": self.base_commit,
            "environment_setup_commit": self.request["environment_setup_commit"],
            "image": self.image,
            "platform": payload.get("platform"),
            "build_attempts": payload.get("build_attempts", 1),
            "prior_build_errors": payload.get("prior_build_errors", []),
            "attempt_container_names": payload.get(
                "attempt_container_names", []
            ),
            "attempt_cleanup": payload.get("attempt_cleanup", []),
            "docker_api_timeout_seconds": payload.get(
                "docker_api_timeout_seconds"
            ),
            "official_setup_compatibility_changes": payload.get(
                "official_setup_compatibility_changes", []
            ),
            "source_fetch_mode": payload.get("source_fetch_mode", "official"),
            "source_checkout_commit": payload.get("source_checkout_commit", ""),
            "official_instance_image_key": payload.get(
                "official_instance_image_key", payload.get("image")
            ),
            "container_id": self.container_id,
            "container_name": self.container_name,
            "container_persistent": self.container_persistent,
            "container_reused": bool(payload.get("container_reused", False)),
            "container_adopted": bool(payload.get("container_adopted", False)),
            "container_registry": str(payload.get("container_registry") or ""),
            "repo_directory": self.repo_directory,
            "env_name": self.env_name,
            "gold_fields_present": [],
            "host_project_environment_created": False,
            "container_reuse_scope": (
                "persistent_official_instance"
                if self.container_persistent
                else "one_instance"
            ),
            "instance_image_cache_policy": (
                "remove_on_instance_close"
                if self.image_owned
                else "preserve_cached_image"
            ),
            "docker_storage": storage,
            "status": "RUNNING",
        }
        safe_json_dump(self.manifest, str(self.manifest_path))

    def _docker_exec(
        self, command: str, *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            ["docker", "exec", self.container_id, "bash", "-lc", command],
            timeout=timeout,
        )

    def _host_delta(self, host_repo: str) -> tuple[list[str], list[str]]:
        modified = _run(
            ["git", "-C", host_repo, "diff", "--name-only", "-z", "HEAD"],
            timeout=120,
        )
        untracked = _run(
            [
                "git",
                "-C",
                host_repo,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            timeout=120,
        )
        if modified.returncode != 0 or untracked.returncode != 0:
            raise RuntimeError("could not enumerate host candidate delta")
        changed = {
            item
            for raw in (str(modified.stdout or ""), str(untracked.stdout or ""))
            for item in raw.split("\0")
            if item
        }
        files: list[str] = []
        deleted: list[str] = []
        root = Path(host_repo).resolve()
        for relative in sorted(changed):
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"candidate delta escapes worktree: {relative}") from exc
            if candidate.is_file() or candidate.is_symlink():
                files.append(relative)
            elif not candidate.exists():
                deleted.append(relative)
        return files, deleted

    def _copy_delta(self, host_repo: str, files: list[str]) -> None:
        if not files:
            return
        root = Path(host_repo).resolve()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for relative in files:
                archive.add(root / relative, arcname=relative, recursive=False)
        copy = _run(
            ["docker", "cp", "-", f"{self.container_id}:{self.repo_directory}"],
            timeout=300,
            input_bytes=stream.getvalue(),
        )
        if copy.returncode != 0:
            stderr = (copy.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"docker candidate copy failed: {stderr[-2000:]}")

    def execute(
        self,
        command: str,
        host_repo: str,
        timeout: int,
        behavior: BehaviorEvidence | None,
    ) -> ExecutionResult:
        from ..execution.executor import classify_execution

        started = time.time()
        timed_out = False
        with self._exec_lock:
            self._execution_index += 1
            reset = self._docker_exec(
                " && ".join(
                    (
                        f"git -C {shlex.quote(self.repo_directory)} reset --hard {shlex.quote(self.base_commit)}",
                        f"git -C {shlex.quote(self.repo_directory)} clean -fd",
                    )
                ),
                timeout=min(max(timeout, 120), 600),
            )
            if reset.returncode != 0:
                stdout = str(reset.stdout or "")
                stderr = str(reset.stderr or "")
                returncode = reset.returncode
            else:
                files, deleted = self._host_delta(host_repo)
                self._copy_delta(host_repo, files)
                if deleted:
                    targets = " ".join(
                        shlex.quote(f"{self.repo_directory}/{item}")
                        for item in deleted
                    )
                    self._docker_exec(f"rm -f -- {targets}", timeout=120)
                mapped_command = command.replace(
                    str(Path(host_repo).resolve()), self.repo_directory
                )
                python_paths = ":".join(
                    (
                        self.repo_directory,
                        f"{self.repo_directory}/src",
                        f"{self.repo_directory}/lib",
                    )
                )
                activated = " && ".join(
                    (
                        "source /opt/miniconda3/bin/activate",
                        f"conda activate {shlex.quote(self.env_name)}",
                        f"cd {shlex.quote(self.repo_directory)}",
                        f"export PYTHONPATH={shlex.quote(python_paths)}:${{PYTHONPATH:-}}",
                        mapped_command,
                    )
                )
                try:
                    proc = _run(
                        [
                            "docker",
                            "exec",
                            self.container_id,
                            "timeout",
                            "--signal=TERM",
                            "--kill-after=10s",
                            f"{int(timeout)}s",
                            "bash",
                            "-lc",
                            activated,
                        ],
                        timeout=int(timeout) + 30,
                    )
                    stdout = str(proc.stdout or "")
                    stderr = str(proc.stderr or "")
                    returncode = proc.returncode
                    timed_out = returncode in {124, 137}
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    stdout = str(exc.stdout or "")
                    stderr = str(exc.stderr or "")
                    returncode = 124
            status = classify_execution(
                returncode, stdout, stderr, timed_out, behavior
            )
            result = ExecutionResult(
                instance_id=self.instance_id,
                command=command,
                cwd=host_repo,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                timeout=timed_out,
                duration=time.time() - started,
                status=status,
                error_reason=(
                    "command timed out"
                    if timed_out
                    else "" if returncode == 0 else (stdout + "\n" + stderr)[-4000:]
                ),
            )
            record = {
                **result.to_dict(),
                "runtime_backend": OFFICIAL_RUNTIME_BACKEND,
                "container_id": self.container_id,
                "execution_index": self._execution_index,
            }
            with (self.output_dir / "official_generation_executions.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            return result

    def close(self) -> None:
        if not self.container_id and not self.image:
            return

        if self.container_persistent:
            self.manifest.update(
                {
                    "status": "PERSISTENT_READY",
                    "container_cleanup_attempted": False,
                    "container_cleanup_returncode": 0,
                    "instance_image_cleanup_attempted": False,
                    "instance_image_cleanup_returncode": 0,
                    "instance_image_cache_policy": "preserve_cached_image",
                }
            )
            safe_json_dump(self.manifest, str(self.manifest_path))
            self.container_id = ""
            self.image = ""
            self.image_owned = True
            return

        def cleanup_command(
            command: list[str], timeout_seconds: int
        ) -> tuple[int, str, str]:
            try:
                result = _run(command, timeout=timeout_seconds)
                return (
                    int(result.returncode),
                    str(result.stdout or "")[-2000:],
                    str(result.stderr or "")[-2000:],
                )
            except Exception as exc:  # cleanup must continue to the image step
                return -1, "", repr(exc)[-2000:]

        container_attempted = bool(self.container_id)
        container_cleanup = (
            cleanup_command(["docker", "rm", "-f", self.container_id], 120)
            if container_attempted
            else (0, "", "")
        )
        try:
            image_cleanup_timeout = max(
                120,
                int(
                    os.environ.get(
                        "BRT_DOCKER_IMAGE_CLEANUP_TIMEOUT_SECONDS", "900"
                    )
                ),
            )
        except ValueError:
            image_cleanup_timeout = 900
        image_attempted = bool(self.image and self.image_owned)
        image_cleanup = (
            cleanup_command(
                ["docker", "image", "rm", "-f", self.image],
                image_cleanup_timeout,
            )
            if image_attempted
            else (0, "", "")
        )
        image_cleanup_attempts = (
            [
                {
                    "returncode": image_cleanup[0],
                    "stdout": image_cleanup[1],
                    "stderr": image_cleanup[2],
                }
            ]
            if image_attempted
            else []
        )
        docker_recovery = {
            "status": "NOT_NEEDED",
            "attempts": 0,
            "elapsed_seconds": 0.0,
            "failures": [],
        }
        if image_attempted and image_cleanup[0] == -1:
            docker_recovery = _wait_for_docker_ready()
            if docker_recovery["status"] == "READY":
                retry = cleanup_command(
                    ["docker", "image", "rm", "-f", self.image],
                    image_cleanup_timeout,
                )
                if retry[0] != 0 and "No such image" in retry[2]:
                    retry = (0, retry[1], retry[2])
                image_cleanup_attempts.append(
                    {
                        "returncode": retry[0],
                        "stdout": retry[1],
                        "stderr": retry[2],
                    }
                )
                image_cleanup = retry
        cleaned = container_cleanup[0] == 0 and image_cleanup[0] == 0
        self.manifest.update(
            {
                "status": "CLEANED" if cleaned else "CLEANUP_ERROR",
                "cleanup_returncode": container_cleanup[0],
                "cleanup_stdout": container_cleanup[1],
                "cleanup_stderr": container_cleanup[2],
                "container_cleanup_attempted": container_attempted,
                "container_cleanup_returncode": container_cleanup[0],
                "container_cleanup_stdout": container_cleanup[1],
                "container_cleanup_stderr": container_cleanup[2],
                "instance_image_cleanup_attempted": image_attempted,
                "instance_image_cleanup_returncode": image_cleanup[0],
                "instance_image_cleanup_stdout": image_cleanup[1],
                "instance_image_cleanup_stderr": image_cleanup[2],
                "instance_image_cleanup_timeout_seconds": image_cleanup_timeout,
                "instance_image_cleanup_attempts": image_cleanup_attempts,
                "docker_recovery_after_cleanup": docker_recovery,
                "instance_image_cache_policy": (
                    "remove_on_instance_close"
                    if self.image_owned
                    else "preserve_cached_image"
                ),
            }
        )
        safe_json_dump(self.manifest, str(self.manifest_path))
        self.container_id = ""
        self.image = ""
        self.image_owned = True


@contextmanager
def official_generation_runtime(
    *,
    dataset_mode: str,
    issue_row: dict[str, Any],
    official_python: str,
    harness_root: str,
    source_repo: str,
    output_dir: str,
    startup_timeout: int,
) -> Iterator[OfficialDockerRuntime]:
    runtime = OfficialDockerRuntime(
        dataset_mode=dataset_mode,
        issue_row=issue_row,
        official_python=official_python,
        harness_root=harness_root,
        source_repo=source_repo,
        output_dir=output_dir,
        startup_timeout=startup_timeout,
    )
    instance_lock = InstanceLock(runtime.instance_id) if dataset_mode == "swt" else None
    if instance_lock is not None:
        instance_lock.acquire()
    try:
        runtime.start()
        with _ACTIVE_LOCK:
            if runtime.instance_id in _ACTIVE:
                runtime.close()
                raise RuntimeError(
                    f"official Docker runtime already active: {runtime.instance_id}"
                )
            _ACTIVE[runtime.instance_id] = runtime
        try:
            yield runtime
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.pop(runtime.instance_id, None)
            runtime.close()
    except Exception:
        runtime.close()
        raise
    finally:
        if instance_lock is not None:
            instance_lock.release()


def _cleanup_active_at_exit() -> None:
    with _ACTIVE_LOCK:
        runtimes = list(_ACTIVE.values())
        _ACTIVE.clear()
    for runtime in runtimes:
        try:
            runtime.close()
        except Exception:
            pass


atexit.register(_cleanup_active_at_exit)

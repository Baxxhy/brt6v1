#!/usr/bin/env python3
"""Build and start one gold-free official benchmark generation container.

This helper intentionally runs under the official harness interpreter.  Its
JSON input is an allow-listed runtime descriptor, never a benchmark dataset
row, so the generation process cannot accidentally receive a gold patch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path


SAFE_KEYS = {
    "instance_id",
    "repo",
    "version",
    "base_commit",
    "environment_setup_commit",
}

DEFAULT_DOCKER_API_TIMEOUT_SECONDS = 300
_APT_RETRY_OPTIONS = "-o Acquire::Retries=5 -o Acquire::http::Timeout=120"


def _write_lifecycle(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _remove_container_by_name(client, name: str) -> dict[str, str | int]:
    """Reconcile a Docker create whose server result may outlive its response."""

    if not name:
        return {"name": name, "returncode": 0, "error": ""}
    try:
        container = client.containers.get(name)
    except Exception as exc:
        # Docker's NotFound type is deliberately not imported at module import
        # time because this helper is also unit tested in the BRT interpreter.
        if exc.__class__.__name__ in {"NotFound", "ImageNotFound"}:
            return {"name": name, "returncode": 0, "error": ""}
        return {"name": name, "returncode": 1, "error": repr(exc)[-2000:]}
    try:
        container.remove(force=True)
        return {"name": name, "returncode": 0, "error": ""}
    except Exception as exc:
        return {"name": name, "returncode": 1, "error": repr(exc)[-2000:]}


def _normalize_swt_repo_commands(
    request: dict[str, str], commands: list[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Keep official recipes, adding only time-stable tool/transport guards."""

    normalized: list[str] = []
    changes: list[dict[str, str]] = []
    for original in commands:
        command = original
        if (
            request["repo"] == "pylint-dev/pylint"
            and command.strip() == "python -m pip install -e ."
        ):
            command = (
                "python -m pip install --disable-pip-version-check "
                "'pip==21.2.4' 'setuptools==63.4.3' && " + command
            )
        for verb in ("update", "upgrade", "install"):
            command = command.replace(
                f"apt-get -y {verb}",
                f"apt-get {_APT_RETRY_OPTIONS} -y {verb}",
            )
            command = command.replace(
                f"apt-get {verb}",
                f"apt-get {_APT_RETRY_OPTIONS} {verb}",
            )
        normalized.append(command)
        if command != original:
            changes.append({"official": original, "normalized": command})
    return normalized, changes


def _git_output(source_repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(source_repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"local source cache git command failed: {proc.stderr[-2000:]}"
        )
    return proc.stdout


def _prepare_source_checkout(
    request: dict[str, str], source_repo: Path, build_dir: Path
) -> Path:
    """Stage an exact shallow base-commit checkout in the Docker build context."""

    source_repo = source_repo.resolve()
    if not (source_repo / ".git").exists():
        raise ValueError(f"local source repository is missing: {source_repo}")
    if _git_output(source_repo, "cat-file", "-t", request["base_commit"]).strip() != "commit":
        raise ValueError(
            f"local source cache lacks base commit {request['base_commit']}"
        )
    build_dir.mkdir(parents=True, exist_ok=True)
    destination = build_dir / "source"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    commands = [
        ["git", "-C", str(destination), "init", "-q"],
        [
            "git", "-C", str(destination), "-c", "protocol.file.allow=always",
            "fetch", "-q", "--depth=1", "--no-tags", source_repo.as_uri(),
            request["base_commit"],
        ],
        [
            "git", "-C", str(destination), "checkout", "-q", "--detach",
            "FETCH_HEAD",
        ],
        [
            "git", "-C", str(destination), "remote", "add", "origin",
            f"https://github.com/{request['repo']}",
        ],
    ]
    for command in commands:
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1800,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "could not stage local base-commit checkout: "
                + proc.stderr[-2000:]
            )
    actual = _git_output(destination, "rev-parse", "HEAD").strip()
    if actual != request["base_commit"]:
        raise RuntimeError(
            f"staged source commit drifted: expected {request['base_commit']}, got {actual}"
        )
    return destination


def _load_request(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("runtime request must be a JSON object")
    extra = sorted(set(raw) - SAFE_KEYS)
    if extra:
        raise ValueError(f"runtime request contains forbidden keys: {extra}")
    request = {key: str(raw.get(key) or "") for key in sorted(SAFE_KEYS)}
    missing = [
        key
        for key in ("instance_id", "repo", "version", "base_commit")
        if not request[key]
    ]
    if missing:
        raise ValueError(f"runtime request is missing required keys: {missing}")
    request["environment_setup_commit"] = (
        request["environment_setup_commit"] or request["base_commit"]
    )
    return request


def _start_swt_cached(
    request: dict[str, str],
    root: Path,
    log_path: Path,
    source_repo: Path,
    lifecycle_path: Path | None,
) -> dict:
    """Start the pinned official image without rebuilding or touching its tag."""

    package_root = Path(__file__).resolve().parents[2]
    for path in (package_root, root):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)

    import docker

    from brt6.runtime.swt_cached_compat import configure_cached_official_runtime
    from src.constants import MAP_VERSION_TO_INSTALL
    from src.exec_spec import ExecSpec, make_exec_spec
    from src.utils import close_logger, setup_logger

    configure_cached_official_runtime(
        MAP_VERSION_TO_INSTALL,
        ExecSpec,
        source_repo.resolve().parent,
    )
    spec_input = dict(request)
    spec_input["golden_code_patch"] = ""
    spec = make_exec_spec(spec_input)
    spec.rm_image = False
    spec.force_rebuild = False

    docker_api_timeout = max(
        60,
        int(
            os.environ.get(
                "BRT_DOCKER_API_TIMEOUT_SECONDS",
                str(DEFAULT_DOCKER_API_TIMEOUT_SECONDS),
            )
        ),
    )
    client = docker.from_env(timeout=docker_api_timeout)
    logger = setup_logger(
        f"brt6-generation-{request['instance_id']}", log_path, "w"
    )
    image = spec.instance_image_key
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound as exc:
        close_logger(logger)
        raise RuntimeError(f"cached official image missing: {image}") from exc

    attempt_names: list[str] = []
    cleanup_results: list[dict[str, str | int]] = []
    lifecycle = {
        "schema_version": 1,
        "instance_id": request["instance_id"],
        "image": image,
        "image_owned": False,
        "context_root": "",
        "container_names": attempt_names,
        "docker_api_timeout_seconds": docker_api_timeout,
        "status": "PREPARING",
    }
    _write_lifecycle(lifecycle_path, lifecycle)
    container = None
    errors: list[str] = []
    try:
        config = MAP_VERSION_TO_INSTALL[spec.repo][spec.version]
        user = "root" if not config.get("execute_test_as_nonroot", False) else "nonroot"
        retries = max(1, int(os.environ.get("BRT_OFFICIAL_BUILD_RETRIES", "3")))
        for attempt in range(1, retries + 1):
            name = (
                image.replace(":latest", "")
                + f".brt6.{os.getpid()}.{uuid.uuid4().hex[:8]}.a{attempt}"
            )
            attempt_names.append(name)
            lifecycle["status"] = f"ATTEMPT_{attempt}"
            _write_lifecycle(lifecycle_path, lifecycle)
            try:
                container = client.containers.create(
                    image=image,
                    name=name,
                    user=user,
                    detach=True,
                    command="tail -f /dev/null",
                    nano_cpus=config.get("nano_cpus"),
                    platform=spec.platform,
                )
                container.start()
                check = container.exec_run(
                    ["git", "-C", spec.repo_directory, "rev-parse", "HEAD"]
                )
                actual_commit = check.output.decode(
                    "utf-8", errors="replace"
                ).strip()
                if check.exit_code != 0 or actual_commit != request["base_commit"]:
                    raise RuntimeError(
                        "cached image base commit mismatch: "
                        f"expected {request['base_commit']}, got {actual_commit or 'unavailable'}"
                    )
                break
            except Exception as exc:
                errors.append(f"attempt {attempt}/{retries}: {exc!r}")
                logger.error(errors[-1])
                if container is not None:
                    try:
                        container.remove(force=True)
                    except Exception:
                        pass
                    container = None
                cleanup_results.append(_remove_container_by_name(client, name))
                lifecycle["attempt_cleanup"] = cleanup_results
                _write_lifecycle(lifecycle_path, lifecycle)
                if attempt == retries:
                    raise
                time.sleep(min(30, 5 * attempt))

        assert container is not None
        result = {
            "container_id": container.id,
            "container_name": container.name,
            "image": image,
            "image_owned": False,
            "repo_directory": spec.repo_directory,
            "env_name": spec.env_name,
            "platform": spec.platform,
            "harness": "official_swtbench_cached",
            "source_fetch_mode": "official_cached_instance_image",
            "source_checkout_commit": actual_commit,
            "official_instance_image_key": image,
            "build_attempts": len(errors) + 1,
            "prior_build_errors": errors,
            "attempt_container_names": attempt_names,
            "attempt_cleanup": cleanup_results,
            "docker_api_timeout_seconds": docker_api_timeout,
            "official_setup_compatibility_changes": [],
        }
        lifecycle.update(
            {
                "status": "RUNNING",
                "container_id": container.id,
                "container_name": container.name,
            }
        )
        _write_lifecycle(lifecycle_path, lifecycle)
        return result
    except Exception:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
        cleanup_results.extend(
            _remove_container_by_name(client, name) for name in attempt_names
        )
        lifecycle["status"] = "FAILED_CLEANED"
        lifecycle["attempt_cleanup"] = cleanup_results
        _write_lifecycle(lifecycle_path, lifecycle)
        raise
    finally:
        close_logger(logger)


def _start_swt_rebuild(
    request: dict[str, str],
    root: Path,
    log_path: Path,
    source_repo: Path,
    lifecycle_path: Path | None,
) -> dict:
    sys.path.insert(0, str(root))
    import docker

    from src import docker_build as official_docker_build
    from src.exec_spec import ExecSpec, make_exec_spec
    from src.utils import close_logger, setup_logger

    # make_exec_spec needs this field only to derive optional coverage paths.
    # Empty is the deliberate, audited value for generation.
    spec_input = dict(request)
    spec_input["golden_code_patch"] = ""
    official_spec = make_exec_spec(spec_input)
    official_dockerfile = official_spec.instance_dockerfile
    reliable_dockerfile = official_dockerfile.replace(
        "COPY ./setup_repo.sh /root/",
        "COPY ./source/ /testbed/\nCOPY ./setup_repo.sh /root/",
        1,
    )

    compatibility_changes: list[dict[str, str]] = []

    class ReliableFetchExecSpec(ExecSpec):
        """Keep the official image key while staging the exact local base tree."""

        @property
        def instance_image_key(self):
            return self._official_instance_image_key

        @property
        def instance_dockerfile(self):
            return self._reliable_instance_dockerfile

        @property
        def repo_script_list(self):
            nonlocal compatibility_changes
            commands = super().repo_script_list
            commands[0] = f"test -d {self.repo_directory}/.git"
            commands, compatibility_changes = _normalize_swt_repo_commands(
                request, commands
            )
            return commands

        def get_instance_container_name(self):
            return self._brt_container_name

    spec = ReliableFetchExecSpec(**asdict(official_spec))
    spec._official_instance_image_key = official_spec.instance_image_key
    spec._reliable_instance_dockerfile = reliable_dockerfile
    spec._brt_container_name = ""
    context_root = (
        log_path.parent
        / f".official_build_context_{os.getpid()}_{uuid.uuid4().hex[:10]}"
    )
    context_root.mkdir(parents=True, exist_ok=False)
    original_instance_build_dir = official_docker_build.INSTANCE_IMAGE_BUILD_DIR
    official_docker_build.INSTANCE_IMAGE_BUILD_DIR = context_root
    try:
        source_checkout = _prepare_source_checkout(
            request,
            source_repo,
            context_root / spec.instance_image_key.replace(":", "__"),
        )
    except Exception:
        official_docker_build.INSTANCE_IMAGE_BUILD_DIR = (
            original_instance_build_dir
        )
        shutil.rmtree(context_root, ignore_errors=True)
        raise
    docker_api_timeout = max(
        60,
        int(
            os.environ.get(
                "BRT_DOCKER_API_TIMEOUT_SECONDS",
                str(DEFAULT_DOCKER_API_TIMEOUT_SECONDS),
            )
        ),
    )
    try:
        client = docker.from_env(timeout=docker_api_timeout)
        logger = setup_logger(
            f"brt6-generation-{request['instance_id']}", log_path, "w"
        )
    except Exception:
        official_docker_build.INSTANCE_IMAGE_BUILD_DIR = (
            original_instance_build_dir
        )
        shutil.rmtree(context_root, ignore_errors=True)
        raise
    container = None
    attempt_names: list[str] = []
    cleanup_results: list[dict[str, str | int]] = []
    lifecycle = {
        "schema_version": 1,
        "instance_id": request["instance_id"],
        "image": spec.instance_image_key,
        "context_root": str(context_root),
        "container_names": attempt_names,
        "docker_api_timeout_seconds": docker_api_timeout,
        "status": "PREPARING",
    }
    _write_lifecycle(lifecycle_path, lifecycle)
    try:
        retries = max(1, int(os.environ.get("BRT_OFFICIAL_BUILD_RETRIES", "3")))
        errors: list[str] = []
        for attempt in range(1, retries + 1):
            spec._brt_container_name = (
                f"exec.eval.{spec.arch}.{spec.env_hash}.{spec.instance_hash}."
                f"brt6.{os.getpid()}.{uuid.uuid4().hex[:8]}.a{attempt}"
            )
            attempt_names.append(spec._brt_container_name)
            lifecycle["status"] = f"ATTEMPT_{attempt}"
            _write_lifecycle(lifecycle_path, lifecycle)
            try:
                container = official_docker_build.start_container(
                    spec, client, logger, build_mode="api"
                )
                break
            except Exception as exc:
                errors.append(f"attempt {attempt}/{retries}: {exc!r}")
                logger.error(errors[-1])
                error_log = Path(str(getattr(exc, "log_path", "") or ""))
                if error_log.is_file():
                    shutil.copyfile(
                        error_log,
                        log_path.parent
                        / f"official_image_build_attempt_{attempt}.log",
                    )
                if container is not None:
                    try:
                        container.remove(force=True)
                    except Exception:
                        pass
                    container = None
                cleanup = _remove_container_by_name(
                    client, spec._brt_container_name
                )
                cleanup_results.append(cleanup)
                lifecycle["attempt_cleanup"] = cleanup_results
                _write_lifecycle(lifecycle_path, lifecycle)
                if attempt == retries:
                    raise
                time.sleep(min(30, 5 * attempt))
        assert container is not None
        result = {
            "container_id": container.id,
            "container_name": container.name,
            "image": spec.instance_image_key,
            "repo_directory": spec.repo_directory,
            "env_name": spec.env_name,
            "platform": spec.platform,
            "harness": "official_swtbench",
            "source_fetch_mode": "official_commit_local_shallow_checkout",
            "source_checkout_commit": _git_output(
                source_checkout, "rev-parse", "HEAD"
            ).strip(),
            "official_instance_image_key": official_spec.instance_image_key,
            "build_attempts": len(errors) + 1,
            "prior_build_errors": errors,
            "attempt_container_names": attempt_names,
            "attempt_cleanup": cleanup_results,
            "docker_api_timeout_seconds": docker_api_timeout,
            "official_setup_compatibility_changes": compatibility_changes,
        }
        lifecycle["status"] = "RUNNING"
        lifecycle["container_id"] = container.id
        lifecycle["container_name"] = container.name
        _write_lifecycle(lifecycle_path, lifecycle)
        return result
    except Exception:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
        cleanup_results.extend(
            _remove_container_by_name(client, name) for name in attempt_names
        )
        try:
            client.images.remove(spec.instance_image_key, force=True)
        except Exception:
            pass
        lifecycle["status"] = "FAILED_CLEANED"
        lifecycle["attempt_cleanup"] = cleanup_results
        _write_lifecycle(lifecycle_path, lifecycle)
        raise
    finally:
        official_docker_build.INSTANCE_IMAGE_BUILD_DIR = original_instance_build_dir
        shutil.rmtree(context_root, ignore_errors=True)
        close_logger(logger)


def _start_swt(
    request: dict[str, str],
    root: Path,
    log_path: Path,
    source_repo: Path,
    lifecycle_path: Path | None,
) -> dict:
    if os.environ.get("BRT_ALLOW_OFFICIAL_IMAGE_REBUILD", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return _start_swt_rebuild(
            request, root, log_path, source_repo, lifecycle_path
        )
    return _start_swt_cached(
        request, root, log_path, source_repo, lifecycle_path
    )


def _start_tdd(
    request: dict[str, str],
    root: Path,
    log_path: Path,
    source_repo: Path,
    lifecycle_path: Path | None,
) -> dict:
    sys.path.insert(0, str(root))
    import docker

    from tddbench.harness.docker_build import (
        build_container,
        build_env_images,
        close_logger,
        setup_logger,
    )
    from tddbench.harness.constants import INSTANCE_IMAGE_BUILD_DIR
    from tddbench.harness.test_spec import TestSpec, make_test_spec

    # The official TDD image builder expects dataset-shaped metadata.  All
    # patch/test fields are fixed empty strings and never loaded from gold.
    spec_input = {
        **request,
        "problem_statement": "",
        "hints_text": "",
        "created_at": "",
        "patch": "",
        "test_patch": "",
        "FAIL_TO_PASS": "[]",
        "PASS_TO_PASS": "[]",
    }
    official_spec = make_test_spec(spec_input)
    repo_script = list(official_spec.repo_script_list)
    repo_script[0] = "test -d /testbed/.git"
    reliable_dockerfile = official_spec.instance_dockerfile.replace(
        "COPY ./setup_repo.sh /root/",
        "COPY ./source/ /testbed/\nCOPY ./setup_repo.sh /root/",
        1,
    )

    class ReliableFetchTestSpec(TestSpec):
        @property
        def instance_dockerfile(self):
            return self._reliable_instance_dockerfile

    spec_fields = asdict(official_spec)
    spec_fields["repo_script_list"] = repo_script
    spec = ReliableFetchTestSpec(**spec_fields)
    spec._reliable_instance_dockerfile = reliable_dockerfile
    source_checkout = _prepare_source_checkout(
        request,
        source_repo,
        root
        / INSTANCE_IMAGE_BUILD_DIR
        / spec.instance_image_key.replace(":", "__"),
    )
    client = docker.from_env(
        timeout=max(
            60,
            int(
                os.environ.get(
                    "BRT_DOCKER_API_TIMEOUT_SECONDS",
                    str(DEFAULT_DOCKER_API_TIMEOUT_SECONDS),
                )
            ),
        )
    )
    logger = setup_logger(request["instance_id"], log_path, "w")
    container = None
    run_id = f"brt6gen{os.getpid()}{uuid.uuid4().hex[:8]}"
    try:
        retries = max(1, int(os.environ.get("BRT_OFFICIAL_BUILD_RETRIES", "3")))
        errors: list[str] = []
        for attempt in range(1, retries + 1):
            try:
                _, failed = build_env_images(
                    client, [spec], force_rebuild=False, max_workers=1
                )
                if failed:
                    raise RuntimeError(
                        f"official TDDBench environment build failed: {failed}"
                    )
                container = build_container(
                    spec,
                    client,
                    run_id,
                    logger,
                    nocache=False,
                    force_rebuild=False,
                )
                container.start()
                break
            except Exception as exc:
                errors.append(f"attempt {attempt}/{retries}: {exc!r}")
                logger.error(errors[-1])
                error_log = Path(str(getattr(exc, "log_path", "") or ""))
                if error_log.is_file():
                    shutil.copyfile(
                        error_log,
                        log_path.parent
                        / f"official_image_build_attempt_{attempt}.log",
                    )
                if container is not None:
                    try:
                        container.remove(force=True)
                    except Exception:
                        pass
                    container = None
                if attempt == retries:
                    raise
                time.sleep(min(30, 5 * attempt))
        assert container is not None
        return {
            "container_id": container.id,
            "container_name": container.name,
            "image": spec.instance_image_key,
            "repo_directory": "/testbed",
            "env_name": "testbed",
            "platform": spec.platform,
            "harness": "official_tddbench",
            "source_fetch_mode": "official_commit_local_shallow_checkout",
            "source_checkout_commit": _git_output(
                source_checkout, "rev-parse", "HEAD"
            ).strip(),
            "build_attempts": len(errors) + 1,
            "prior_build_errors": errors,
        }
    except Exception:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(source_checkout, ignore_errors=True)
        close_logger(logger)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=("swt", "tdd"), required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--harness-root", type=Path, required=True)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--lifecycle-path", type=Path)
    args = parser.parse_args()

    request = _load_request(args.request)
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    root = args.harness_root.resolve()
    result = (
        _start_swt(
            request,
            root,
            args.log_path,
            args.source_repo,
            args.lifecycle_path,
        )
        if args.dataset == "swt"
        else _start_tdd(
            request,
            root,
            args.log_path,
            args.source_repo,
            args.lifecycle_path,
        )
    )
    print("BRT_OFFICIAL_CONTAINER=" + json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()

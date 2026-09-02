"""Runtime-only reliability fixes for the official SWTBench Docker harness.

The shim deliberately leaves dataset selection, test commands, patches,
coverage collection, grading, image names, and official environment recipes
unchanged.  It only makes host-side source transport and cleanup reliable.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from brt6.runtime.official_container_registry import (
    InstanceLock,
    OfficialContainerRegistry,
    ensure_container_running,
    lock_filename,
)


_CLONE_RE = re.compile(
    r"^git clone(?: -o \S+)? https://github\.com/([^/\s]+)/([^\s]+?)(?:\.git)? (/[A-Za-z0-9_.-]+)$",
    re.MULTILINE,
)
_RESET_RE = re.compile(r"^git reset --hard ([0-9a-f]{40})$", re.MULTILINE)
_INSTALLED = False
_DEFAULT_LOCK_DIR = Path("/root/Baxxhy/BugReproduce/brt6/.runtime/locks")
_DEFAULT_SWT_METADATA_ROOT = Path(__file__).resolve().parent / "vendor/swtbench_metadata"


def _lock_filename(instance_id: str) -> str:
    """Return a filesystem-safe lock name without changing Docker naming."""
    return lock_filename(instance_id)


def _configure_container_reuse(environment: MutableMapping[str, str]) -> None:
    """Enable one persistent official container per benchmark instance."""

    environment["SWT_REUSE_CONTAINERS"] = "1"
    environment["SWT_KEEP_CONTAINERS"] = "1"
    environment["SWT_CONTAINER_REUSE_SCOPE"] = "instance"
    environment["SWT_SKIP_EVAL_INSTALL"] = "1"
    environment["PIP_NO_INDEX"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["HF_HUB_OFFLINE"] = "1"
    environment["HF_DATASETS_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["BRT_SWT_CONTAINER_LOCK_DIR"] = str(_DEFAULT_LOCK_DIR)


def _container_is_reusable(attrs: Mapping[str, Any], expected_image: str) -> bool:
    """Accept only a healthy container created from the expected image tag."""

    config = attrs.get("Config") or {}
    state = attrs.get("State") or {}
    status = str(state.get("Status") or "").lower()
    return (
        config.get("Image") == expected_image
        and not bool(state.get("Dead"))
        and status in {"created", "running", "exited"}
    )


@contextmanager
def _instance_lock(instance_id: str):
    """Serialize all six official states for a single benchmark instance."""

    lock_dir = Path(
        os.environ.get("BRT_SWT_CONTAINER_LOCK_DIR", str(_DEFAULT_LOCK_DIR))
    )
    with InstanceLock(instance_id, lock_dir):
        yield


def _preserve_cached_image(client, image_id, logger=None):
    """Keep the shared official image cache intact after an evaluation."""

    del client
    message = f"Preserving cached image {image_id}."
    if logger not in (None, "quiet"):
        logger.info(message)
    elif logger is None:
        print(message)
    return None


def _decode_test_output(raw: bytes, log_dir: Path) -> str:
    """Decode official test output without discarding invalid raw bytes silently."""

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        diagnostic = {
            "schema_version": 1,
            "policy": "utf-8-strict-then-backslashreplace",
            "raw_bytes": len(raw),
            "error_start": error.start,
            "error_end": error.end,
            "error_reason": error.reason,
            "invalid_bytes_hex": raw[error.start:error.end].hex(),
        }
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "decode_diagnostics.json").write_text(
            json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8"
        )
        return raw.decode("utf-8", errors="backslashreplace")


def _parse_pytest_single_test_progress(log: str) -> dict[str, str]:
    """Parse only an unambiguous one-test progress line from old pytest."""

    if len(re.findall(r"\bcollected\s+1\s+item\b", log)) != 1:
        return {}
    matches = re.findall(
        r"(?m)^\s*(\S+\.py(?:\S*)?)\s+([.FE])\s+\[100%\]\s*$",
        log,
    )
    if len(matches) != 1:
        return {}
    test_path, progress = matches[0]
    summaries = {
        ".": ("PASSED", r"\b1\s+passed\b"),
        "F": ("FAILED", r"\b1\s+failed\b"),
        "E": ("ERROR", r"\b1\s+error\b"),
    }
    status, summary_pattern = summaries[progress]
    if re.search(summary_pattern, log, flags=re.IGNORECASE) is None:
        return {}
    return {test_path: status}


def _run_git(repo: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr[-2000:])
    return process.stdout


def _find_local_repo(repo_root: Path, owner: str, name: str) -> Path:
    aliases = {
        ("pytest-dev", "pytest"): "pytest",
        ("pylint-dev", "pylint"): "pylint",
        ("scikit-learn", "scikit-learn"): "scikit-learn",
    }
    candidates = [
        repo_root / aliases.get((owner, name), name),
        repo_root / f"{owner}__{name}",
    ]
    for candidate in candidates:
        if (candidate / ".git").is_dir():
            return candidate.resolve()
    raise RuntimeError(f"local source cache is missing for {owner}/{name}")


def _stage_checkout(source_repo: Path, commit: str, destination: Path) -> None:
    if _run_git(source_repo, "cat-file", "-t", commit).strip() != "commit":
        raise RuntimeError(f"local source cache lacks commit {commit}")
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    commands = (
        ["git", "-C", str(destination), "init", "-q"],
        [
            "git", "-C", str(destination), "-c", "protocol.file.allow=always",
            "fetch", "-q", "--depth=1", "--no-tags", source_repo.as_uri(), commit,
        ],
        ["git", "-C", str(destination), "checkout", "-q", "--detach", "FETCH_HEAD"],
    )
    for command in commands:
        process = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1800,
            check=False,
        )
        if process.returncode:
            raise RuntimeError(process.stderr[-2000:])
    actual = _run_git(destination, "rev-parse", "HEAD").strip()
    if actual != commit:
        raise RuntimeError(f"staged checkout drifted: expected {commit}, got {actual}")


def _make_tree_world_accessible(root: Path) -> None:
    """Reproduce the official recursive chmod before Docker copies the tree."""

    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        os.chmod(current_path, 0o777)
        for name in directories + files:
            path = current_path / name
            if not path.is_symlink():
                os.chmod(path, 0o777)


def _retryable_build_failure(build_dir: Path) -> bool:
    """Recognize transport or overloaded-filesystem failures, not recipe errors."""

    log_path = build_dir / "build_image.log"
    if not log_path.exists():
        return False
    log = log_path.read_text(encoding="utf-8", errors="replace")[-512_000:]
    astropy_transport_failure = "astropy_helpers" in log and any(
        marker in log
        for marker in (
            "GnuTLS recv error",
            "Failed to connect to github.com",
            "Connection timed out",
            "TLS connection was non-properly terminated",
        )
    )
    scm_git_timeout = (
        "subprocess.TimeoutExpired" in log
        and "git" in log
        and "status" in log
        and "timed out after 40 seconds" in log
    )
    isolated_build_index_failure = (
        "Installing build dependencies" in log
        and "ResolutionImpossible" in log
        and "no matching distributions available" in log
        and "setuptools" in log
    )
    return (
        astropy_transport_failure
        or scm_git_timeout
        or isolated_build_index_failure
    )


def _bounded_setup_logger(instance_id: str, log_file: Path, mode: str = "a"):
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"{instance_id}.{log_file.name}")
    if mode == "w" and log_file.exists():
        log_file.unlink()
    max_mb = max(1, int(os.environ.get("BRT_OFFICIAL_LOG_MAX_MB", "32")))
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        mode="a",
        maxBytes=max_mb * 1024 * 1024,
        backupCount=1,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    setattr(logger, "log_file", log_file)
    return logger


def install() -> None:
    """Install idempotent host-side patches before the official CLI imports."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _configure_container_reuse(os.environ)

    import docker
    from src import docker_utils, log_parsers, utils
    from src.constants import MAP_VERSION_TO_INSTALL
    from src.exec_spec import ExecSpec

    from brt6.runtime.swt_cached_compat import configure_cached_official_runtime

    repo_root = Path(
        os.environ.get(
            "BRT_SWT_LOCAL_REPOS", str(_DEFAULT_SWT_METADATA_ROOT)
        )
    )
    configure_cached_official_runtime(
        MAP_VERSION_TO_INSTALL,
        ExecSpec,
        repo_root,
    )

    original_pytest_v2_parser = log_parsers.parse_log_pytest_v2

    def parse_pytest_v2_with_single_test_fallback(log: str) -> dict[str, str]:
        parsed = original_pytest_v2_parser(log)
        return parsed or _parse_pytest_single_test_progress(log)

    for repo, parser in list(log_parsers.MAP_REPO_TO_PARSER.items()):
        if parser is original_pytest_v2_parser:
            log_parsers.MAP_REPO_TO_PARSER[repo] = (
                parse_pytest_v2_with_single_test_fallback
            )

    utils.setup_logger = _bounded_setup_logger

    remove_image = _preserve_cached_image
    docker_utils.remove_image = remove_image

    from src import docker_build

    docker_build.setup_logger = _bounded_setup_logger

    def require_cached_instance_image(
        exec_spec, force_rebuild=False, build_mode="api"
    ):
        del force_rebuild, build_mode
        client = docker.from_env()
        try:
            client.images.get(exec_spec.instance_image_key)
        except docker.errors.ImageNotFound as exc:
            raise RuntimeError(
                f"cached official image missing: {exec_spec.instance_image_key}"
            ) from exc
        finally:
            client.close()

    docker_build.build_instance_image_from_exec_spec = (
        require_cached_instance_image
    )

    def build_error_str(error) -> str:
        return (
            f"{error.image_name}: {Exception.__str__(error)}\n"
            f"Check ({error.log_path}) for more information."
        )

    docker_build.BuildImageError.__str__ = build_error_str
    original_build_image = docker_build.build_image

    def reliable_build_image(*args: Any, **kwargs: Any):
        names = (
            "image_name", "setup_scripts", "dockerfile", "platform", "client",
            "build_dir", "nocache", "build_mode",
        )
        values = dict(zip(names, args))
        values.update(kwargs)
        setup_scripts = values.get("setup_scripts") or {}
        repo_script = setup_scripts.get("setup_repo.sh")
        if not repo_script:
            return original_build_image(*args, **kwargs)

        clone_match = _CLONE_RE.search(repo_script)
        reset_match = _RESET_RE.search(repo_script)
        if not clone_match or not reset_match:
            return original_build_image(*args, **kwargs)

        owner, repo_name, container_repo = clone_match.groups()
        commit = reset_match.group(1)
        build_dir = Path(values["build_dir"])
        source_dir = build_dir / "source"
        repo_root = Path(
            os.environ.get(
                "BRT_SWT_LOCAL_REPOS", str(_DEFAULT_SWT_METADATA_ROOT)
            )
        )
        source_repo = _find_local_repo(repo_root, owner, repo_name)
        _stage_checkout(source_repo, commit, source_dir)
        _run_git(
            source_dir,
            "remote", "add", "origin", f"https://github.com/{owner}/{repo_name}",
        )
        _make_tree_world_accessible(source_dir)

        patched_scripts = dict(setup_scripts)
        patched_repo_script = repo_script.replace(
            clone_match.group(0), f"test -d {container_repo}/.git", 1
        )
        official_chmod = f"chmod -R 777 {container_repo}"
        permission_check = (
            f"chmod 777 {container_repo}\n"
            f'test -z "$(find {container_repo} -xdev -mindepth 1 '
            f'! -perm 0777 -print -quit)"'
        )
        patched_repo_script = patched_repo_script.replace(
            official_chmod, permission_check, 1
        )
        patched_scripts["setup_repo.sh"] = patched_repo_script
        copy_line = f"COPY ./source/ {container_repo}/\n"
        dockerfile = values["dockerfile"]
        if copy_line not in dockerfile:
            dockerfile = dockerfile.replace(
                "COPY ./setup_repo.sh /root/", copy_line + "COPY ./setup_repo.sh /root/", 1
            )
        values["setup_scripts"] = patched_scripts
        values["dockerfile"] = dockerfile
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "brt_runtime_compatibility.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scope": "source_transport_and_permission_equivalence",
                    "official_repo": f"{owner}/{repo_name}",
                    "base_commit": commit,
                    "local_source_repo": str(source_repo),
                    "container_repo": container_repo,
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        try:
            try:
                return original_build_image(**values)
            except (docker.errors.BuildError, docker_build.BuildImageError):
                if not _retryable_build_failure(build_dir):
                    raise
                (build_dir / "brt_runtime_build_retry.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "attempts": 2,
                            "reason": "transient_transport_or_git_status_timeout",
                        },
                        indent=2,
                    ) + "\n",
                    encoding="utf-8",
                )
                return original_build_image(**values)
        finally:
            shutil.rmtree(source_dir, ignore_errors=True)

    docker_build.build_image = reliable_build_image

    original_build_container = docker_build.build_container

    def reuse_validated_container(
        exec_spec, client, logger, nocache, force_rebuild=False, build_mode="api"
    ):
        registry = OfficialContainerRegistry()
        resolution = registry.resolve(
            client,
            instance_id=exec_spec.instance_id,
            expected_image=exec_spec.instance_image_key,
            preferred_name=exec_spec.get_instance_container_name(),
        )
        container_name = resolution.name
        container = resolution.container
        if container is not None:
            ensure_container_running(container)
            logger.info(
                f"Reusing persistent container {container_name} for "
                f"{exec_spec.instance_id}."
            )
            return container

        original_name_method = exec_spec.get_instance_container_name
        exec_spec.get_instance_container_name = lambda: container_name
        try:
            container = original_build_container(
                exec_spec,
                client,
                logger,
                nocache,
                force_rebuild=force_rebuild,
                build_mode=build_mode,
            )
        finally:
            exec_spec.get_instance_container_name = original_name_method
        registry.confirm(
            instance_id=exec_spec.instance_id,
            expected_image=exec_spec.instance_image_key,
            container_name=container.name,
        )
        return container

    docker_build.build_container = reuse_validated_container

    def evaluation_error_str(error) -> str:
        return (
            f"{error.instance_id}: {Exception.__str__(error)}\n"
            f"Check ({error.log_file}) for more information."
        )

    def patch_run_evaluation(run_evaluation) -> None:
        run_evaluation.EvaluationError.__str__ = evaluation_error_str
        run_evaluation.remove_image = remove_image

        def eval_in_container(
            log_dir,
            container,
            logger,
            eval_script,
            timeout,
            instance_id,
            compute_coverage,
            build_mode,
        ):
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            eval_file = log_dir / "eval.sh"
            eval_file.write_text(eval_script, encoding="utf-8")
            logger.info(
                f"Eval script for {instance_id} written to eval.sh, now applying to container..."
            )
            run_evaluation.copy_to_container(
                container, eval_file, Path("/eval.sh"), build_mode=build_mode
            )
            if compute_coverage:
                trace_file = (
                    Path(run_evaluation.__file__).parent / "auxillary_src" / "trace.py"
                )
                run_evaluation.copy_to_container(
                    container,
                    trace_file,
                    Path("/root/trace.py"),
                    build_mode=build_mode,
                )

            raw_output = run_evaluation.exec_run_with_timeout(
                container, "/bin/bash /eval.sh", timeout=timeout
            )
            test_output = _decode_test_output(raw_output, log_dir)
            test_output_path = log_dir / "test_output.txt"
            test_output_path.write_text(test_output, encoding="utf-8")
            logger.info(f"Test output for {instance_id} written to {test_output_path}")
            return test_output_path

        run_evaluation.eval_in_container = eval_in_container

        # The official loop evaluates six patch states per benchmark instance.
        # Defer cache_level=env removal until all six states have finished.
        original_run_instance = run_evaluation.run_instance

        def run_instance_with_one_image_cleanup(
            test_spec, pred, rm_image, *args: Any, **kwargs: Any
        ):
            with _instance_lock(test_spec.instance_id):
                try:
                    return original_run_instance(
                        test_spec, pred, False, *args, **kwargs
                    )
                finally:
                    if rm_image:
                        client = docker.from_env()
                        try:
                            remove_image(
                                client, test_spec.exec_spec.instance_image_key
                            )
                        except Exception as error:
                            print(
                                "Deferred instance-image cleanup failed for "
                                f"{test_spec.instance_id}: {type(error).__name__}: "
                                f"{str(error)[-2000:]}"
                            )
                        finally:
                            client.close()

        run_evaluation.run_instance = run_instance_with_one_image_cleanup

    from src import run_evaluation as package_run_evaluation

    run_evaluation_modules = [package_run_evaluation]
    top_level_run_evaluation = sys.modules.get("run_evaluation")
    if (
        top_level_run_evaluation is not None
        and top_level_run_evaluation is not package_run_evaluation
    ):
        run_evaluation_modules.append(top_level_run_evaluation)
    for run_evaluation_module in run_evaluation_modules:
        patch_run_evaluation(run_evaluation_module)

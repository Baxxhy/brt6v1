"""Direct same-directory evaluator for BRT6 generated tests.

This evaluator intentionally does not use iCoRe/Libro's insertion pipeline.
It copies each generated final_test.py into a new test_brt_*.py file under the
same directory as the primary related test, then executes only that file/test.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..execution.executor import no_tests_executed

from ..io.io_utils import load_issue_data
from ..core.utils import ensure_dir, safe_json_dump, sanitize_instance_id
from ..retrieval.icore_runtime import (
    ensure_icore_environment,
    ensure_isolated_runtime_environment,
    icore_setup_command,
    make_instance_spec,
    remove_isolated_runtime_environment,
)
from ..runtime.conda_env_manager import (
    classify_env_error,
    conda_activate_cmd,
    environment_cache_key,
    environment_lock_path,
    environment_operation_lock,
    env_health_check,
    environment_manifest,
    project_cache_ready,
    project_health_check,
    preflight_system,
    read_environment_state,
    resolve_eval_env,
    write_environment_state,
)


SWT_TRACE_PATH = Path(__file__).resolve().parent / "vendor" / "swt_trace.py"
LEGACY_SYMPY_COMPAT_PATH = (
    Path(__file__).resolve().parents[1] / "runtime" / "legacy_sympy_compat"
)
DJANGO_GENERATED_TEST_RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "runtime"
    / "django_generated_test_runner.py"
)
SWT_TRACE_SHA256 = "2d79a79444c267790ee3a0bf41ef018b6776a4b598f1629e57e0e47d4da8b561"
SWT_NON_TEST_EXTENSIONS = (
    ".json",
    ".png",
    "csv",
    ".txt",
    ".md",
    ".jpg",
    ".jpeg",
    ".pkl",
    ".yml",
    ".yaml",
    ".toml",
)


def repo_path(repo_root_base: str, issue: dict[str, Any]) -> str:
    return str(Path(repo_root_base) / issue["repo"].split("/")[-1])


def run_shell(cmd: str, cwd: str, timeout: int | None = None) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "command": cmd,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration": time.time() - started,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "cwd": cwd,
            "returncode": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace"),
            "stderr": exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace"),
            "duration": time.time() - started,
            "timeout": True,
        }


@contextlib.contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git_lock_paths(repo_dir: str) -> list[Path]:
    repo = Path(repo_dir)
    paths = [repo / ".git" / "index.lock"]
    git_file = repo / ".git"
    if git_file.is_file():
        try:
            text = git_file.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        prefix = "gitdir:"
        if text.startswith(prefix):
            git_dir = (repo / text[len(prefix):].strip()).resolve()
            paths.append(git_dir / "index.lock")
    return paths


def wait_for_git_locks(repo_dir: str, timeout: int = 600) -> dict[str, Any]:
    started = time.time()
    observed: list[str] = []
    while True:
        existing = [path for path in _git_lock_paths(repo_dir) if path.exists()]
        if not existing:
            return {
                "waited": time.time() - started,
                "observed_locks": observed,
                "timeout": False,
            }
        observed = sorted({*observed, *(str(path) for path in existing)})
        if time.time() - started >= timeout:
            return {
                "waited": time.time() - started,
                "observed_locks": observed,
                "timeout": True,
            }
        time.sleep(2)


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def git_reset_to(repo_dir: str, commit: str, clean: bool = True) -> dict[str, Any]:
    pre_wait = wait_for_git_locks(repo_dir)
    if pre_wait.get("timeout"):
        raise RuntimeError(
            f"git locks did not clear in {repo_dir}: {pre_wait.get('observed_locks')}"
        )
    reset = run_shell(f"git reset --hard {shlex.quote(commit)}", repo_dir, 120)
    if reset["returncode"] != 0:
        retry_wait = wait_for_git_locks(repo_dir)
        retry = run_shell(f"git reset --hard {shlex.quote(commit)}", repo_dir, 120)
        reset = {"first": reset, "retry_wait": retry_wait, "retry": retry, **retry}
    if reset["returncode"] != 0:
        raise RuntimeError(f"git reset failed in {repo_dir}: {reset.get('stderr') or reset.get('stdout')}")
    clean_result = None
    if clean:
        clean_result = run_shell("git clean -fdxq", repo_dir, 120)
        if clean_result["returncode"] != 0:
            retry_wait = wait_for_git_locks(repo_dir)
            retry = run_shell("git clean -fdxq", repo_dir, 120)
            clean_result = {"first": clean_result, "retry_wait": retry_wait, "retry": retry, **retry}
        if clean_result["returncode"] != 0:
            raise RuntimeError(f"git clean failed in {repo_dir}: {clean_result.get('stderr') or clean_result.get('stdout')}")
    return {"pre_wait": pre_wait, "reset": reset, "clean": clean_result}


def git_reset_clean(repo_dir: str) -> None:
    run_shell("git reset --hard", repo_dir, 120)
    run_shell("git clean -fdxq", repo_dir, 120)


def apply_patch_text(repo_dir: str, patch_text: str) -> dict[str, Any]:
    if not patch_text.strip():
        return {
            "command": "git apply <empty patch>",
            "cwd": repo_dir,
            "returncode": 2,
            "stdout": "",
            "stderr": "empty patch text",
            "duration": 0.0,
            "timeout": False,
        }
    handle = tempfile.NamedTemporaryFile("w", suffix=".brt3.patch", encoding="utf-8", delete=False)
    patch_file = Path(handle.name)
    try:
        with handle:
            handle.write(patch_text)
        return run_shell(f"git apply {shlex.quote(str(patch_file))}", repo_dir, 120)
    finally:
        try:
            patch_file.unlink()
        except OSError:
            pass


def _remove_eval_worktree(base_repo_dir: str, worktree_dir: Path) -> dict[str, Any]:
    result = run_shell(
        f"git worktree remove --force {shlex.quote(str(worktree_dir))}",
        base_repo_dir,
        300,
    )
    if result["returncode"] != 0 and worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
        result["fallback_rmtree"] = True
    return result


def prepare_eval_worktree(
    issue: dict[str, Any],
    repo_root_base: str,
    eval_worktree_root: str,
) -> tuple[str, dict[str, Any]]:
    base_repo_dir = repo_path(repo_root_base, issue)
    root = Path(eval_worktree_root)
    root.mkdir(parents=True, exist_ok=True)
    instance_id = str(issue["instance_id"])
    worktree_dir = root / sanitize_instance_id(instance_id)
    lock_name = sanitize_instance_id(issue["repo"].replace("/", "__"))
    lock_path = root / "_locks" / f"{lock_name}.lock"
    metadata: dict[str, Any] = {
        "base_repo_dir": base_repo_dir,
        "worktree_dir": str(worktree_dir),
        "lock_path": str(lock_path),
        "base_commit": issue["base_commit"],
    }
    with file_lock(lock_path):
        if worktree_dir.exists():
            metadata["preexisting_remove"] = _remove_eval_worktree(base_repo_dir, worktree_dir)
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
                metadata["preexisting_rmtree"] = True
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        add = run_shell(
            (
                "git worktree add --force --detach "
                f"{shlex.quote(str(worktree_dir))} {shlex.quote(str(issue['base_commit']))}"
            ),
            base_repo_dir,
            600,
        )
        metadata["worktree_add"] = add
        if add["returncode"] != 0:
            raise RuntimeError(
                f"git worktree add failed for {instance_id}: {add.get('stderr') or add.get('stdout')}"
            )
    return str(worktree_dir), metadata


def cleanup_eval_worktree(
    issue: dict[str, Any],
    repo_root_base: str,
    eval_worktree_root: str,
    worktree_dir: str,
) -> dict[str, Any]:
    root = Path(eval_worktree_root)
    base_repo_dir = repo_path(repo_root_base, issue)
    lock_name = sanitize_instance_id(issue["repo"].replace("/", "__"))
    lock_path = root / "_locks" / f"{lock_name}.lock"
    with file_lock(lock_path):
        return _remove_eval_worktree(base_repo_dir, Path(worktree_dir))


def prepare_eval_clone(
    issue: dict[str, Any],
    repo_root_base: str,
    eval_clone_root: str,
) -> tuple[str, dict[str, Any]]:
    base_repo_dir = repo_path(repo_root_base, issue)
    root = Path(eval_clone_root)
    root.mkdir(parents=True, exist_ok=True)
    instance_id = str(issue["instance_id"])
    clone_dir = root / sanitize_instance_id(instance_id)
    metadata: dict[str, Any] = {
        "base_repo_dir": base_repo_dir,
        "clone_dir": str(clone_dir),
        "base_commit": issue["base_commit"],
    }
    if (clone_dir / ".git").exists():
        metadata["cache_hit"] = True
        metadata["reset"] = git_reset_to(
            str(clone_dir), str(issue["base_commit"]), clean=True
        )
        return str(clone_dir), metadata
    if clone_dir.exists():
        shutil.rmtree(clone_dir, ignore_errors=True)
        metadata["preexisting_invalid_rmtree"] = True
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    clone = run_shell(
        f"git clone --shared --no-checkout {shlex.quote(base_repo_dir)} {shlex.quote(str(clone_dir))}",
        str(root),
        600,
    )
    metadata["clone"] = clone
    metadata["cache_hit"] = False
    if clone["returncode"] != 0:
        raise RuntimeError(
            f"git clone failed for {instance_id}: {clone.get('stderr') or clone.get('stdout')}"
        )
    checkout = run_shell(f"git checkout --force {shlex.quote(str(issue['base_commit']))}", str(clone_dir), 600)
    metadata["checkout"] = checkout
    if checkout["returncode"] != 0:
        raise RuntimeError(
            f"git checkout failed for {instance_id}: {checkout.get('stderr') or checkout.get('stdout')}"
        )
    return str(clone_dir), metadata


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_changed_paths(repo_dir: str, *, include_untracked: bool = True) -> list[str]:
    option = "--untracked-files=all" if include_untracked else "--untracked-files=no"
    result = run_shell(f"git status --porcelain=v1 {option}", repo_dir, 60)
    paths: list[str] = []
    for line in (result.get("stdout") or "").splitlines():
        value = line[3:].strip() if len(line) >= 4 else line.strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value:
            paths.append(value)
    return sorted(set(paths))


def ensure_eval_project_ready(
    issue: dict[str, Any],
    env_name: str,
    repo_dir: str,
    timeout: int,
    spec: Any | None = None,
) -> dict[str, Any]:
    """Reuse a healthy prepared environment and install only on a real miss."""

    if spec is None:
        spec = make_instance_spec(
            str(issue.get("instance_id") or ""),
            str(issue.get("repo") or ""),
            str(issue.get("version") or ""),
            str(issue.get("base_commit") or ""),
            str(issue.get("environment_setup_commit") or issue.get("base_commit") or ""),
        )
    command = icore_setup_command(spec, repo_dir)
    fingerprint = hashlib.sha256(command.encode("utf-8")).hexdigest()
    env_health = env_health_check(env_name)
    python_spec = str(env_health.get("version") or "")
    cache_key = environment_cache_key(issue, fingerprint, python_spec)
    state = read_environment_state(env_name, cache_key)
    health_before = project_health_check(
        env_name, str(issue["repo"]), repo_dir, min(timeout, 120)
    )
    cache_hit = project_cache_ready(state, repo_dir, health_before)
    result: dict[str, Any] = {
        "environment_cache_key": cache_key,
        "python_spec": python_spec,
        "cache_hit": cache_hit,
        "cache_miss": not cache_hit,
        "setup_command": command,
        "state_before": state,
        "health_before": health_before,
        "reinstalled": False,
        "setup_skipped": cache_hit,
    }
    if cache_hit:
        result["returncode"] = 0
        result["status"] = "READY"
        result["health_after"] = health_before
        return result
    lock_path = environment_lock_path(env_name, "project_setup")
    with file_lock(lock_path):
        # Another worker may have completed setup while this one waited.
        state_after_wait = read_environment_state(env_name, cache_key)
        health_after_wait = project_health_check(
            env_name, str(issue["repo"]), repo_dir, min(timeout, 120)
        )
        if project_cache_ready(state_after_wait, repo_dir, health_after_wait):
            result.update(
                {
                    "cache_hit": True,
                    "cache_miss": False,
                    "setup_skipped": True,
                    "returncode": 0,
                    "status": "READY_AFTER_LOCK",
                    "state_after_lock": state_after_wait,
                    "health_after": health_after_wait,
                }
            )
            return result
        write_environment_state(
            env_name,
            cache_key,
            {
                "status": "installing",
                "repo": issue.get("repo"),
                "version": issue.get("version"),
                "base_commit": issue.get("base_commit"),
                "setup_script_fingerprint": fingerprint,
                "python_spec": python_spec,
                "prepared_source_path": repo_dir,
                "source": "formal_eval",
            },
        )
        started = time.time()
        setup_result = run_setup_with_fallback(
            f"{conda_activate_cmd(env_name)} && {command}", repo_dir, timeout
        )
        # Setup helpers may edit tracked build metadata.  Keep built artifacts,
        # but restore production sources before the final test is applied.
        reset_result = git_reset_to(
            repo_dir, str(issue["base_commit"]), clean=False
        )
        health_after = project_health_check(
            env_name, str(issue["repo"]), repo_dir, min(timeout, 120)
        )
        ready = setup_result.get("returncode") == 0 and health_after.get("ok")
        write_environment_state(
            env_name,
            cache_key,
            {
                "status": "ready" if ready else "failed",
                "repo": issue.get("repo"),
                "version": issue.get("version"),
                "base_commit": issue.get("base_commit"),
                "setup_script_fingerprint": fingerprint,
                "python_spec": python_spec,
                "prepared_source_path": repo_dir,
                "editable_install": " -e " in f" {command} ",
                "project_health": health_after,
                "source": "formal_eval",
            },
        )
        result.update(
            {
                "returncode": 0 if ready else int(setup_result.get("returncode") or 1),
                "status": "READY" if ready else "INSTALL_FAILURE",
                "reinstalled": True,
                "setup_skipped": False,
                "prepare_time": round(time.time() - started, 4),
                "setup_execution": setup_result,
                "post_setup_reset": reset_result,
                "health_after": health_after,
            }
        )
        return result


_PROTOCOL_SUMMARY_FIELDS = (
    "candidate_repo_path",
    "direct_test_repo_path_hint",
    "command",
    "selector",
    "pytest_nodeid",
    "placement_dir",
    "runner_kind",
    "protocol_recovery_enabled",
    "seed_mutation_enabled",
    "observation_oracle_enabled",
    "strict_verifier_enabled",
)


def load_generation_summary(instance_id: str, generated_dir: str) -> dict[str, Any]:
    """Load top-level metadata, recovering legacy adaptive Top-3 omissions."""

    instance_dir = Path(generated_dir) / instance_id
    summary_path = instance_dir / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    if summary.get("candidate_repo_path") and summary.get("command"):
        return summary
    try:
        selected_seed_index = int(summary.get("selected_seed_index"))
    except (TypeError, ValueError):
        return summary
    seed_path = (
        instance_dir
        / "seed_candidates"
        / f"seed_{selected_seed_index}"
        / "summary.json"
    )
    try:
        selected = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return summary
    if not isinstance(selected, dict):
        return summary
    recovered = dict(summary)
    for field in _PROTOCOL_SUMMARY_FIELDS:
        if not recovered.get(field) and selected.get(field) not in {None, ""}:
            recovered[field] = selected[field]
    recovered["protocol_metadata_source"] = "selected_seed_summary_fallback"
    return recovered


def direct_test_relpath(
    instance_id: str,
    generated_dir: str,
    repo: str = "",
) -> str:
    summary = load_generation_summary(instance_id, generated_dir)
    recorded_path = str(
        summary.get("candidate_repo_path")
        or summary.get("direct_test_repo_path_hint")
        or ""
    ).strip()
    if recorded_path:
        candidate = Path(recorded_path)
        if not candidate.is_absolute() and ".." not in candidate.parts:
            normalized = candidate.as_posix()
            if (
                repo.split("/")[-1] == "sympy"
                and not normalized.startswith("sympy/")
            ):
                # bin/test walks only below the sympy package. A generic
                # top-level tests/ placement otherwise returns rc=0 after
                # executing zero test bodies.
                return f"sympy/tests/{candidate.name}"
            return normalized
    host_path = Path(generated_dir) / instance_id / "host_context.json"
    test_dir = "tests"
    if host_path.exists():
        host = json.loads(host_path.read_text(encoding="utf-8"))
        host_file = host.get("host_file") or ""
        if host_file:
            test_dir = os.path.dirname(host_file) or "."
    return os.path.join(test_dir, f"test_brt_{sanitize_instance_id(instance_id)}.py")


def runner_parity_info(
    instance_id: str,
    generated_dir: str,
    formal_rel_file: str,
    formal_selector: str,
    formal_command: str,
) -> dict[str, Any]:
    instance_dir = Path(generated_dir) / instance_id
    host_path = instance_dir / "host_context.json"
    host: dict[str, Any] = {}
    summary = load_generation_summary(instance_id, generated_dir)
    try:
        host = json.loads(host_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        host = {}
    generation_path = str(
        summary.get("candidate_repo_path")
        or summary.get("direct_test_repo_path_hint")
        or ""
    )
    generation_command = str(
        summary.get("command") or (summary.get("buggy_execution") or {}).get("command") or ""
    )
    generation_selector = str(summary.get("selector") or "")
    if not generation_selector and "::" in generation_command:
        generation_selector = generation_command.rsplit("::", 1)[-1].split()[0]
    warnings: list[str] = []
    same_dir = (
        bool(generation_path)
        and os.path.dirname(generation_path) == os.path.dirname(formal_rel_file)
    )
    if generation_path and not same_dir:
        warnings.append("formal eval writes BRT to a different directory than generation candidate")
    same_selector = not generation_selector or generation_selector == formal_selector
    if generation_selector and not same_selector:
        warnings.append("formal eval selector differs from generation selector")
    return {
        "generation_candidate_repo_path": generation_path,
        "formal_direct_test_repo_path": formal_rel_file,
        "generation_command": generation_command,
        "formal_command": formal_command,
        "generation_selector": generation_selector,
        "formal_selector": formal_selector,
        "protocol_metadata_source": summary.get(
            "protocol_metadata_source", "top_level_summary"
        ),
        "host_file": host.get("host_file", ""),
        "same_dir": same_dir,
        "same_selector": same_selector,
        "warnings": warnings,
    }


def first_test_selector(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    return f"{node.name}::{child.name}"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            return node.name
    return ""


def adapt_generated_test_for_runner(
    repo: str,
    code: str,
    instance_id: str,
) -> tuple[str, dict[str, Any]]:
    """Make a standalone Django function discoverable without changing it."""

    if repo.split("/")[-1] != "django":
        return code, {"applied": False, "kind": ""}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, {"applied": False, "kind": ""}
    class_tests = [
        child
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name.startswith("test")
    ]
    top_level_tests = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test")
        and not node.args.args
        and not node.args.kwonlyargs
        and node.args.vararg is None
        and node.args.kwarg is None
    ]
    if class_tests or not top_level_tests:
        return code, {"applied": False, "kind": ""}
    function_name = top_level_tests[0].name
    class_name = (
        "TestBRT"
        + "".join(
            part.capitalize()
            for part in sanitize_instance_id(instance_id).split("_")
            if part
        )
        + "Adapter"
    )
    adapted = code.rstrip() + (
        "\n\n\nimport unittest as _brt_unittest\n\n\n"
        f"class {class_name}(_brt_unittest.TestCase):\n"
        f"    def {function_name}(self):\n"
        f"        return {function_name}()\n"
    )
    return adapted, {
        "applied": True,
        "kind": "django_module_function_unittest_adapter",
        "function": function_name,
        "class": class_name,
    }


def swt_test_framework(repo: str, version: str) -> str:
    """Return the checked-in SWT-Bench test runner for a repository version."""

    project = repo.split("/")[-1]
    if project in {
        "astropy",
        "matplotlib",
        "flask",
        "xarray",
        "pylint",
        "scikit-learn",
        "requests",
    }:
        return "pytest --no-header -rA --tb=no -p no:cacheprovider"
    if project == "seaborn":
        return "pytest --no-header -rA"
    if project == "pytest":
        return "pytest -rA"
    if project == "django":
        if str(version) == "1.9":
            return "./tests/runtests.py --verbosity 2"
        return "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"
    if project == "sphinx":
        return "tox --current-env -epy39 -v --"
    if project == "sympy":
        return (
            f"PYTHONPATH={shlex.quote(str(LEGACY_SYMPY_COMPAT_PATH))}:"
            "${PYTHONPATH:-} "
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning,"
            "ignore::DeprecationWarning' "
            "bin/test -C --verbose"
        )
    raise ValueError(f"unsupported project: {repo} version={version}")


def test_command(repo: str, version: str, rel_file: str, selector: str) -> str:
    project = repo.split("/")[-1]
    nodeid = rel_file if not selector else f"{rel_file}::{selector}"
    framework = swt_test_framework(repo, version)
    if project == "django":
        label = nodeid.replace(".py", "").replace("/", ".").replace("::", ".")
        if label.startswith("tests."):
            label = label[len("tests.") :]
        if rel_file.startswith("tests/gis_tests/"):
            return (
                f"python {shlex.quote(str(DJANGO_GENERATED_TEST_RUNNER_PATH))} "
                "--settings test_sqlite --verbosity 2 "
                f"--label {shlex.quote(label)}"
            )
        return f"{framework} {shlex.quote(label)}"
    if project == "sympy":
        test_name = selector.split("::")[-1] if selector else ""
        if test_name:
            return (
                f"{framework} {shlex.quote(rel_file)} "
                f"-k {shlex.quote(test_name)}"
            )
        return f"{framework} {shlex.quote(rel_file)}"
    return f"{framework} {shlex.quote(nodeid)}"


def tdd_test_command(
    repo: str,
    version: str,
    rel_file: str,
    selector: str,
    *,
    with_coverage: bool,
) -> str:
    """Return the TDD-Bench runner for one generated test file.

    TDD-Bench uses coverage.py around the prediction on the buggy and fixed
    code states.  This is deliberately separate from ``test_command`` above,
    which is the checked-in SWT-Bench runner contract.
    """

    project = repo.split("/")[-1]
    nodeid = rel_file if not selector else f"{rel_file}::{selector}"
    coverage_prefix = "python -m coverage run " if with_coverage else ""
    if project == "django":
        framework = (
            "./tests/runtests.py --verbosity 2"
            if str(version) == "1.9"
            else "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"
        )
        label = nodeid.replace(".py", "").replace("/", ".").replace("::", ".")
        if label.startswith("tests."):
            label = label[len("tests.") :]
        # ``coverage run ./tests/runtests.py`` executes through coverage.py,
        # so Python no longer prepends ``tests/`` to sys.path as it does when
        # the runner script is invoked directly.  Django's
        # ``--settings=test_sqlite`` module lives in that directory.  Preserve
        # the benchmark runner's import contract explicitly for both covered
        # and F2P-only TDD runs.
        return (
            "PYTHONPATH=tests:${PYTHONPATH:-} "
            f"{coverage_prefix}{framework} {shlex.quote(label)}"
        )
    if project == "sphinx":
        return f"tox --current-env -epy39 -v -- {shlex.quote(nodeid)}"
    if project == "sympy":
        test_name = selector.split("::")[-1] if selector else ""
        suffix = (
            f" {shlex.quote(rel_file)} -k {shlex.quote(test_name)}"
            if test_name
            else f" {shlex.quote(rel_file)}"
        )
        return (
            f"PYTHONPATH={shlex.quote(str(LEGACY_SYMPY_COMPAT_PATH))}:"
            "${PYTHONPATH:-} "
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning,"
            "ignore::DeprecationWarning' "
            f"./bin/test -C --verbose{suffix}"
        )
    if project == "astropy" and str(version) in {
        "0.1",
        "0.2",
        "0.3",
        "0.4",
        "1.1",
        "1.2",
        "1.3",
    }:
        pytest_args = "-m pytest -rA -vv -o console_output_style=classic --tb=no"
    elif project == "seaborn":
        pytest_args = "-m pytest --no-header -rA"
    else:
        pytest_args = "-m pytest -rA"
    if with_coverage:
        return f"{coverage_prefix}--branch {pytest_args} {shlex.quote(nodeid)}"
    return f"python {pytest_args} {shlex.quote(nodeid)}"


def trace_test_command(
    command: str,
    coverage_output: str,
    repo_dir: str,
    target_lines: dict[str, list[int]],
) -> str:
    """Wrap a test command with SWT-Bench's subprocess-aware tracer."""

    include_targets = [
        re.escape(str((Path(repo_dir).resolve() / target).resolve()))
        for target in sorted(target_lines)
    ]
    include_pattern = "(?:" + "|".join(include_targets) + ")"
    trace_prefix = (
        f"python3 {shlex.quote(str(SWT_TRACE_PATH))} --count "
        f"-C {shlex.quote(coverage_output)} "
        f"--include-pattern {shlex.quote(include_pattern)}"
    )
    env_parts: list[str] = []
    raw_parts = shlex.split(command)
    parts = list(raw_parts)
    while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
        env_parts.append(parts.pop(0))
    # SWT-Bench removes this option before wrapping pytest with trace so that
    # the wrapper, rather than pytest's traceback suppression, owns execution.
    parts = [part for part in parts if part != "--tb=no"]
    env_prefix = " ".join(shlex.quote(part) for part in env_parts)
    env_prefix = (env_prefix + " ") if env_prefix else ""
    if len(parts) >= 3 and parts[0] in {"python", "python3"} and parts[1] == "-m":
        module = parts[2]
        args = " ".join(shlex.quote(part) for part in parts[3:])
        return f"{env_prefix}{trace_prefix} -m {shlex.quote(module)} {args}".strip()
    if parts and parts[0] in {"pytest", "py.test"}:
        args = " ".join(shlex.quote(part) for part in parts[1:])
        return f"{env_prefix}{trace_prefix} -m pytest {args}".strip()
    if parts and parts[0] == "unittest":
        args = " ".join(shlex.quote(part) for part in parts[1:])
        return f"{env_prefix}{trace_prefix} -m unittest {args}".strip()
    if parts and parts[0] == "tox":
        args = " ".join(shlex.quote(part) for part in parts[1:])
        return f"{env_prefix}{trace_prefix} -m tox {args}".strip()
    # Use the command after removing leading environment assignments.  Passing
    # the original command here makes the tracer interpret e.g.
    # ``PYTHONWARNINGS=...`` as the script name (the SymPy runner hits this).
    traced_target = " ".join(shlex.quote(part) for part in parts)
    return f"{env_prefix}{trace_prefix} {traced_target}".strip()


def setup_command(repo: str, version: str) -> str:
    project = repo.split("/")[-1]
    # In this workspace the environments were already created by the author-style
    # run. We only need an editable install refresh after resetting/applying.
    if project == "django":
        return "python -m pip install --ignore-installed --no-deps -e ."
    if project == "sympy":
        return "python -m pip install --ignore-installed --no-deps -e ."
    if project == "astropy":
        if str(version).startswith("1.3"):
            return "python setup.py develop --no-deps"
        return "if [ -f pyproject.toml ]; then sed -i 's/requires = \\[\"setuptools\",/requires = [\"setuptools==68.0.0\",/' pyproject.toml; fi && python -m pip install --ignore-installed --no-build-isolation --no-deps -e .\"[test]\" --verbose"
    if project == "matplotlib":
        scm_version = (
            "export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MATPLOTLIB=3.6.0.dev0 && "
            "export CFLAGS=\"${CFLAGS:-} -fno-lto\" CPPFLAGS=\"${CPPFLAGS:-} -fno-lto\" "
            "CXXFLAGS=\"${CXXFLAGS:-} -fno-lto\" LDFLAGS=\"${LDFLAGS:-} -fno-lto\" && "
        )
        if version in {"3.0", "3.1", "3.2", "3.3", "3.4"}:
            return scm_version + "python setup.py build_ext --inplace"
        return (
            scm_version
            + "python -m pip install --ignore-installed --no-build-isolation --no-deps -e ."
        )
    if project == "scikit-learn":
        if str(version) == "1.3":
            # The repository pins setuptools<60 in build isolation, whose
            # backend has no PEP 660 editable-install hook.  The formal
            # evaluator must import the checked-out (and later patched)
            # source tree, so a non-editable wheel fallback is not valid.
            return "python -m pip install --ignore-installed --no-build-isolation --no-deps -e ."
        return "python -m pip install --ignore-installed --no-deps -e ."
    if project in {"pytest", "sphinx", "xarray", "flask", "seaborn", "requests", "pylint"}:
        return "python -m pip install --ignore-installed --no-deps -e ."
    return "python -m pip install --ignore-installed --no-deps -e ."


def run_setup_with_fallback(command: str, repo_dir: str, timeout: int) -> dict[str, Any]:
    def snapshot(item: dict[str, Any]) -> dict[str, Any]:
        return dict(item)

    result = run_shell(command, repo_dir, timeout)
    log = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    attempts = [{"name": "primary", "command": command, "result": snapshot(result)}]
    if result["returncode"] != 0 and "missing the 'build_editable' hook" in log and " -e ." in command:
        fallback_command = command.replace(" -e .", " .")
        fallback = run_shell(fallback_command, repo_dir, timeout)
        attempts.append({"name": "non_editable_fallback", "command": fallback_command, "result": snapshot(fallback)})
        if fallback["returncode"] == 0:
            fallback["fallback_attempts"] = attempts
            return fallback
        result = fallback
        log = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    if (
        result["returncode"] != 0
        and (
            "uninstall-no-record-file" in log
            or ("egg-link" in log.lower() and "does not match installed location" in log.lower())
        )
        and "python -m pip install" in command
        and " -e ." in command
        and "--ignore-installed" not in command
    ):
        fallback_command = re.sub(
            r"python -m pip install(?![^&]*--ignore-installed)([^&]*\s-e\s+\.)",
            r"python -m pip install --ignore-installed --no-deps\1",
            command,
            count=1,
        )
        fallback = run_shell(fallback_command, repo_dir, timeout)
        attempts.append({"name": "ignore_installed_fallback", "command": fallback_command, "result": snapshot(fallback)})
        if fallback["returncode"] == 0:
            fallback["fallback_attempts"] = attempts
            return fallback
        result = fallback
    result["fallback_attempts"] = attempts
    return result


def classify_run(result: dict[str, Any]) -> dict[str, Any]:
    text = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    low = text.lower()
    if result.get("timeout"):
        status = "TIMEOUT"
    elif result.get("returncode") == 0 and no_tests_executed(
        result.get("stdout") or "", result.get("stderr") or ""
    ):
        status = "COLLECT_ERROR"
    elif result.get("returncode") == 0:
        status = "PASS"
    elif "syntaxerror" in low or "indentationerror" in low:
        status = "SYNTAX_ERROR"
    elif any(x in low for x in ["importerror", "modulenotfounderror", "fixture", "improperlyconfigured", "settings are not configured"]):
        status = "SETUP_ERROR"
    elif "no tests ran" in low or "not found" in low or "collected 0 items" in low:
        status = "COLLECT_ERROR"
    else:
        status = "FAIL"
    return {
        "status": status,
        "failed": status not in {"PASS"},
        "error_excerpt": "\n".join(text.splitlines()[-80:]),
    }


def runner_environment_error_category(
    command: str,
    result: dict[str, Any],
) -> str:
    """Classify failures in the test runner itself, not in the generated BRT.

    In particular, a tox backend that cannot start or a tox environment that
    lacks pytest has not executed the generated test.  Such a run must remain
    an environment failure instead of being folded into ``FIXED_FAIL``.
    """

    if "tox" not in shlex.split(command):
        return ""
    text = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    low = text.lower()
    markers = (
        "packaging backend failed",
        "failedtostart",
        "failed to start backend",
        "no module named pytest",
        "no module named 'pytest'",
        "no module named flit_core",
        "no module named 'flit_core'",
        "no module named tox_current_env",
        "unrecognized arguments: --current-env",
    )
    if not any(marker in low for marker in markers):
        return ""
    return classify_env_error(text)


def write_generated_test(repo_dir: str, rel_file: str, code: str) -> str:
    target = Path(repo_dir) / rel_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code.rstrip() + "\n", encoding="utf-8")
    return str(target)


def patch_requires_rebuild(patch_text: str) -> bool:
    build_suffixes = {
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".pyx", ".pxd", ".pxi",
    }
    for line in patch_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[len("+++ b/"):].strip()
        if Path(path).suffix.lower() in build_suffixes:
            return True
        if Path(path).name in {"setup.py", "pyproject.toml", "meson.build", "CMakeLists.txt"}:
            return True
    return False


def fixed_setup_required(
    *,
    setup: bool,
    preserve_build_artifacts: bool,
    patch_text: str,
) -> bool:
    """Return whether the fixed checkout must be prepared again.

    If a recovery reset had to clean the checkout, in-place extensions and
    generated package metadata are gone even for a pure-Python golden patch.
    Otherwise setup is only required when the patch itself changes native
    build inputs.
    """

    return bool(
        setup
        and (
            not preserve_build_artifacts
            or patch_requires_rebuild(patch_text)
        )
    )


def patch_target_lines(patch_text: str) -> dict[str, Any]:
    """Extract changed Python lines on both sides of a unified diff.

    ``buggy`` contains removed/modified line numbers in the unpatched source;
    ``fixed`` contains added/modified line numbers after the golden patch.

    This function deliberately keeps comments and blank changed lines. The
    SWT-Bench denominator is determined later from base/golden test coverage,
    not from source-text heuristics.
    """

    targets: dict[str, dict[str, set[int]]] = {"buggy": {}, "fixed": {}}
    details: dict[str, dict[str, list[dict[str, Any]]]] = {
        "buggy": {},
        "fixed": {},
    }
    old_file = ""
    new_file = ""
    old_line: int | None = None
    new_line: int | None = None
    for line in patch_text.splitlines():
        if line.startswith("--- "):
            value = line[4:].split("\t", 1)[0].strip()
            old_file = "" if value == "/dev/null" else value[2:] if value.startswith("a/") else value
            continue
        if line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0].strip()
            new_file = "" if value == "/dev/null" else value[2:] if value.startswith("b/") else value
            continue
        if line.startswith("@@"):
            match = re.search(
                r"@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@",
                line,
            )
            old_line = int(match.group(1)) if match else None
            new_line = int(match.group(3)) if match else None
            continue
        if old_line is None or new_line is None:
            continue
        if line.startswith("-") and not line.startswith("---"):
            content = line[1:]
            if old_file and Path(old_file).suffix == ".py":
                targets["buggy"].setdefault(old_file, set()).add(old_line)
                details["buggy"].setdefault(old_file, []).append(
                    {"line": old_line, "kind": "removed_or_modified", "text": content.strip()}
                )
            old_line += 1
        elif line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if new_file and Path(new_file).suffix == ".py":
                targets["fixed"].setdefault(new_file, set()).add(new_line)
                details["fixed"].setdefault(new_file, []).append(
                    {"line": new_line, "kind": "added_or_modified", "text": content.strip()}
                )
            new_line += 1
        elif line.startswith(" ") or not line:
            old_line += 1
            new_line += 1
    normalized = {
        side: {
            path: sorted(lines)
            for path, lines in side_targets.items()
            if lines
        }
        for side, side_targets in targets.items()
    }
    return {**normalized, "details": details}


def tdd_changed_lines(patch_text: str) -> dict[str, dict[str, list[int]]]:
    """Extract TDD-Bench's changed-line denominator from a golden code patch.

    The upstream TDD-Bench metric counts nonblank, non-comment removed lines
    before the golden patch and corresponding added lines after it.  Unlike
    SWT-Bench, this denominator is not restricted to dynamically executable
    Python lines and is not derived from gold/base reference tests.
    """

    targets: dict[str, dict[str, set[int]]] = {"before": {}, "after": {}}
    # Keep the checked-in TDD-Bench parser's exact text semantics, including
    # stripping before it tests the leading +/- marker.  This means a context
    # source line whose code itself begins with '+' or '-' is treated as a
    # changed line by the upstream metric.  It occurs in one of the 449 gold
    # patches, so using a cleaner unified-diff parser would change the score.
    for focus in patch_text.split("+++ b")[1:]:
        filename = focus.split("\n", 1)[0].strip().lstrip("/")
        pieces = focus.split("@@")
        segment_count = int(len(pieces) / 2)
        for index in range(segment_count):
            header = pieces[2 * index + 1].strip().split()
            if len(header) < 2:
                continue
            before_start = abs(int(header[0].split(",", 1)[0])) - 1
            after_start = abs(int(header[1].split(",", 1)[0])) - 1
            hunk_lines = pieces[2 * index + 2].split("\n")
            for side, marker, opposite, start in (
                ("before", "-", "+", before_start),
                ("after", "+", "-", after_start),
            ):
                filtered = [
                    line
                    for line in hunk_lines
                    if not line.strip().startswith(opposite)
                ]
                for offset, line in enumerate(filtered):
                    stripped = line.strip()
                    if stripped.startswith(marker * 3):
                        continue
                    if not stripped.startswith(marker):
                        continue
                    content = line.replace(marker, "").strip()
                    if not content or content.startswith("#"):
                        continue
                    targets[side].setdefault(filename, set()).add(start + offset)
    return {
        side: {
            path: sorted(lines)
            for path, lines in side_targets.items()
            if lines
        }
        for side, side_targets in targets.items()
    }


def parse_tdd_coverage_json(
    coverage_output: Path,
    changed_lines_by_file: dict[str, list[int]],
    repo_dir: str,
) -> dict[str, Any]:
    """Map coverage.py missing statements/branches to TDD changed lines.

    coverage.py's JSON ``missing_branches`` endpoints are included because the
    upstream text parser treats ``N->M`` and ``N->exit`` entries as missing
    changed lines too.  If a source file is absent from the report, upstream
    TDD-Bench observes no listed missing lines for it; we preserve that quirk
    and record it explicitly for auditability.
    """

    files: dict[str, Any] = {}
    parse_error = ""
    if coverage_output.is_file():
        try:
            payload = json.loads(coverage_output.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("files"), dict):
                files = payload["files"]
        except (OSError, json.JSONDecodeError) as exc:
            parse_error = repr(exc)

    root = Path(repo_dir).resolve()
    by_relative_path: dict[str, dict[str, Any]] = {}
    for raw_path, entry in files.items():
        if not isinstance(entry, dict):
            continue
        path = Path(str(raw_path))
        try:
            absolute = path.resolve() if path.is_absolute() else (root / path).resolve()
            relative = absolute.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            relative = str(raw_path).lstrip("./")
        by_relative_path[relative] = entry

    missing_by_file: dict[str, list[int]] = {}
    missed_changed_by_file: dict[str, list[int]] = {}
    absent_from_report: list[str] = []
    for path, changed in changed_lines_by_file.items():
        entry = by_relative_path.get(path)
        missing: set[int] = set()
        if entry is None:
            absent_from_report.append(path)
        else:
            for value in entry.get("missing_lines") or []:
                try:
                    missing.add(int(value))
                except (TypeError, ValueError):
                    continue
            for branch in entry.get("missing_branches") or []:
                if not isinstance(branch, (list, tuple)):
                    continue
                for value in branch:
                    try:
                        line_number = int(value)
                    except (TypeError, ValueError):
                        continue
                    if line_number > 0:
                        missing.add(line_number)
        missing_by_file[path] = sorted(missing)
        missed_changed_by_file[path] = sorted(set(changed) & missing)

    total_changed = sum(len(lines) for lines in changed_lines_by_file.values())
    total_missed = sum(len(lines) for lines in missed_changed_by_file.values())
    return {
        "status": (
            "OK"
            if coverage_output.is_file() and not parse_error
            else "COVERAGE_JSON_PARSE_ERROR"
            if parse_error
            else "NO_COVERAGE_DATA"
        ),
        "changed_lines_by_file": changed_lines_by_file,
        "missing_lines_by_file": missing_by_file,
        "missed_changed_lines_by_file": missed_changed_by_file,
        "files_absent_from_coverage_report": sorted(absent_from_report),
        "coverage_files_found": len(by_relative_path),
        "total_changed": total_changed,
        "total_missed": total_missed,
        "covered_changed": total_changed - total_missed,
        "coverage": (
            (total_changed - total_missed) / total_changed
            if total_changed
            else 0.0
        ),
        "parse_error": parse_error,
    }


def parse_patch_coverage(
    coverage_output: Path,
    target_lines: dict[str, list[int]],
    repo_dir: str,
) -> dict[str, Any]:
    """Parse SWT-Bench JSON-lines coverage, including child-process records."""

    root = Path(repo_dir).resolve()
    target_by_absolute_path = {
        str((root / target).resolve()): target for target in target_lines
    }
    merged_counts: dict[str, dict[int, int]] = {
        target: {} for target in target_lines
    }
    matched_source_files: set[str] = set()
    if coverage_output.is_file():
        for raw_line in coverage_output.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            for source_path, line_counts in record.items():
                if not isinstance(line_counts, dict):
                    continue
                try:
                    normalized_source = str(Path(source_path).resolve())
                except (OSError, RuntimeError):
                    normalized_source = str(source_path)
                target = target_by_absolute_path.get(normalized_source)
                if target is None:
                    continue
                matched_source_files.add(normalized_source)
                counts = merged_counts[target]
                for raw_line_number, raw_count in line_counts.items():
                    try:
                        line_number = int(raw_line_number)
                        count = int(raw_count)
                    except (TypeError, ValueError):
                        continue
                    counts[line_number] = counts.get(line_number, 0) + count

    covered: dict[str, list[int]] = {}
    executable: dict[str, list[int]] = {}
    hit_counts: dict[str, dict[str, int]] = {}
    for target, lines in target_lines.items():
        line_set = set(lines)
        counts = {
            line: count
            for line, count in merged_counts.get(target, {}).items()
            if line in line_set
        }
        hits = {line for line, count in counts.items() if count > 0}
        executable_lines = set(counts)
        covered[target] = sorted(hits)
        executable[target] = sorted(executable_lines)
        hit_counts[target] = {str(line): counts[line] for line in sorted(counts)}
    target_count = sum(len(lines) for lines in target_lines.values())
    covered_count = sum(len(lines) for lines in covered.values())
    return {
        "status": "OK" if coverage_output.is_file() else "MISSING_COVERAGE_OUTPUT",
        "target_lines_by_file": target_lines,
        "covered_lines_by_file": covered,
        "executable_lines_by_file": executable,
        "hit_counts_by_file": hit_counts,
        "target_line_count": target_count,
        "covered_line_count": covered_count,
        "patch_covered": bool(target_count and covered_count),
        "patch_line_coverage": covered_count / target_count if target_count else 0.0,
        "uncovered_lines_by_file": {
            path: sorted(set(lines) - set(covered.get(path, [])))
            for path, lines in target_lines.items()
        },
        "coverage_files_found": len(matched_source_files),
    }


def collect_patch_side_coverage(
    instance_id: str,
    side: str,
    target_lines: dict[str, list[int]],
    command: str,
    repo_dir: str,
    env_name: str,
    pythonpath: str,
    timeout: int,
) -> dict[str, Any]:
    if not target_lines:
        return {
            "status": "NO_PATCH_TARGET_LINES",
            "target_lines_by_file": {},
            "covered_lines_by_file": {},
            "executable_lines_by_file": {},
            "hit_counts_by_file": {},
            "target_line_count": 0,
            "covered_line_count": 0,
            "patch_covered": False,
            "patch_line_coverage": 0.0,
            "coverage_files_found": 0,
        }
    coverage_dir = Path(repo_dir) / ".brt_patch_coverage" / (
        sanitize_instance_id(instance_id) + "_" + side
    )
    if coverage_dir.exists():
        shutil.rmtree(coverage_dir)
    coverage_dir.mkdir(parents=True, exist_ok=True)
    coverage_output = coverage_dir / "coverage.cover"
    traced_command = trace_test_command(
        command,
        str(coverage_output),
        repo_dir,
        target_lines,
    )
    full_command = (
        f"{conda_activate_cmd(env_name)} && export PYTHONPATH={pythonpath}:$PYTHONPATH "
        f"&& {traced_command}"
    )
    try:
        run = run_shell(full_command, repo_dir, timeout)
        coverage = parse_patch_coverage(coverage_output, target_lines, repo_dir)
    finally:
        shutil.rmtree(coverage_dir, ignore_errors=True)
    coverage["command"] = traced_command
    coverage["side"] = side
    coverage["run"] = run
    coverage["instrumentation"] = "vendored_swt_bench_subprocess_aware_trace"
    coverage["trace_source"] = "logic-star-ai/swt-bench src/auxillary_src/trace.py"
    coverage["trace_sha256"] = SWT_TRACE_SHA256
    coverage["execution_status"] = (
        "TIMEOUT"
        if run.get("timeout")
        else "PASS"
        if run.get("returncode") == 0
        else "ERROR"
    )
    coverage["execution_returncode"] = run.get("returncode")
    coverage["execution_timeout"] = bool(run.get("timeout"))
    if coverage["status"] == "MISSING_COVERAGE_OUTPUT":
        # SWT-Bench's get_coverage_eval() returns {} whenever no coverage dump
        # is present.  That is an observed empty set for every one of the six
        # views, including tests that fail before reaching production code.
        coverage["status"] = "NO_COVERAGE_FILES"
        coverage["swt_empty_coverage"] = True
        coverage["swt_empty_coverage_reason"] = coverage["execution_status"]
    elif coverage["status"] == "OK" and not coverage.get("coverage_files_found"):
        coverage["status"] = "NO_COVERAGE_FILES"
        coverage["swt_empty_coverage"] = True
        coverage["swt_empty_coverage_reason"] = "NO_TARGET_FILE_RECORD"
    return coverage


def ensure_tdd_coverage_tools(
    repo: str,
    env_name: str,
    repo_dir: str,
    timeout: int,
) -> dict[str, Any]:
    """Install TDD-Bench coverage tooling only in the disposable eval env."""

    project = repo.split("/")[-1]
    if project == "sympy":
        return {
            "returncode": 0,
            "status": "SYMPY_COVERAGE_EXEMPT",
            "installed": False,
        }
    modules = ["coverage"] + (["pytest_cov"] if project == "sphinx" else [])
    packages = ["coverage"] + (["pytest-cov"] if project == "sphinx" else [])
    imports = "; ".join(f"import {module}" for module in modules)
    check = run_shell(
        f"{conda_activate_cmd(env_name)} && python -c {shlex.quote(imports)}",
        repo_dir,
        min(timeout, 180),
    )
    if check.get("returncode") == 0:
        return {
            "returncode": 0,
            "status": "AVAILABLE",
            "installed": False,
            "check": check,
        }
    install = run_shell(
        f"{conda_activate_cmd(env_name)} && python -m pip install "
        + " ".join(shlex.quote(package) for package in packages),
        repo_dir,
        min(timeout, 600),
    )
    return {
        "returncode": int(install.get("returncode") or 0),
        "status": "INSTALLED" if install.get("returncode") == 0 else "INSTALL_FAILED",
        "installed": install.get("returncode") == 0,
        "check": check,
        "install": install,
    }


def ensure_tdd_optional_runtime_tools(
    repo: str,
    generated_code: str,
    env_name: str,
    repo_dir: str,
    timeout: int,
) -> dict[str, Any]:
    """Provision external tools explicitly required by one generated TDD test."""

    project = repo.split("/")[-1]
    if project != "matplotlib" or not re.search(
        r"(?:text\.usetex|usetex\s*[:=])", generated_code
    ):
        return {
            "returncode": 0,
            "status": "NOT_REQUIRED",
            "required_tools": [],
        }
    check = run_shell(
        f"{conda_activate_cmd(env_name)} && command -v latex && command -v dvipng",
        repo_dir,
        min(max(timeout, 120), 300),
    )
    if check.get("returncode") == 0:
        return {
            "returncode": 0,
            "status": "AVAILABLE",
            "required_tools": ["latex", "dvipng"],
            "check": check,
        }
    install = run_shell(
        f"{conda_activate_cmd(env_name)} && "
        "conda install -y -c conda-forge texlive-core",
        repo_dir,
        max(timeout, 3600),
    )
    verify = run_shell(
        f"{conda_activate_cmd(env_name)} && command -v latex && command -v dvipng",
        repo_dir,
        min(max(timeout, 120), 300),
    )
    ready = install.get("returncode") == 0 and verify.get("returncode") == 0
    return {
        "returncode": 0 if ready else 1,
        "status": "INSTALLED" if ready else "INSTALL_FAILED",
        "required_tools": ["latex", "dvipng"],
        "check": check,
        "install": install,
        "verify": verify,
    }


def collect_tdd_side_coverage(
    instance_id: str,
    side: str,
    changed_lines_by_file: dict[str, list[int]],
    command: str,
    repo_dir: str,
    env_name: str,
    pythonpath: str,
    timeout: int,
    *,
    repo: str,
) -> dict[str, Any]:
    """Run one TDD-Bench pre/post test view and read coverage.py JSON."""

    if repo.split("/")[-1] == "sympy":
        full_command = (
            f"{conda_activate_cmd(env_name)} && "
            f"export PYTHONPATH={shlex.quote(pythonpath)}:$PYTHONPATH && {command}"
        )
        run = run_shell(full_command, repo_dir, timeout)
        return {
            "status": "SYMPY_COVERAGE_EXEMPT",
            "side": side,
            "command": command,
            "run": run,
            "changed_lines_by_file": changed_lines_by_file,
            "missed_changed_lines_by_file": {},
            "total_changed": sum(map(len, changed_lines_by_file.values())),
            "total_missed": 0,
            "covered_changed": 0,
            "coverage": None,
            "instrumentation": "none_sympy_tdd_bench_exemption",
        }

    coverage_dir = Path(repo_dir) / ".brt_tdd_coverage" / (
        sanitize_instance_id(instance_id) + "_" + side
    )
    if coverage_dir.exists():
        shutil.rmtree(coverage_dir)
    coverage_dir.mkdir(parents=True, exist_ok=True)
    data_file = coverage_dir / ".coverage"
    json_file = coverage_dir / "coverage.json"
    sphinx_env = (
        "export PYTEST_ADDOPTS='--cov=sphinx --cov-report=term-missing' && "
        if repo.split("/")[-1] == "sphinx"
        else ""
    )
    # Preserve the test command's exit status after exporting coverage data.
    # A failing buggy-side test is an expected and useful outcome.
    full_command = (
        f"{conda_activate_cmd(env_name)} && "
        f"export PYTHONPATH={shlex.quote(pythonpath)}:$PYTHONPATH && "
        f"export COVERAGE_FILE={shlex.quote(str(data_file))} && "
        f"{sphinx_env}rm -f {shlex.quote(str(data_file))} "
        f"{shlex.quote(str(json_file))} && "
        f"{command}; test_rc=$?; "
        f"python -m coverage json -o {shlex.quote(str(json_file))}; "
        "coverage_rc=$?; "
        "echo BRT_TDD_COVERAGE_REPORT_RC=$coverage_rc >&2; "
        "exit $test_rc"
    )
    try:
        run = run_shell(full_command, repo_dir, timeout)
        coverage = parse_tdd_coverage_json(
            json_file, changed_lines_by_file, repo_dir
        )
    finally:
        shutil.rmtree(coverage_dir, ignore_errors=True)
    coverage.update(
        {
            "side": side,
            "command": command,
            "run": run,
            "instrumentation": "tdd_bench_coverage_py_changed_line_report",
            "execution_status": (
                "TIMEOUT"
                if run.get("timeout")
                else "PASS"
                if run.get("returncode") == 0
                else "ERROR"
            ),
            "execution_returncode": run.get("returncode"),
            "execution_timeout": bool(run.get("timeout")),
        }
    )
    return coverage


def combine_tdd_coverage(
    instance_id: str,
    targets: dict[str, dict[str, list[int]]],
    before: dict[str, Any],
    after: dict[str, Any],
    buggy: dict[str, Any],
    fixed: dict[str, Any],
) -> dict[str, Any]:
    """Compute the per-instance score used by TDD-Bench."""

    total_changed = int(before.get("total_changed") or 0) + int(
        after.get("total_changed") or 0
    )
    total_missed = int(before.get("total_missed") or 0) + int(
        after.get("total_missed") or 0
    )
    cov_score = (
        (total_changed - total_missed) / total_changed if total_changed else 0.0
    )
    fail_before = int(bool(buggy.get("failed")))
    pass_after = int(not bool(fixed.get("failed")))
    sympy_exempt = "sympy" in instance_id.lower()
    final_score = (
        float(fail_before * pass_after)
        if sympy_exempt
        else cov_score * fail_before * pass_after
    )
    return {
        "status": (
            "SYMPY_COVERAGE_EXEMPT"
            if sympy_exempt
            else "NO_CHANGED_LINES"
            if not total_changed
            else "OK"
        ),
        "metric_family": "tdd_bench_changed_line_coverage",
        "coverage_schema_version": 1,
        "denominator_source": (
            "nonblank_noncomment_removed_lines_before_plus_added_lines_after"
        ),
        "targets": targets,
        "before": before,
        "after": after,
        "total_changed": total_changed,
        "total_missed": total_missed,
        "covered_changed": total_changed - total_missed,
        "cov_score": cov_score,
        "fail_before": fail_before,
        "pass_after": pass_after,
        "f2p_gate": fail_before * pass_after,
        "f2p_gate_source": "brt_formal_buggy_fixed_test_classification",
        "sympy_coverage_exempt": sympy_exempt,
        "final_score": final_score,
        "resolved": final_score > 0,
    }


def aggregate_tdd_bench_score(
    results: dict[str, dict[str, Any]],
    *,
    coverage_enabled: bool,
) -> dict[str, Any]:
    """Average TDD final_score over the complete requested dataset."""

    denominator = len(results)
    per_instance: dict[str, float] = {}
    zeroed: dict[str, str] = {}
    resolved: list[str] = []
    for instance_id, result in results.items():
        coverage = (
            result.get("tdd_coverage")
            if isinstance(result.get("tdd_coverage"), dict)
            else {}
        )
        if coverage_enabled and "final_score" in coverage:
            value = float(coverage.get("final_score") or 0.0)
        else:
            value = 0.0
            zeroed[instance_id] = str(
                result.get("status") or "MISSING_TDD_COVERAGE"
            )
        per_instance[instance_id] = value
        if value > 0:
            resolved.append(instance_id)
    numerator = sum(per_instance.values())
    value = numerator / denominator if denominator else 0.0
    return {
        "valid": bool(coverage_enabled and denominator),
        "value": value if coverage_enabled else None,
        "percent": value * 100 if coverage_enabled else None,
        "numerator": numerator,
        "denominator": denominator,
        "resolved_instances": len(resolved),
        "resolved_ids": sorted(resolved),
        "zeroed_instances": dict(sorted(zeroed.items())),
        "per_instance": dict(sorted(per_instance.items())),
        "invalid_reason": "" if coverage_enabled else "coverage was disabled",
    }


def patch_paths_from_patch(test_patch: str) -> list[str]:
    """Return every target path so untracked fixtures can be cleaned safely."""

    paths: list[str] = []
    for path in re.findall(r"diff --git a/.* b/(.*)", test_patch):
        path = path.strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def test_directives_from_patch(test_patch: str) -> list[str]:
    # Keep the same suffix filter (including its historical ``csv`` spelling)
    # as SWT-Bench's get_test_directives().  Cleanup deliberately uses the
    # unfiltered patch path list instead.
    return [
        path
        for path in patch_paths_from_patch(test_patch)
        if not any(path.endswith(ext) for ext in SWT_NON_TEST_EXTENSIONS)
    ]


def test_command_for_directives(repo: str, version: str, directives: list[str]) -> str:
    if not directives:
        raise ValueError("golden test patch contains no executable test directives")
    project = repo.split("/")[-1]
    framework = swt_test_framework(repo, version)
    if project == "django":
        labels = []
        for path in directives:
            label = path[:-3] if path.endswith(".py") else path
            label = label[len("tests/"):] if label.startswith("tests/") else label
            labels.append(label.replace("/", "."))
        return framework + " " + " ".join(
            shlex.quote(label) for label in labels
        )
    quoted = " ".join(shlex.quote(path) for path in directives)
    return f"{framework} {quoted}"


def _line_set(mapping: Any, path: str) -> set[int]:
    if not isinstance(mapping, dict):
        return set()
    values = mapping.get(path) or []
    return {int(value) for value in values}


def _hit_count(coverage: dict[str, Any], path: str, line: int) -> int:
    all_counts = coverage.get("hit_counts_by_file") or {}
    path_counts = all_counts.get(path) if isinstance(all_counts, dict) else {}
    if not isinstance(path_counts, dict):
        return 0
    return int(path_counts.get(str(line), path_counts.get(line, 0)) or 0)


def _official_executable_lines(
    target_lines: dict[str, list[int]],
    coverage_results: list[dict[str, Any]],
) -> dict[str, list[int]]:
    output: dict[str, list[int]] = {}
    for path, lines in target_lines.items():
        executable: set[int] = set()
        for coverage in coverage_results:
            executable.update(_line_set(coverage.get("executable_lines_by_file"), path))
        selected = sorted(set(int(line) for line in lines) & executable)
        if selected:
            output[path] = selected
    return output


def _coverage_for_lines(
    lines_by_file: dict[str, list[int]],
    coverage: dict[str, Any],
) -> tuple[dict[str, list[int]], dict[str, dict[str, int]]]:
    covered: dict[str, list[int]] = {}
    counts: dict[str, dict[str, int]] = {}
    for path, lines in lines_by_file.items():
        path_counts = {str(line): _hit_count(coverage, path, line) for line in lines}
        counts[path] = path_counts
        covered[path] = [line for line in lines if path_counts[str(line)] > 0]
    return covered, counts


def _flatten_line_map(lines_by_file: dict[str, list[int]]) -> list[dict[str, Any]]:
    return [
        {"path": path, "line": line}
        for path, lines in sorted(lines_by_file.items())
        for line in sorted(lines)
    ]


def combine_patch_coverage(
    targets: dict[str, Any],
    pred_pre: dict[str, Any],
    pred_post: dict[str, Any],
    gold_pre: dict[str, Any],
    gold_post: dict[str, Any],
    base_pre: dict[str, Any],
    base_post: dict[str, Any],
    gold_base_pre: dict[str, Any],
    gold_base_post: dict[str, Any],
) -> dict[str, Any]:
    """Compute SWT-Bench coverage plus its independent gold denominator.

    The model score uses SWT-Bench's six views. ``gold_base_pre/post`` are the
    two additional no-gold-test views corresponding to the separate gold run
    used by SWT-Bench reporting to fix the macro-average denominator.
    """

    executable_removed = _official_executable_lines(
        targets.get("buggy") or {}, [gold_pre, base_pre]
    )
    executable_added = _official_executable_lines(
        targets.get("fixed") or {}, [gold_post, base_post]
    )
    pred_removed, pred_removed_counts = _coverage_for_lines(executable_removed, pred_pre)
    pred_added, pred_added_counts = _coverage_for_lines(executable_added, pred_post)
    gold_removed, gold_removed_counts = _coverage_for_lines(executable_removed, gold_pre)
    gold_added, gold_added_counts = _coverage_for_lines(executable_added, gold_post)

    # Match the official SWT-Bench grading implementation: both removed and
    # added-line deltas use base_post as the base coverage reference.
    base_removed, base_removed_counts = _coverage_for_lines(executable_removed, base_post)
    base_added, base_added_counts = _coverage_for_lines(executable_added, base_post)
    delta_removed: dict[str, list[int]] = {}
    delta_added: dict[str, list[int]] = {}
    for path, lines in executable_removed.items():
        delta_removed[path] = [
            line for line in lines
            if pred_removed_counts[path][str(line)] - base_removed_counts[path][str(line)] > 0
        ]
    for path, lines in executable_added.items():
        delta_added[path] = [
            line for line in lines
            if pred_added_counts[path][str(line)] - base_added_counts[path][str(line)] > 0
        ]
    official_delta_gold_removed: dict[str, list[int]] = {}
    official_delta_gold_added: dict[str, list[int]] = {}
    for path, lines in executable_removed.items():
        official_delta_gold_removed[path] = [
            line for line in lines
            if gold_removed_counts[path][str(line)]
            - base_removed_counts[path][str(line)]
            > 0
        ]
    for path, lines in executable_added.items():
        official_delta_gold_added[path] = [
            line for line in lines
            if gold_added_counts[path][str(line)]
            - base_added_counts[path][str(line)]
            > 0
        ]

    # Reconstruct the independent gold prediction run used as SWT-Bench's
    # paper-level macro denominator.  Its baseline directives come from the
    # golden test patch, not from the model prediction.
    gold_executable_removed = _official_executable_lines(
        targets.get("buggy") or {}, [gold_pre, gold_base_pre]
    )
    gold_executable_added = _official_executable_lines(
        targets.get("fixed") or {}, [gold_post, gold_base_post]
    )
    _, gold_reference_removed_counts = _coverage_for_lines(
        gold_executable_removed, gold_pre
    )
    _, gold_reference_added_counts = _coverage_for_lines(
        gold_executable_added, gold_post
    )
    _, gold_base_removed_counts = _coverage_for_lines(
        gold_executable_removed, gold_base_post
    )
    _, gold_base_added_counts = _coverage_for_lines(
        gold_executable_added, gold_base_post
    )
    gold_reference_delta_removed: dict[str, list[int]] = {}
    gold_reference_delta_added: dict[str, list[int]] = {}
    for path, lines in gold_executable_removed.items():
        gold_reference_delta_removed[path] = [
            line for line in lines
            if gold_reference_removed_counts[path][str(line)]
            - gold_base_removed_counts[path][str(line)]
            > 0
        ]
    for path, lines in gold_executable_added.items():
        gold_reference_delta_added[path] = [
            line for line in lines
            if gold_reference_added_counts[path][str(line)]
            - gold_base_added_counts[path][str(line)]
            > 0
        ]

    executable_count = sum(map(len, executable_removed.values())) + sum(
        map(len, executable_added.values())
    )
    pred_count = sum(map(len, pred_removed.values())) + sum(map(len, pred_added.values()))
    gold_count = sum(map(len, gold_removed.values())) + sum(map(len, gold_added.values()))
    base_count = sum(map(len, base_removed.values())) + sum(map(len, base_added.values()))
    delta_pred_count = sum(map(len, delta_removed.values())) + sum(map(len, delta_added.values()))
    official_delta_gold_count = sum(
        map(len, official_delta_gold_removed.values())
    ) + sum(map(len, official_delta_gold_added.values()))
    gold_executable_count = sum(map(len, gold_executable_removed.values())) + sum(
        map(len, gold_executable_added.values())
    )
    gold_reference_delta_count = sum(
        map(len, gold_reference_delta_removed.values())
    ) + sum(
        map(len, gold_reference_delta_added.values())
    )
    model_input_statuses = {
        name: (
            "TIMEOUT"
            if value.get("execution_timeout")
            else str(value.get("status") or "UNKNOWN")
        )
        for name, value in {
            "pred_pre": pred_pre,
            "pred_post": pred_post,
            "gold_pre": gold_pre,
            "gold_post": gold_post,
            "base_pre": base_pre,
            "base_post": base_post,
        }.items()
    }
    gold_reference_input_statuses = {
        name: (
            "TIMEOUT"
            if value.get("execution_timeout")
            else str(value.get("status") or "UNKNOWN")
        )
        for name, value in {
            "gold_pre": gold_pre,
            "gold_post": gold_post,
            "gold_base_pre": gold_base_pre,
            "gold_base_post": gold_base_post,
        }.items()
    }
    valid_input_statuses = {"OK", "NO_PATCH_TARGET_LINES", "NO_COVERAGE_FILES"}
    invalid_inputs = {
        name: status
        for name, status in model_input_statuses.items()
        # No repository ``.cover`` file is a valid zero-coverage observation,
        # not a missing view. This is expected for base references after the
        # generated test is removed and for tests that stop before entering a
        # patched source file.
        if status not in valid_input_statuses
    }
    invalid_gold_reference_inputs = {
        name: status
        for name, status in gold_reference_input_statuses.items()
        if status not in valid_input_statuses
    }
    status = "OK" if executable_count else "NO_EXECUTABLE_PATCH_LINES"
    if invalid_inputs:
        status = "INCOMPLETE_REFERENCE_COVERAGE"
    gold_reference_status = (
        "OK" if gold_executable_count else "NO_EXECUTABLE_GOLD_PATCH_LINES"
    )
    if invalid_gold_reference_inputs:
        gold_reference_status = "INCOMPLETE_GOLD_REFERENCE_COVERAGE"
    gold_applicable = gold_reference_status == "OK" and gold_executable_count > 0
    paper_metric_eligible = status == "OK" and gold_applicable
    return {
        "coverage_schema_version": 3,
        "status": status,
        "definition": (
            "SWT-Bench official six-view Patch Coverage: executable removed lines are "
            "identified by gold_pre/base_pre, executable added lines by gold_post/base_post; "
            "coverage_pred is the fraction hit by one final_test.py on buggy/fixed sides."
        ),
        "algorithm": "swt_bench_six_view_with_independent_gold_denominator",
        "compatibility_target": "logic-star-ai/swt-bench grading.py",
        "coverage_instrumentation": "vendored_swt_bench_subprocess_aware_trace",
        "coverage_instrumentation_sha256": SWT_TRACE_SHA256,
        "prediction_coverage_scope": "complete_final_test_file",
        "gold_denominator_source": "independent_gold_test_and_gold_baseline_views",
        "coverage_delta_gold_source": "official_model_six_view_denominator",
        "removed_line_baseline_view": "base_post",
        "added_line_baseline_view": "base_post",
        "paper_metric_eligible": paper_metric_eligible,
        "gold_applicable": gold_applicable,
        "gold_reference_status": gold_reference_status,
        "gold_reference_target_line_count": gold_executable_count,
        "targets": targets,
        "input_statuses": model_input_statuses,
        "invalid_inputs": invalid_inputs,
        "gold_reference_input_statuses": gold_reference_input_statuses,
        "invalid_gold_reference_inputs": invalid_gold_reference_inputs,
        "buggy": pred_pre,
        "fixed": pred_post,
        "pred_pre": pred_pre,
        "pred_post": pred_post,
        "gold_pre": gold_pre,
        "gold_post": gold_post,
        "base_pre": base_pre,
        "base_post": base_post,
        "gold_base_pre": gold_base_pre,
        "gold_base_post": gold_base_post,
        "executable_removed_lines": _flatten_line_map(executable_removed),
        "executable_added_lines": _flatten_line_map(executable_added),
        "covered_removed_lines": _flatten_line_map(pred_removed),
        "covered_added_lines": _flatten_line_map(pred_added),
        "uncovered_removed_lines": _flatten_line_map({
            path: sorted(set(lines) - set(pred_removed.get(path, [])))
            for path, lines in executable_removed.items()
        }),
        "uncovered_added_lines": _flatten_line_map({
            path: sorted(set(lines) - set(pred_added.get(path, [])))
            for path, lines in executable_added.items()
        }),
        "executable_removed_count": sum(map(len, executable_removed.values())),
        "executable_added_count": sum(map(len, executable_added.values())),
        "target_line_count": executable_count,
        "covered_line_count": pred_count,
        "patch_covered": bool(executable_count and pred_count),
        "patch_line_coverage": pred_count / executable_count if executable_count else None,
        "coverage_pred": pred_count / executable_count if executable_count else None,
        "coverage_gold": gold_count / executable_count if executable_count else None,
        "coverage_base": base_count / executable_count if executable_count else None,
        "coverage_delta_pred": delta_pred_count / executable_count if executable_count else None,
        "coverage_delta_gold": (
            official_delta_gold_count / executable_count
            if executable_count and not invalid_inputs
            else None
        ),
        "gold_reference_coverage_delta": (
            gold_reference_delta_count / gold_executable_count
            if gold_executable_count and not invalid_gold_reference_inputs
            else None
        ),
        "delta_covered_removed_lines": _flatten_line_map(delta_removed),
        "delta_covered_added_lines": _flatten_line_map(delta_added),
        "delta_gold_removed_lines": _flatten_line_map(
            official_delta_gold_removed
        ),
        "delta_gold_added_lines": _flatten_line_map(official_delta_gold_added),
        "gold_reference_delta_removed_lines": _flatten_line_map(
            gold_reference_delta_removed
        ),
        "gold_reference_delta_added_lines": _flatten_line_map(
            gold_reference_delta_added
        ),
    }


def aggregate_delta_change_coverage(
    results: dict[str, dict[str, Any]],
    *,
    coverage_enabled: bool,
) -> dict[str, Any]:
    """Aggregate paper-facing SWT-Bench Delta Mean Change Coverage.

    SWT-Bench divides the sum of model ``coverage_delta_pred`` values by the
    number of instances whose separate gold run has a defined
    ``coverage_delta_gold``. Gold-unavailable instances are excluded; a model
    run that is unavailable for a gold-applicable instance contributes zero.
    Both decisions are returned explicitly for auditability.
    """

    eligible_values: list[float] = []
    eligible_ids: list[str] = []
    excluded_no_executable: list[str] = []
    excluded_gold_unavailable: dict[str, str] = {}
    zeroed_model_instances: dict[str, str] = {}
    per_instance: dict[str, float] = {}

    if not coverage_enabled:
        return {
            "valid": False,
            "value": None,
            "percent": None,
            "numerator": 0.0,
            "denominator": 0,
            "eligible_ids": [],
            "excluded_no_executable_ids": [],
            "excluded_gold_unavailable": {},
            "zeroed_model_instances": {},
            "invalid_instances": {},
            "per_instance": {},
            "invalid_reason": "patch coverage was disabled",
        }

    for instance_id, result in results.items():
        coverage = (
            result.get("patch_coverage")
            if isinstance(result.get("patch_coverage"), dict)
            else {}
        )
        status = str(coverage.get("status") or "MISSING_PATCH_COVERAGE")
        gold_reference_status = str(
            coverage.get("gold_reference_status") or "MISSING_GOLD_REFERENCE"
        )
        if (
            gold_reference_status == "NO_EXECUTABLE_GOLD_PATCH_LINES"
            and not coverage.get("invalid_gold_reference_inputs")
        ):
            excluded_no_executable.append(instance_id)
            continue
        schema_version = int(coverage.get("coverage_schema_version") or 0)
        gold_reference_delta = (
            coverage.get("gold_reference_coverage_delta")
            if schema_version >= 3
            else coverage.get("coverage_delta_gold")
        )
        if not coverage.get("gold_applicable") or gold_reference_delta is None:
            excluded_gold_unavailable[instance_id] = gold_reference_status
            continue
        delta = coverage.get("coverage_delta_pred")
        if (
            not coverage.get("paper_metric_eligible")
            or status != "OK"
            or delta is None
        ):
            value = 0.0
            zeroed_model_instances[instance_id] = status
        else:
            value = float(delta)
        eligible_ids.append(instance_id)
        eligible_values.append(value)
        per_instance[instance_id] = value

    valid = bool(eligible_values)
    numerator = sum(eligible_values)
    value = numerator / len(eligible_values) if valid else None
    if not eligible_values:
        invalid_reason = "no gold-applicable instances with executable patch lines"
    else:
        invalid_reason = ""
    return {
        "valid": valid,
        "value": value,
        "percent": value * 100 if value is not None else None,
        "numerator": numerator,
        "denominator": len(eligible_values),
        "eligible_ids": sorted(eligible_ids),
        "excluded_no_executable_ids": sorted(excluded_no_executable),
        "excluded_gold_unavailable": dict(
            sorted(excluded_gold_unavailable.items())
        ),
        "zeroed_model_instances": dict(sorted(zeroed_model_instances.items())),
        "invalid_instances": {},
        "per_instance": dict(sorted(per_instance.items())),
        "invalid_reason": invalid_reason,
    }


def _remove_untracked_test_path(repo_dir: str, rel_file: str) -> None:
    target = Path(repo_dir) / rel_file
    if target.is_file() or target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def _remove_untracked_patch_paths(repo_dir: str, rel_files: list[str]) -> list[str]:
    """Remove files created by a reference test patch, preserving tracked tests."""

    removed: list[str] = []
    for rel_file in rel_files:
        tracked = run_shell(
            f"git ls-files --error-unmatch -- {shlex.quote(rel_file)}",
            repo_dir,
            60,
        )
        if tracked.get("returncode") == 0:
            continue
        target = Path(repo_dir) / rel_file
        if target.is_file() or target.is_symlink():
            target.unlink()
            removed.append(rel_file)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(rel_file)
    return removed


def collect_reference_patch_coverage(
    issue: dict[str, Any],
    instance_id: str,
    state: str,
    target_lines: dict[str, list[int]],
    repo_dir: str,
    env_name: str,
    pythonpath: str,
    timeout: int,
    final_test_relpath: str,
    prediction_command: str,
) -> dict[str, Any]:
    """Collect base/golden-test coverage for one code state.

    These runs are denominator/reference runs for Patch Coverage only. They do
    not participate in the F2P status and never enter generation or ranking.
    """

    test_patch = str(issue.get("test_patch") or "")
    if not test_patch.strip():
        missing = {
            "status": "MISSING_GOLD_TEST_PATCH",
            "target_lines_by_file": target_lines,
            "covered_lines_by_file": {},
            "executable_lines_by_file": {},
            "hit_counts_by_file": {},
            "target_line_count": sum(map(len, target_lines.values())),
            "covered_line_count": 0,
        }
        return {
            "base": dict(missing),
            "gold_base": dict(missing),
            "gold": dict(missing),
            "audit": {},
        }
    all_test_patch_paths = patch_paths_from_patch(test_patch)
    directives = test_directives_from_patch(test_patch)
    try:
        gold_command = test_command_for_directives(
            str(issue["repo"]), str(issue["version"]), directives
        )
    except ValueError as exc:
        invalid = {
            "status": "INVALID_GOLD_TEST_DIRECTIVES",
            "error": str(exc),
            "target_lines_by_file": target_lines,
            "covered_lines_by_file": {},
            "executable_lines_by_file": {},
            "hit_counts_by_file": {},
            "target_line_count": sum(map(len, target_lines.values())),
            "covered_line_count": 0,
        }
        return {
            "base": dict(invalid),
            "gold_base": dict(invalid),
            "gold": dict(invalid),
            "audit": {"directives": directives},
        }

    def prepare_state(label: str) -> dict[str, Any]:
        reset = git_reset_to(repo_dir, str(issue["base_commit"]), clean=False)
        _remove_untracked_test_path(repo_dir, final_test_relpath)
        removed_reference_paths = _remove_untracked_patch_paths(
            repo_dir, all_test_patch_paths
        )
        patch_result: dict[str, Any] = {}
        if state == "post":
            patch_result = apply_patch_text(repo_dir, str(issue.get("patch") or ""))
            if patch_result.get("returncode") != 0:
                raise RuntimeError(
                    f"golden code patch failed before {label}: "
                    f"{patch_result.get('stderr') or patch_result.get('stdout')}"
                )
        return {
            "reset": reset,
            "code_patch": patch_result,
            "removed_untracked_reference_paths": removed_reference_paths,
        }

    audit: dict[str, Any] = {
        "state": state,
        "all_test_patch_paths": all_test_patch_paths,
        "directives": directives,
        "prediction_command": prediction_command,
        "gold_command": gold_command,
        "golden_test_patch_used_only_for_executable_line_reference": True,
    }
    try:
        audit["base_prepare"] = prepare_state("base reference")
        base = collect_patch_side_coverage(
            instance_id,
            f"base_{state}",
            target_lines,
            prediction_command,
            repo_dir,
            env_name,
            pythonpath,
            timeout,
        )
        # SWT-Bench's paper report fixes its macro denominator with a separate
        # gold prediction run.  Reproduce that denominator locally by running
        # the gold test directive without applying the gold test patch.
        audit["gold_base_prepare"] = prepare_state("gold baseline reference")
        gold_base = collect_patch_side_coverage(
            instance_id,
            f"gold_base_{state}",
            target_lines,
            gold_command,
            repo_dir,
            env_name,
            pythonpath,
            timeout,
        )
        audit["gold_prepare"] = prepare_state("gold reference")
        test_patch_result = apply_patch_text(repo_dir, test_patch)
        audit["gold_test_patch_apply"] = test_patch_result
        if test_patch_result.get("returncode") != 0:
            gold = {
                "status": "GOLD_TEST_PATCH_APPLY_ERROR",
                "error": test_patch_result.get("stderr") or test_patch_result.get("stdout"),
                "target_lines_by_file": target_lines,
                "covered_lines_by_file": {},
                "executable_lines_by_file": {},
                "hit_counts_by_file": {},
                "target_line_count": sum(map(len, target_lines.values())),
                "covered_line_count": 0,
            }
        else:
            gold = collect_patch_side_coverage(
                instance_id,
                f"gold_{state}",
                target_lines,
                gold_command,
                repo_dir,
                env_name,
                pythonpath,
                timeout,
            )
    except Exception as exc:  # noqa: BLE001
        error = {
            "status": "REFERENCE_COVERAGE_ERROR",
            "error": repr(exc),
            "target_lines_by_file": target_lines,
            "covered_lines_by_file": {},
            "executable_lines_by_file": {},
            "hit_counts_by_file": {},
            "target_line_count": sum(map(len, target_lines.values())),
            "covered_line_count": 0,
        }
        base = locals().get("base", dict(error))
        gold_base = locals().get("gold_base", dict(error))
        gold = locals().get("gold", dict(error))
        audit["error"] = repr(exc)
    finally:
        try:
            audit["cleanup_reset"] = git_reset_to(
                repo_dir, str(issue["base_commit"]), clean=False
            )
            _remove_untracked_test_path(repo_dir, final_test_relpath)
            audit["cleanup_removed_untracked_reference_paths"] = (
                _remove_untracked_patch_paths(repo_dir, all_test_patch_paths)
            )
        except Exception as exc:  # noqa: BLE001
            audit["cleanup_error"] = repr(exc)
    return {"base": base, "gold_base": gold_base, "gold": gold, "audit": audit}


def evaluate_one(
    issue: dict[str, Any],
    generated_dir: str,
    repo_root_base: str,
    timeout: int,
    setup: bool,
    use_generated_worktree: bool = False,
    eval_worktree_root: str = "",
    eval_clone_root: str = "",
    cleanup_isolated_worktree: bool = True,
    compute_patch_coverage: bool = False,
    dataset_mode: str = "swt",
) -> dict[str, Any]:
    instance_id = issue["instance_id"]
    final_path = Path(generated_dir) / instance_id / "final_test.py"
    if not final_path.exists():
        missing_result: dict[str, Any] = {
            "instance_id": instance_id,
            "repo": str(issue.get("repo") or "UNKNOWN"),
            "dataset_mode": dataset_mode,
            "status": "MISSING_GENERATION",
            "success": False,
        }
        if dataset_mode == "tdd":
            missing_result["tdd_coverage"] = {
                "status": "MISSING_GENERATION",
                "metric_family": "tdd_bench_changed_line_coverage",
                "total_changed": 0,
                "total_missed": 0,
                "cov_score": 0.0,
                "fail_before": 0,
                "pass_after": 0,
                "final_score": 0.0,
                "resolved": False,
                "audit": {
                    "final_test_present": False,
                    "denominator_policy": (
                        "missing generation contributes zero in the complete "
                        "TDD dataset denominator"
                    ),
                },
            }
        else:
            missing_result["patch_coverage"] = {
                "status": "MISSING_GENERATION",
                "patch_line_coverage": 0.0,
                "coverage_delta_pred": 0.0,
                "target_line_count": 0,
                "covered_line_count": 0,
                "audit": {
                    "final_test_present": False,
                    "six_views_executed": False,
                    "denominator_policy": (
                        "missing generation counts as zero in the fixed 276-row "
                        "F2P denominator; Delta C requires an independent gold run"
                    ),
                },
            }
        return missing_result
    generated_worktree = Path(generated_dir) / instance_id / "worktree"
    eval_worktree_metadata: dict[str, Any] = {}
    isolated_eval_worktree = False
    isolated_eval_clone = False
    try:
        if use_generated_worktree and generated_worktree.is_dir():
            repo_dir = str(generated_worktree)
            preserve_build_artifacts = True
        elif eval_clone_root:
            repo_dir, eval_worktree_metadata = prepare_eval_clone(issue, repo_root_base, eval_clone_root)
            preserve_build_artifacts = False
            isolated_eval_clone = True
        elif eval_worktree_root:
            repo_dir, eval_worktree_metadata = prepare_eval_worktree(issue, repo_root_base, eval_worktree_root)
            preserve_build_artifacts = False
            isolated_eval_worktree = True
        else:
            repo_dir = repo_path(repo_root_base, issue)
            preserve_build_artifacts = False
    except Exception as exc:  # noqa: BLE001
        return {
            "instance_id": instance_id,
            "status": "ERROR",
            "success": False,
            "error": repr(exc),
            "worktree_mode": "isolated_eval_clone" if eval_clone_root else "isolated_eval_worktree",
        }
    code = final_path.read_text(encoding="utf-8")
    execution_code, runner_adapter = adapt_generated_test_for_runner(
        str(issue.get("repo") or ""),
        code,
        instance_id,
    )
    rel_file = direct_test_relpath(
        instance_id,
        generated_dir,
        str(issue.get("repo") or ""),
    )
    selector = first_test_selector(execution_code)
    # SWT-Bench evaluates every test in every file changed by the model patch.
    # Our model output is one generated file, so F2P and coverage both execute
    # that complete file; ``selector`` remains metadata only.
    command = (
        tdd_test_command(
            issue["repo"],
            issue["version"],
            rel_file,
            "",
            with_coverage=compute_patch_coverage,
        )
        if dataset_mode == "tdd"
        else test_command(issue["repo"], issue["version"], rel_file, "")
    )
    coverage_command = command
    runner_parity = runner_parity_info(
        instance_id, generated_dir, rel_file, selector, command
    )
    env_resolution = resolve_eval_env(issue, generated_dir)
    template_candidate = (
        env_resolution.resolved_env or env_resolution.requested_env
    )
    spec = make_instance_spec(
        instance_id,
        str(issue.get("repo") or ""),
        str(issue.get("version") or ""),
        str(issue.get("base_commit") or ""),
        str(issue.get("environment_setup_commit") or issue.get("base_commit") or ""),
    )
    template_prepare = ensure_icore_environment(
        spec, template_candidate, repo_dir, timeout
    )
    template_prepare_attempts = [
        {"candidate": template_candidate, "result": template_prepare}
    ]
    if (
        template_prepare.get("returncode") != 0
        and env_resolution.requested_env
        and env_resolution.requested_env != template_candidate
    ):
        template_prepare = ensure_icore_environment(
            spec, env_resolution.requested_env, repo_dir, timeout
        )
        template_prepare_attempts.append(
            {
                "candidate": env_resolution.requested_env,
                "result": template_prepare,
            }
        )
    template_env_name = str(
        template_prepare.get("env_name") or template_candidate
    )
    runtime_prepare: dict[str, Any] = {}
    if template_prepare.get("returncode") == 0:
        runtime_prepare = ensure_isolated_runtime_environment(
            template_env_name,
            instance_id,
            repo_dir,
            timeout,
            "formal_eval",
            spec.install,
            project_distribution=str(issue.get("repo") or "").split("/")[-1],
        )
    env_name = str(runtime_prepare.get("env_name") or template_env_name)
    runtime_health = (
        runtime_prepare.get("health")
        if isinstance(runtime_prepare.get("health"), dict)
        else {}
    )
    pythonpath = f"{repo_dir}:{repo_dir}/src:{repo_dir}/lib"
    full_command = f"{conda_activate_cmd(env_name)} && export PYTHONPATH={pythonpath}:$PYTHONPATH && {command}"
    result: dict[str, Any] = {
        "instance_id": instance_id,
        "dataset_mode": dataset_mode,
        "coverage_metric_family": (
            "tdd_bench_changed_line_coverage"
            if dataset_mode == "tdd"
            else "swt_bench_delta_mean_change_coverage"
        ),
        "repo": issue["repo"],
        "version": issue["version"],
        "env_name": env_name,
        "template_env_name": template_env_name,
        "template_environment": template_prepare,
        "template_environment_attempts": template_prepare_attempts,
        "runtime_environment": runtime_prepare,
        "env_resolution": env_resolution.to_dict(),
        "requested_env": env_resolution.requested_env,
        "recorded_env": env_resolution.recorded_env,
        "resolved_env": env_name,
        "resolution_source": env_resolution.resolution_source,
        "env_exists": bool(runtime_health.get("ok")),
        "env_health": runtime_health,
        "repo_dir": repo_dir,
        "generated_final_test": str(final_path),
        "direct_test_repo_path": rel_file,
        "selector": selector,
        "test_command": command,
        "coverage_test_command": coverage_command,
        "runner_parity": runner_parity,
        "runner_adapter": {
            **runner_adapter,
            "generated_test_sha256": hashlib.sha256(
                code.encode("utf-8")
            ).hexdigest(),
            "executed_test_sha256": hashlib.sha256(
                execution_code.encode("utf-8")
            ).hexdigest(),
        },
        "worktree_mode": (
            "generated_instance_worktree"
            if use_generated_worktree and generated_worktree.is_dir()
            else "isolated_eval_clone"
            if isolated_eval_clone
            else "isolated_eval_worktree"
            if isolated_eval_worktree
            else "shared_repo"
        ),
        "eval_isolation": eval_worktree_metadata,
        "workspace_audit": {
            "instance_id": instance_id,
            "workspace_clean_before": False,
            "applied_test": str(final_path),
            "applied_test_hash": _sha256_file(final_path),
            "production_files_modified": [],
            "intermediate_candidates_applied": [],
            "surrogate_patch_applied": False,
            "golden_patch_applied_only_in_fixed_phase": True,
            "workspace_clean_after": False,
        },
    }
    if template_prepare.get("returncode") != 0:
        template_health = (
            template_prepare.get("health")
            if isinstance(template_prepare.get("health"), dict)
            else {}
        )
        category = str(template_health.get("category") or "ENV_TEMPLATE_ERROR")
        result["success"] = False
        result["status"] = category
        result["env_error_category"] = category
        result["error"] = str(
            template_health.get("reason")
            or template_prepare.get("status")
            or "; ".join(env_resolution.errors)
        )
        return result
    if runtime_prepare.get("returncode") != 0 or not runtime_health.get("ok"):
        category = str(runtime_health.get("category") or "ENV_ISOLATION_ERROR")
        result["success"] = False
        result["status"] = category
        result["env_error_category"] = category
        result["error"] = str(
            runtime_prepare.get("status") or runtime_health.get("reason") or ""
        )
        return result
    try:
        result["buggy_reset"] = git_reset_to(repo_dir, issue["base_commit"], clean=not preserve_build_artifacts)
        if setup:
            result["environment_cache"] = ensure_eval_project_ready(
                issue, env_name, repo_dir, timeout, spec
            )
            result["buggy_setup"] = result["environment_cache"]
            if result["environment_cache"].get("returncode") != 0:
                setup_execution = result["environment_cache"].get("setup_execution") or result["environment_cache"]
                result["buggy"] = classify_run(setup_execution)
                result["fixed"] = {}
                result["success"] = False
                result["status"] = "BUGGY_SETUP_ERROR"
                result["env_error_category"] = str(
                    (result["environment_cache"].get("health_after") or {}).get("category")
                    or "INSTALL_FAILURE"
                )
                return result
        result["runtime_manifest_after_setup"] = environment_manifest(
            env_name, timeout=min(max(timeout, 120), 300), refresh=True
        )
        if dataset_mode == "tdd" and compute_patch_coverage:
            result["tdd_coverage_tools"] = ensure_tdd_coverage_tools(
                str(issue.get("repo") or ""), env_name, repo_dir, timeout
            )
            if result["tdd_coverage_tools"].get("returncode") != 0:
                result["success"] = False
                result["status"] = "TDD_COVERAGE_TOOL_ERROR"
                result["env_error_category"] = "INSTALL_FAILURE"
                return result
        if dataset_mode == "tdd":
            result["tdd_optional_runtime_tools"] = (
                ensure_tdd_optional_runtime_tools(
                    str(issue.get("repo") or ""),
                    code,
                    env_name,
                    repo_dir,
                    timeout,
                )
            )
            if result["tdd_optional_runtime_tools"].get("returncode") != 0:
                result["success"] = False
                result["status"] = "TDD_OPTIONAL_RUNTIME_ERROR"
                result["env_error_category"] = "INSTALL_FAILURE"
                return result
        baseline_paths = _git_changed_paths(repo_dir)
        tracked_before = _git_changed_paths(repo_dir, include_untracked=False)
        result["workspace_audit"]["workspace_clean_before"] = not tracked_before
        result["workspace_audit"]["baseline_untracked_or_build_artifacts"] = baseline_paths
        written_test = Path(
            write_generated_test(repo_dir, rel_file, execution_code)
        )
        result["workspace_audit"]["applied_repo_test"] = rel_file
        result["workspace_audit"]["applied_repo_test_hash"] = _sha256_file(written_test)
        after_test_paths = _git_changed_paths(repo_dir)
        introduced = sorted(set(after_test_paths) - set(baseline_paths))
        unexpected = [path for path in introduced if path != rel_file]
        result["workspace_audit"]["paths_introduced_by_test_application"] = introduced
        result["workspace_audit"]["production_files_modified"] = unexpected
        if unexpected:
            result["success"] = False
            result["status"] = "FINAL_TEST_APPLICATION_POLLUTION"
            result["error"] = f"applying final_test.py changed unexpected paths: {unexpected}"
            return result

        patch_targets = patch_target_lines(str(issue.get("patch") or ""))
        tdd_targets = (
            tdd_changed_lines(str(issue.get("patch") or ""))
            if dataset_mode == "tdd"
            else {"before": {}, "after": {}}
        )
        buggy_patch_coverage: dict[str, Any] = {}
        buggy_tdd_coverage: dict[str, Any] = {}
        if compute_patch_coverage and dataset_mode == "tdd":
            buggy_tdd_coverage = collect_tdd_side_coverage(
                instance_id,
                "before",
                tdd_targets.get("before") or {},
                coverage_command,
                repo_dir,
                env_name,
                pythonpath,
                timeout,
                repo=str(issue.get("repo") or ""),
            )
            buggy_run = buggy_tdd_coverage["run"]
        elif compute_patch_coverage:
            buggy_patch_coverage = collect_patch_side_coverage(
                instance_id,
                "buggy",
                patch_targets.get("buggy") or {},
                coverage_command,
                repo_dir,
                env_name,
                pythonpath,
                timeout,
            )
            buggy_run = run_shell(full_command, repo_dir, timeout)
        else:
            buggy_run = run_shell(full_command, repo_dir, timeout)
        result["buggy_run"] = buggy_run
        result["buggy"] = classify_run(buggy_run)
        buggy_runner_env = runner_environment_error_category(command, buggy_run)
        if buggy_runner_env:
            result["fixed"] = {}
            result["success"] = False
            result["status"] = "BUGGY_RUNTIME_ENV_ERROR"
            result["env_error_category"] = buggy_runner_env
            return result
        reference_pre: dict[str, Any] = {}
        if compute_patch_coverage and dataset_mode == "swt":
            reference_pre = collect_reference_patch_coverage(
                issue,
                instance_id,
                "pre",
                patch_targets.get("buggy") or {},
                repo_dir,
                env_name,
                pythonpath,
                timeout,
                rel_file,
                coverage_command,
            )

        # Keep the setup products from the buggy phase.  ``git reset --hard``
        # restores all tracked source files, while avoiding ``git clean -fdx``
        # preserves generated version modules and in-place native extensions.
        # This makes fixed-side evaluation both correct and no slower than the
        # original buggy-side preparation for ordinary Python patches.
        fixed_build_artifacts_preserved = True
        result["pre_patch_reset"] = git_reset_to(
            repo_dir, issue["base_commit"], clean=False
        )
        patch_result = apply_patch_text(repo_dir, issue.get("patch", ""))
        if patch_result["returncode"] != 0:
            result["patch_apply_initial"] = patch_result
            retry_clean = not preserve_build_artifacts
            result["patch_retry_reset"] = git_reset_to(
                repo_dir, issue["base_commit"], clean=retry_clean
            )
            fixed_build_artifacts_preserved = not retry_clean
            patch_result = apply_patch_text(repo_dir, issue.get("patch", ""))
        result["patch_apply"] = patch_result
        if patch_result["returncode"] != 0:
            result["fixed"] = {}
            result["success"] = False
            result["status"] = "PATCH_APPLY_ERROR"
            return result
        write_generated_test(repo_dir, rel_file, execution_code)
        if fixed_setup_required(
            setup=setup,
            preserve_build_artifacts=fixed_build_artifacts_preserved,
            patch_text=str(issue.get("patch") or ""),
        ):
            fixed_setup_command = (
                f"{conda_activate_cmd(env_name)} && "
                f"{icore_setup_command(spec, repo_dir)}"
            )
            result["fixed_setup"] = run_setup_with_fallback(
                fixed_setup_command, repo_dir, timeout
            )
            if result["fixed_setup"]["returncode"] != 0:
                result["fixed"] = classify_run(result["fixed_setup"])
                result["success"] = False
                result["status"] = "FIXED_SETUP_ERROR"
                result["env_error_category"] = classify_env_error(
                    (result["fixed_setup"].get("stdout") or "") + "\n" + (result["fixed_setup"].get("stderr") or "")
                )
                return result
            # Reapply the exact golden patch after a build command potentially
            # touched tracked metadata; built artifacts remain in place.
            result["fixed_post_setup_reset"] = git_reset_to(
                repo_dir, issue["base_commit"], clean=False
            )
            result["fixed_patch_reapply"] = apply_patch_text(
                repo_dir, issue.get("patch", "")
            )
            if result["fixed_patch_reapply"]["returncode"] != 0:
                result["fixed"] = {}
                result["success"] = False
                result["status"] = "PATCH_REAPPLY_ERROR"
                return result
            write_generated_test(repo_dir, rel_file, execution_code)
        fixed_tdd_coverage: dict[str, Any] = {}
        if compute_patch_coverage and dataset_mode == "tdd":
            fixed_tdd_coverage = collect_tdd_side_coverage(
                instance_id,
                "after",
                tdd_targets.get("after") or {},
                coverage_command,
                repo_dir,
                env_name,
                pythonpath,
                timeout,
                repo=str(issue.get("repo") or ""),
            )
            fixed_run = fixed_tdd_coverage["run"]
        else:
            fixed_run = run_shell(full_command, repo_dir, timeout)
        result["fixed_run"] = fixed_run
        result["fixed"] = classify_run(fixed_run)
        result["runtime_manifest_after_evaluation"] = environment_manifest(
            env_name, timeout=min(max(timeout, 120), 300), refresh=True
        )
        fixed_runner_env = runner_environment_error_category(command, fixed_run)
        if fixed_runner_env:
            result["success"] = False
            result["status"] = "FIXED_RUNTIME_ENV_ERROR"
            result["env_error_category"] = fixed_runner_env
            return result
        fixed_patch_coverage: dict[str, Any] = {}
        if compute_patch_coverage and dataset_mode == "swt":
            fixed_patch_coverage = collect_patch_side_coverage(
                instance_id,
                "fixed",
                patch_targets.get("fixed") or {},
                coverage_command,
                repo_dir,
                env_name,
                pythonpath,
                timeout,
            )
        fixed_paths = _git_changed_paths(repo_dir)
        result["workspace_audit"]["fixed_phase_paths"] = fixed_paths
        result["workspace_audit"]["golden_patch_files"] = sorted(
            set((patch_targets.get("buggy") or {}))
            | set((patch_targets.get("fixed") or {}))
        )
        if compute_patch_coverage and dataset_mode == "swt":
            reference_post = collect_reference_patch_coverage(
                issue,
                instance_id,
                "post",
                patch_targets.get("fixed") or {},
                repo_dir,
                env_name,
                pythonpath,
                timeout,
                rel_file,
                coverage_command,
            )
            result["workspace_audit"]["patch_coverage_reference_runs"] = {
                "pre": reference_pre.get("audit") or {},
                "post": reference_post.get("audit") or {},
            }
            result["workspace_audit"][
                "golden_test_patch_applied_only_in_patch_coverage"
            ] = True
            result["patch_coverage"] = combine_patch_coverage(
                patch_targets,
                buggy_patch_coverage,
                fixed_patch_coverage,
                reference_pre.get("gold") or {},
                reference_post.get("gold") or {},
                reference_pre.get("base") or {},
                reference_post.get("base") or {},
                reference_pre.get("gold_base") or {},
                reference_post.get("gold_base") or {},
            )
        elif compute_patch_coverage and dataset_mode == "tdd":
            result["workspace_audit"]["tdd_coverage_views"] = [
                "generated_test_before_golden_patch",
                "generated_test_after_golden_patch",
            ]
            result["workspace_audit"]["swt_reference_views_executed"] = False
            result["tdd_coverage"] = combine_tdd_coverage(
                instance_id,
                tdd_targets,
                buggy_tdd_coverage,
                fixed_tdd_coverage,
                result["buggy"],
                result["fixed"],
            )
        result["success"] = bool(result["buggy"]["failed"] and not result["fixed"]["failed"])
        if result["success"]:
            result["status"] = "F2P_SUCCESS"
        elif not result["buggy"]["failed"]:
            result["status"] = "BUGGY_PASS"
        elif result["fixed"]["failed"]:
            result["status"] = "FIXED_FAIL"
        else:
            result["status"] = "UNKNOWN"
        return result
    except Exception as exc:  # noqa: BLE001
        result["success"] = False
        result["status"] = "ERROR"
        result["error"] = repr(exc)
        return result
    finally:
        try:
            git_reset_to(repo_dir, issue["base_commit"], clean=not preserve_build_artifacts)
            result.setdefault("workspace_audit", {})["workspace_clean_after"] = not bool(
                _git_changed_paths(repo_dir, include_untracked=False)
            )
        except Exception:
            pass
        if runtime_prepare.get("returncode") == 0 and env_name != template_env_name:
            try:
                result["runtime_environment_cleanup"] = (
                    remove_isolated_runtime_environment(env_name, repo_dir, timeout)
                )
            except Exception as exc:  # noqa: BLE001
                result["runtime_environment_cleanup_error"] = repr(exc)
        if isolated_eval_worktree and cleanup_isolated_worktree:
            try:
                result["eval_worktree_cleanup"] = cleanup_eval_worktree(
                    issue, repo_root_base, eval_worktree_root, repo_dir
                )
            except Exception as exc:  # noqa: BLE001
                result["eval_worktree_cleanup_error"] = repr(exc)
        if isolated_eval_clone and cleanup_isolated_worktree:
            try:
                shutil.rmtree(repo_dir, ignore_errors=True)
                result["eval_clone_cleanup"] = {"removed": True}
            except Exception as exc:  # noqa: BLE001
                result["eval_clone_cleanup_error"] = repr(exc)


def group_ids_by_repo(issues: dict[str, dict[str, Any]], ids: list[str], workers: int) -> list[list[str]]:
    repo_groups: dict[str, list[str]] = {}
    for iid in ids:
        repo_groups.setdefault(issues[iid]["repo"], []).append(iid)
    buckets = [[] for _ in range(workers)]
    sizes = [0 for _ in range(workers)]
    for _, group in sorted(repo_groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        idx = min(range(workers), key=lambda i: sizes[i])
        buckets[idx].extend(group)
        sizes[idx] += len(group)
    return buckets


ENV_ERROR_CATEGORIES = {
    "ENV_NOT_FOUND",
    "ENV_INCOMPLETE",
    "CONDA_LOCK",
    "DISK_FULL",
    "INSTALL_FAILURE",
    "COMMAND_RESOLUTION_FAILURE",
}


def result_env_category(result: dict[str, Any]) -> str:
    category = str(result.get("env_error_category") or "")
    if category:
        return category
    status = str(result.get("status") or "")
    if status in ENV_ERROR_CATEGORIES:
        return status
    return ""


def update_failfast_state(
    result: dict[str, Any],
    state: dict[str, Any],
    stop_event: threading.Event,
    min_count: int,
    ratio: float,
) -> None:
    with state["lock"]:
        state["total"] += 1
        category = result_env_category(result)
        if category:
            state["env_errors"] += 1
            state["by_env_error_category"][category] = state["by_env_error_category"].get(category, 0) + 1
        if (
            state["total"] >= max(1, min_count)
            and state["env_errors"] / max(1, state["total"]) >= ratio
        ):
            state["invalid_environment"] = True
            stop_event.set()


def run_bucket(
    worker_id: int,
    ids: list[str],
    issues: dict[str, dict[str, Any]],
    args: argparse.Namespace,
    stop_event: threading.Event,
    failfast_state: dict[str, Any],
) -> dict[str, Any]:
    worker_dir = Path(args.output_dir) / f"worker_{worker_id}"
    ensure_dir(worker_dir)
    out_path = worker_dir / "results.json"
    results = {}
    if args.resume and out_path.exists():
        results = json.loads(out_path.read_text(encoding="utf-8"))
    for iid in ids:
        if stop_event.is_set():
            break
        if iid in results:
            continue
        resolution = resolve_eval_env(issues[iid], args.generated_dir)
        with environment_operation_lock(
            resolution.resolved_env or "__missing_env__"
        ):
            res = evaluate_one(
                issues[iid],
                args.generated_dir,
                args.repo_root_base,
                args.timeout,
                not args.no_setup,
                args.use_generated_worktrees,
                args.eval_worktree_root,
                args.eval_clone_root or str(Path(args.output_dir) / "eval_clones"),
                not args.keep_eval_worktrees,
                args.compute_patch_coverage,
                args.dataset_mode,
            )
        results[iid] = res
        safe_json_dump(
            res.get("workspace_audit") or {
                "instance_id": iid,
                "workspace_clean_before": False,
                "workspace_clean_after": False,
                "error": "evaluation ended before workspace audit was initialized",
            },
            str(worker_dir / "workspace_audits" / f"{sanitize_instance_id(iid)}.json"),
        )
        if isinstance(res.get("patch_coverage"), dict):
            safe_json_dump(
                res["patch_coverage"],
                str(
                    worker_dir
                    / "patch_coverage"
                    / f"{sanitize_instance_id(iid)}.json"
                ),
            )
        if isinstance(res.get("tdd_coverage"), dict):
            safe_json_dump(
                res["tdd_coverage"],
                str(
                    worker_dir
                    / "tdd_coverage"
                    / f"{sanitize_instance_id(iid)}.json"
                ),
            )
        update_failfast_state(
            res,
            failfast_state,
            stop_event,
            args.env_failfast_min_count,
            args.env_failfast_ratio,
        )
        safe_json_dump(results, str(out_path))
        print(f"worker_{worker_id} {iid} {res.get('status')} success={res.get('success')}", flush=True)
    return {"worker_id": worker_id, "count": len(results), "path": str(out_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Formal buggy/fixed F2P and Patch Coverage evaluator for BRT6 outputs."
    )
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--generated_dir", required=True)
    parser.add_argument("--repo_root_base", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--instance_id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max_workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument(
        "--dataset_mode",
        choices=("swt", "tdd"),
        default="swt",
        help="Select the benchmark-specific coverage protocol.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no_setup", action="store_true", help="Skip editable install refresh before each side.")
    parser.add_argument("--env_failfast_min_count", type=int, default=20)
    parser.add_argument("--env_failfast_ratio", type=float, default=0.30)
    parser.add_argument("--use_swebench_lite", action="store_true", help="Load SWE-bench/SWE-bench_Lite to fill patch fields for F2P validation.")
    parser.add_argument(
        "--use_generated_worktrees",
        action="store_true",
        help="Reuse each generated instance's prepared worktree and preserve its build artifacts.",
    )
    parser.add_argument(
        "--eval_worktree_root",
        default="",
        help=(
            "Directory for isolated per-instance formal-eval worktrees. "
            "This is an opt-in mode; the default uses isolated local clones."
        ),
    )
    parser.add_argument(
        "--eval_clone_root",
        default="",
        help=(
            "Directory for isolated per-instance formal-eval local clones. "
            "Defaults to <output_dir>/eval_clones when --use_generated_worktrees and --eval_worktree_root are not set."
        ),
    )
    parser.add_argument(
        "--keep_eval_worktrees",
        action="store_true",
        help="Keep isolated formal-eval clones/worktrees for debugging instead of removing them after each instance.",
    )
    parser.add_argument(
        "--compute_patch_coverage",
        type=parse_bool,
        default=False,
        help=(
            "Run benchmark-native coverage: SWT-Bench six/eight-view Delta C "
            "for --dataset_mode swt, or TDD-Bench pre/post changed-line "
            "coverage with F2P gating for --dataset_mode tdd."
        ),
    )
    return parser


def fill_patches_from_swebench_lite(issues: dict[str, dict[str, Any]]) -> None:
    try:
        from datasets import Dataset, load_dataset
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("datasets is required for --use_swebench_lite") from exc
    try:
        ds = load_dataset("SWE-bench/SWE-bench_Lite")["test"]
    except OSError as exc:
        # Managed/offline runs may have a readable Arrow cache but no write
        # permission for the adjacent datasets lock file.
        cache_root = Path(
            os.environ.get(
                "HF_DATASETS_CACHE",
                str(Path.home() / ".cache" / "huggingface" / "datasets"),
            )
        )
        candidates = sorted(
            cache_root.glob(
                "SWE-bench___swe-bench_lite/default/*/*/swe-bench_lite-test.arrow"
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError(
                "SWE-bench Lite cache is unavailable and the dataset could not be loaded"
            ) from exc
        ds = Dataset.from_file(str(candidates[0]))
    by_id = {row["instance_id"]: dict(row) for row in ds}
    for iid, row in issues.items():
        src = by_id.get(iid)
        if not src:
            continue
        for key in ["patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "environment_setup_commit"]:
            if key in src and not row.get(key):
                row[key] = src[key]


def main() -> None:
    args = build_parser().parse_args()
    ensure_dir(args.output_dir)
    output_root = Path(args.output_dir)
    existing_metrics_path = output_root / "metrics.json"
    if args.resume and existing_metrics_path.is_file():
        existing_metrics = json.loads(
            existing_metrics_path.read_text(encoding="utf-8")
        )
        existing_mode = str(existing_metrics.get("dataset_mode") or "")
        if (
            not existing_mode
            and str(existing_metrics.get("patch_cov_algorithm") or "").startswith(
                "swt_bench_"
            )
        ):
            existing_mode = "swt"
        if existing_mode != args.dataset_mode:
            raise ValueError(
                "refusing cross-benchmark resume: existing output mode is "
                f"{existing_mode or 'unknown'}, requested {args.dataset_mode}"
            )
    stale_names: tuple[str, ...] = ()
    if not args.compute_patch_coverage:
        # F2P-only ablations must not leave coverage placeholders behind,
        # including when an output directory is explicitly reused.
        stale_names = (
            "patch_coverage_results.json",
            "delta_change_coverage.json",
            "tdd_coverage_results.json",
            "tdd_coverage.json",
        )
    elif not args.resume:
        stale_names = (
            ("patch_coverage_results.json", "delta_change_coverage.json")
            if args.dataset_mode == "tdd"
            else ("tdd_coverage_results.json", "tdd_coverage.json")
        )
    for stale_name in stale_names:
        (output_root / stale_name).unlink(missing_ok=True)
    tmp_root = os.environ.get("TMPDIR") or str(Path(args.output_dir) / "tmp")
    Path(tmp_root).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = tmp_root
    preflight = preflight_system(
        [
            args.output_dir,
            args.generated_dir,
            args.repo_root_base,
            tmp_root,
            str(Path.home()),
        ]
    )
    safe_json_dump(preflight, str(Path(args.output_dir) / "environment_preflight.json"))
    if not preflight.get("ok"):
        metrics = {
            "dataset_mode": args.dataset_mode,
            "coverage_metric_family": (
                "tdd_bench_changed_line_coverage"
                if args.dataset_mode == "tdd"
                else "swt_bench_delta_mean_change_coverage"
            ),
            "total_instances": 0,
            "f2p_success": 0,
            "f2p_fail": 0,
            "f2p_at_1": 0,
            "f2p_at_1_percent": 0,
            "by_status": {},
            "by_env_error_category": {"DISK_FULL": 1} if any(not item.get("ok") for item in preflight.get("checks", [])) else {"COMMAND_RESOLUTION_FAILURE": 1},
            "invalid_environment": True,
            "invalid_reason": "environment preflight failed",
            "environment_preflight": preflight,
            "generated_dir": args.generated_dir,
        }
        safe_json_dump({}, str(Path(args.output_dir) / "merged_results.json"))
        safe_json_dump(metrics, str(Path(args.output_dir) / "metrics.json"))
        print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
        raise SystemExit(3)
    issues = load_issue_data(args.instances_path)
    if args.use_swebench_lite:
        if args.dataset_mode == "tdd":
            raise ValueError(
                "--use_swebench_lite is SWT-only; TDD must use its own 449-row patches"
            )
        fill_patches_from_swebench_lite(issues)
    ids = [args.instance_id] if args.instance_id else list(issues)
    ids = [iid for iid in ids if iid in issues]
    if args.limit:
        ids = ids[: args.limit]
    buckets = group_ids_by_repo(issues, ids, max(1, args.max_workers))
    summaries = []
    stop_event = threading.Event()
    failfast_state: dict[str, Any] = {
        "lock": threading.Lock(),
        "total": 0,
        "env_errors": 0,
        "by_env_error_category": {},
        "invalid_environment": False,
    }
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        futures = [
            pool.submit(run_bucket, i, bucket, issues, args, stop_event, failfast_state)
            for i, bucket in enumerate(buckets)
            if bucket
        ]
        for future in as_completed(futures):
            summaries.append(future.result())
    merged = {}
    for item in summaries:
        path = item["path"]
        if os.path.exists(path):
            merged.update(json.loads(Path(path).read_text(encoding="utf-8")))
    for iid in ids:
        if iid not in merged:
            missing_evaluation: dict[str, Any] = {
                "instance_id": iid,
                "repo": str(issues[iid].get("repo") or "UNKNOWN"),
                "dataset_mode": args.dataset_mode,
                "status": "ERROR",
                "success": False,
                "error": "evaluation was not completed after environment fail-fast",
            }
            if args.dataset_mode == "tdd":
                missing_evaluation["tdd_coverage"] = {
                    "status": "ERROR",
                    "metric_family": "tdd_bench_changed_line_coverage",
                    "total_changed": 0,
                    "total_missed": 0,
                    "cov_score": 0.0,
                    "fail_before": 0,
                    "pass_after": 0,
                    "final_score": 0.0,
                    "resolved": False,
                }
            else:
                missing_evaluation["patch_coverage"] = {
                    "status": "ERROR",
                    "patch_line_coverage": 0.0,
                    "coverage_delta_pred": 0.0,
                    "target_line_count": 0,
                    "covered_line_count": 0,
                }
            merged[iid] = missing_evaluation
    total = len(merged)
    success = sum(1 for r in merged.values() if r.get("success"))
    by_status: dict[str, int] = {}
    by_env_error_category: dict[str, int] = {}
    patch_cov_applicable = 0
    patch_cov_instances = 0
    patch_cov_target_lines = 0
    patch_cov_covered_lines = 0
    patch_cov_buggy_target_lines = 0
    patch_cov_buggy_covered_lines = 0
    patch_cov_fixed_target_lines = 0
    patch_cov_fixed_covered_lines = 0
    patch_cov_ratios: list[float] = []
    patch_cov_by_status: dict[str, int] = {}
    tdd_cov_by_status: dict[str, int] = {}
    tdd_cov_total_changed = 0
    tdd_cov_total_missed = 0
    tdd_cov_scores: list[float] = []
    by_repo: dict[str, dict[str, int]] = {}
    environment_cache_hits = 0
    environment_cache_total = 0
    for r in merged.values():
        status = str(r.get("status") or "UNKNOWN")
        by_status[status] = by_status.get(status, 0) + 1
        repo = str(r.get("repo") or "UNKNOWN")
        by_repo.setdefault(repo, {})[status] = by_repo.setdefault(repo, {}).get(status, 0) + 1
        category = result_env_category(r)
        if category:
            by_env_error_category[category] = by_env_error_category.get(category, 0) + 1
        patch_cov = r.get("patch_coverage") if isinstance(r.get("patch_coverage"), dict) else {}
        if patch_cov:
            cov_status = str(patch_cov.get("status") or "UNKNOWN")
            patch_cov_by_status[cov_status] = patch_cov_by_status.get(cov_status, 0) + 1
            target_lines = int(patch_cov.get("target_line_count") or 0)
            covered_lines = int(patch_cov.get("covered_line_count") or 0)
            if cov_status == "OK" and target_lines:
                patch_cov_target_lines += target_lines
                patch_cov_covered_lines += covered_lines
                patch_cov_applicable += 1
                if patch_cov.get("patch_covered"):
                    patch_cov_instances += 1
                patch_cov_buggy_target_lines += int(
                    patch_cov.get("executable_removed_count") or 0
                )
                patch_cov_buggy_covered_lines += len(
                    patch_cov.get("covered_removed_lines") or []
                )
                patch_cov_fixed_target_lines += int(
                    patch_cov.get("executable_added_count") or 0
                )
                patch_cov_fixed_covered_lines += len(
                    patch_cov.get("covered_added_lines") or []
                )
                patch_cov_ratios.append(
                    float(patch_cov.get("patch_line_coverage") or 0.0)
                )
        tdd_cov = (
            r.get("tdd_coverage")
            if isinstance(r.get("tdd_coverage"), dict)
            else {}
        )
        if tdd_cov:
            tdd_status = str(tdd_cov.get("status") or "UNKNOWN")
            tdd_cov_by_status[tdd_status] = tdd_cov_by_status.get(tdd_status, 0) + 1
            tdd_cov_total_changed += int(tdd_cov.get("total_changed") or 0)
            tdd_cov_total_missed += int(tdd_cov.get("total_missed") or 0)
            if "cov_score" in tdd_cov:
                tdd_cov_scores.append(float(tdd_cov.get("cov_score") or 0.0))
        env_cache = r.get("environment_cache") if isinstance(r.get("environment_cache"), dict) else {}
        if env_cache:
            environment_cache_total += 1
            environment_cache_hits += int(bool(env_cache.get("cache_hit")))
    invalid_environment = bool(failfast_state.get("invalid_environment"))
    delta_c = aggregate_delta_change_coverage(
        merged,
        coverage_enabled=(
            args.compute_patch_coverage and args.dataset_mode == "swt"
        ),
    )
    tdd_score = aggregate_tdd_bench_score(
        merged,
        coverage_enabled=(
            args.compute_patch_coverage and args.dataset_mode == "tdd"
        ),
    )
    if invalid_environment:
        delta_c = {
            **delta_c,
            "valid": False,
            "value": None,
            "percent": None,
            "invalid_reason": "environment error fail-fast threshold exceeded",
        }
        tdd_score = {
            **tdd_score,
            "valid": False,
            "value": None,
            "percent": None,
            "invalid_reason": "environment error fail-fast threshold exceeded",
        }
    macro_patch_coverage = (
        statistics.fmean(patch_cov_ratios) if patch_cov_ratios else None
    )
    median_patch_coverage = (
        statistics.median(patch_cov_ratios) if patch_cov_ratios else None
    )
    macro_patch_delta = delta_c["value"]
    eligible_delta_values = list(delta_c["per_instance"].values())
    median_patch_delta = (
        statistics.median(eligible_delta_values)
        if eligible_delta_values and delta_c["valid"]
        else None
    )
    metrics = {
        "dataset_mode": args.dataset_mode,
        "coverage_metric_family": (
            "tdd_bench_changed_line_coverage"
            if args.dataset_mode == "tdd"
            else "swt_bench_delta_mean_change_coverage"
        ),
        "total_instances": total,
        "f2p_success": success,
        "f2p_fail": total - success,
        "f2p_at_1": None if invalid_environment else success / total if total else 0,
        "f2p_at_1_percent": None if invalid_environment else round(success / total * 100, 4) if total else 0,
        "metrics_valid": not invalid_environment,
        "invalid_reason": "environment error fail-fast threshold exceeded" if invalid_environment else "",
        "patch_cov_enabled": args.compute_patch_coverage,
        "patch_cov_applicable": patch_cov_applicable,
        "patch_cov_success": patch_cov_instances,
        "patch_cov_nonzero_instances": patch_cov_instances,
        "patch_cov_nonzero_rate": patch_cov_instances / total if total else 0,
        "patch_cov_at_1": macro_patch_coverage,
        "patch_cov_at_1_percent": (
            round(macro_patch_coverage * 100, 4)
            if macro_patch_coverage is not None
            else None
        ),
        "patch_cov_definition": (
            "SWT-Bench official six-view macro coverage_pred from one final_test.py: "
            "gold/base tests define executable removed and added golden-patch lines; "
            "the prediction is measured on buggy and golden-code-patched states."
        ),
        "patch_cov_algorithm": (
            "swt_bench_six_view_with_independent_gold_denominator"
        ),
        "patch_cov_delta_at_1": macro_patch_delta,
        "patch_cov_delta_at_1_percent": (
            round(macro_patch_delta * 100, 4)
            if macro_patch_delta is not None
            else None
        ),
        "delta_mean_change_coverage": macro_patch_delta,
        "delta_mean_change_coverage_percent": (
            round(macro_patch_delta * 100, 4)
            if macro_patch_delta is not None
            else None
        ),
        "delta_c_valid": bool(delta_c["valid"]),
        "delta_c_invalid_reason": delta_c["invalid_reason"],
        "delta_c_numerator": delta_c["numerator"],
        "delta_c_denominator": delta_c["denominator"],
        "delta_c_eligible_ids": delta_c["eligible_ids"],
        "delta_c_excluded_no_executable_ids": delta_c[
            "excluded_no_executable_ids"
        ],
        "delta_c_excluded_gold_unavailable": delta_c[
            "excluded_gold_unavailable"
        ],
        "delta_c_zeroed_model_instances": delta_c[
            "zeroed_model_instances"
        ],
        "delta_c_invalid_instances": delta_c["invalid_instances"],
        "delta_c_per_instance": delta_c["per_instance"],
        "delta_c_definition": (
            "Delta Mean Change Coverage (Delta C): SWT-Bench-compatible macro mean "
            "of per-instance coverage_delta_pred over gold-applicable instances. "
            "Each changed executable line contributes iff the generated final_test.py "
            "increases its execution count over the no-generated-test baseline."
        ),
        "delta_c_denominator_policy": (
            "SWT-Bench paper policy: instances with a defined independent-gold "
            "coverage delta form the denominator; unavailable gold runs are excluded; "
            "unavailable model runs contribute zero when gold is applicable"
        ),
        "delta_c_schema_version": 2,
        "delta_c_coverage_instrumentation": (
            "vendored_swt_bench_subprocess_aware_trace"
        ),
        "delta_c_coverage_instrumentation_sha256": SWT_TRACE_SHA256,
        "delta_c_prediction_scope": "complete_final_test_file",
        "delta_c_gold_denominator_source": (
            "independent_gold_test_and_gold_baseline_views"
        ),
        "patch_cov_median": median_patch_coverage,
        "patch_cov_median_percent": (
            round(median_patch_coverage * 100, 4)
            if median_patch_coverage is not None
            else None
        ),
        "patch_cov_delta_median": median_patch_delta,
        "patch_cov_delta_median_percent": (
            round(median_patch_delta * 100, 4)
            if median_patch_delta is not None
            else None
        ),
        "patch_cov_target_lines": patch_cov_target_lines,
        "patch_cov_covered_lines": patch_cov_covered_lines,
        "patch_cov_buggy_target_lines": patch_cov_buggy_target_lines,
        "patch_cov_buggy_covered_lines": patch_cov_buggy_covered_lines,
        "patch_cov_fixed_target_lines": patch_cov_fixed_target_lines,
        "patch_cov_fixed_covered_lines": patch_cov_fixed_covered_lines,
        "patch_line_coverage": patch_cov_covered_lines / patch_cov_target_lines if patch_cov_target_lines else 0,
        "patch_line_coverage_percent": round(patch_cov_covered_lines / patch_cov_target_lines * 100, 4) if patch_cov_target_lines else 0,
        "patch_cov_by_status": patch_cov_by_status,
        "by_status": by_status,
        "by_repo": by_repo,
        "by_env_error_category": by_env_error_category,
        "invalid_environment": invalid_environment,
        "environment_cache_hits": environment_cache_hits,
        "environment_cache_total": environment_cache_total,
        "environment_cache_hit_rate": environment_cache_hits / environment_cache_total if environment_cache_total else 0,
        "environment_preflight": preflight,
        "mode": (
            "generated_worktree_same_dir_new_file_no_author_injection"
            if args.use_generated_worktrees
            else "isolated_eval_worktree_same_dir_new_file_no_author_injection"
            if args.eval_worktree_root
            else "isolated_eval_clone_same_dir_new_file_no_author_injection"
        ),
        "generated_dir": args.generated_dir,
    }
    metrics.update(
        {
            "coverage_enabled": args.compute_patch_coverage,
            "tdd_score": tdd_score["value"] if args.dataset_mode == "tdd" else None,
            "tdd_score_percent": (
                tdd_score["percent"] if args.dataset_mode == "tdd" else None
            ),
            "tdd_score_valid": (
                bool(tdd_score["valid"]) if args.dataset_mode == "tdd" else False
            ),
            "tdd_score_numerator": (
                tdd_score["numerator"] if args.dataset_mode == "tdd" else None
            ),
            "tdd_score_denominator": (
                tdd_score["denominator"] if args.dataset_mode == "tdd" else None
            ),
            "tdd_resolved_instances": (
                tdd_score["resolved_instances"]
                if args.dataset_mode == "tdd"
                else None
            ),
            "tdd_zeroed_instances": (
                tdd_score["zeroed_instances"] if args.dataset_mode == "tdd" else {}
            ),
            "tdd_per_instance_score": (
                tdd_score["per_instance"] if args.dataset_mode == "tdd" else {}
            ),
            "tdd_raw_coverage_macro": (
                statistics.fmean(tdd_cov_scores)
                if args.dataset_mode == "tdd" and tdd_cov_scores
                else None
            ),
            "tdd_total_changed": (
                tdd_cov_total_changed if args.dataset_mode == "tdd" else None
            ),
            "tdd_total_missed": (
                tdd_cov_total_missed if args.dataset_mode == "tdd" else None
            ),
            "tdd_coverage_by_status": (
                tdd_cov_by_status if args.dataset_mode == "tdd" else {}
            ),
            "tdd_definition": (
                "TDD-Bench final score: changed-line coverage over nonblank, "
                "non-comment removed lines before and added lines after the golden "
                "code patch, multiplied by fail-before and pass-after; SymPy uses "
                "only the fail-before/pass-after gate. The run score is averaged "
                "over the complete requested dataset, so missing/error instances are zero."
                if args.dataset_mode == "tdd"
                else ""
            ),
            "tdd_algorithm": (
                "tdd_bench_pre_post_coverage_py_changed_lines_f2p_gated"
                if args.dataset_mode == "tdd"
                else ""
            ),
        }
    )
    if args.dataset_mode == "tdd":
        # SWT-only names are explicitly non-applicable in TDD mode.  Keeping
        # them null avoids downstream scripts silently treating TDD coverage as
        # SWT Delta C while retaining a stable metrics schema.
        metrics.update(
            {
                "patch_cov_enabled": False,
                "patch_cov_applicable": None,
                "patch_cov_success": None,
                "patch_cov_nonzero_instances": None,
                "patch_cov_nonzero_rate": None,
                "patch_cov_at_1": None,
                "patch_cov_at_1_percent": None,
                "patch_cov_definition": "",
                "patch_cov_algorithm": "",
                "patch_cov_delta_at_1": None,
                "patch_cov_delta_at_1_percent": None,
                "delta_mean_change_coverage": None,
                "delta_mean_change_coverage_percent": None,
                "delta_c_valid": False,
                "delta_c_invalid_reason": "not applicable to TDD-Bench",
                "delta_c_numerator": None,
                "delta_c_denominator": None,
                "delta_c_eligible_ids": [],
                "delta_c_excluded_no_executable_ids": [],
                "delta_c_excluded_gold_unavailable": {},
                "delta_c_zeroed_model_instances": {},
                "delta_c_invalid_instances": {},
                "delta_c_per_instance": {},
                "delta_c_definition": "",
                "delta_c_denominator_policy": "",
                "delta_c_coverage_instrumentation": "",
                "delta_c_coverage_instrumentation_sha256": "",
                "delta_c_prediction_scope": "",
                "delta_c_gold_denominator_source": "",
                "patch_cov_median": None,
                "patch_cov_median_percent": None,
                "patch_cov_delta_median": None,
                "patch_cov_delta_median_percent": None,
                "patch_cov_target_lines": None,
                "patch_cov_covered_lines": None,
                "patch_cov_buggy_target_lines": None,
                "patch_cov_buggy_covered_lines": None,
                "patch_cov_fixed_target_lines": None,
                "patch_cov_fixed_covered_lines": None,
                "patch_line_coverage": None,
                "patch_line_coverage_percent": None,
                "patch_cov_by_status": {},
            }
        )
    safe_json_dump(merged, str(Path(args.output_dir) / "merged_results.json"))
    if args.compute_patch_coverage and args.dataset_mode == "swt":
        safe_json_dump(
            {
                instance_id: result.get("patch_coverage")
                for instance_id, result in merged.items()
                if isinstance(result.get("patch_coverage"), dict)
            },
            str(Path(args.output_dir) / "patch_coverage_results.json"),
        )
        safe_json_dump(
            {
                "schema_version": 2,
                "metric_name": "Delta Mean Change Coverage",
                "symbol": "Delta C",
                "algorithm": (
                    "swt_bench_six_view_with_independent_gold_denominator"
                ),
                "coverage_instrumentation": (
                    "vendored_swt_bench_subprocess_aware_trace"
                ),
                "coverage_instrumentation_sha256": SWT_TRACE_SHA256,
                "prediction_scope": "complete_final_test_file",
                "gold_denominator_source": (
                    "independent_gold_test_and_gold_baseline_views"
                ),
                **delta_c,
            },
            str(Path(args.output_dir) / "delta_change_coverage.json"),
        )
    elif args.compute_patch_coverage and args.dataset_mode == "tdd":
        safe_json_dump(
            {
                instance_id: result.get("tdd_coverage")
                for instance_id, result in merged.items()
                if isinstance(result.get("tdd_coverage"), dict)
            },
            str(Path(args.output_dir) / "tdd_coverage_results.json"),
        )
        safe_json_dump(
            {
                "schema_version": 1,
                "metric_name": "TDD-Bench final score",
                "algorithm": (
                    "tdd_bench_pre_post_coverage_py_changed_lines_f2p_gated"
                ),
                "coverage_instrumentation": "coverage.py",
                "prediction_scope": "complete_final_test_file",
                **tdd_score,
            },
            str(Path(args.output_dir) / "tdd_coverage.json"),
        )
    safe_json_dump(metrics, str(Path(args.output_dir) / "metrics.json"))
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

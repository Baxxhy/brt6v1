"""Vendored iCoRe-compatible environment and test command helpers."""

from __future__ import annotations

import ast
import configparser
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    tomllib = None
from pathlib import Path
from typing import Any


LEGACY_SYMPY_COMPAT_PATH = (
    Path(__file__).resolve().parents[1] / "runtime" / "legacy_sympy_compat"
)

from packaging.requirements import InvalidRequirement, Requirement

from .icore_exec_spec import make_exec_spec
from ..execution.executor import run_subprocess_tree
from ..runtime.conda_env_manager import (
    CONDA_EXE,
    conda_env_inventory,
    default_env_name,
    dependency_install_spec,
    dependency_seed_candidates,
    dependency_env_compatibility,
    environment_cache_key,
    environment_cache_root,
    environment_manifest,
    environment_lock_path,
    env_health_check,
    has_recoverable_environment_state,
    invalidate_environment_runtime_cache,
    legacy_env_name,
    read_environment_identity,
    read_environment_state,
    write_environment_identity,
    write_environment_state,
)


_CONDA_EXE_RESOLVED = shutil.which(CONDA_EXE) or CONDA_EXE
_CONDA_EXE_PATH = Path(_CONDA_EXE_RESOLVED).expanduser().resolve()
DEFAULT_CONDA_SH = str(
    _CONDA_EXE_PATH.parent.parent / "etc" / "profile.d" / "conda.sh"
)
CONDA_SH = os.environ.get("BRT3_CONDA_SH", DEFAULT_CONDA_SH)


def env_name_for(
    repo: str,
    version: str,
    base_commit: str = "",
    environment_setup_commit: str = "",
) -> str:
    return default_env_name(
        {
            "repo": repo,
            "version": version,
            "base_commit": base_commit,
            "environment_setup_commit": environment_setup_commit,
        },
        prefix=os.environ.get("BRT4_CONDA_ENV_PREFIX"),
    )


def conda_env_names() -> set[str]:
    return set(conda_env_inventory())


def conda_env_exists(env_name: str) -> bool:
    return env_name in conda_env_names()


def _run_script(script: str, cwd: str, timeout: int) -> dict[str, Any]:
    try:
        proc = run_subprocess_tree(
            # The caller has already established the validated Conda, channel,
            # cache, and network environment.  A login shell would source the
            # machine's profile again; on HPC systems that can print account
            # banners and, more importantly, replace working proxy settings.
            ["bash", "-c", script],
            cwd=cwd,
            timeout=timeout,
        )
        return {
            "command": script,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": script,
            "cwd": cwd,
            "returncode": 124,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
            "timeout": True,
        }


def _remove_environment(env_name: str, cwd: str, timeout: int) -> dict[str, Any]:
    result = _run_script(
        f"{shlex.quote(CONDA_EXE)} env remove -n {shlex.quote(env_name)} -y",
        cwd,
        max(timeout, 900),
    )
    invalidate_environment_runtime_cache(env_name)
    conda_env_inventory(refresh=True)
    return result


def _isolate_environment_script_files(
    script: str,
    cwd: str,
    environment_fingerprint: str,
) -> tuple[str, str]:
    """Rewrite legacy process-global requirement paths into this worktree."""

    scratch = Path(cwd) / ".brt-env"
    scratch.mkdir(parents=True, exist_ok=True)
    requirements_path = scratch / f"requirements-{environment_fingerprint[:16]}.txt"
    quoted_path = shlex.quote(str(requirements_path))
    rewritten = str(script or "")
    for shared_path in (
        "$HOME/requirements.txt",
        "${HOME}/requirements.txt",
        "~/requirements.txt",
        "/root/requirements.txt",
    ):
        rewritten = rewritten.replace(shared_path, quoted_path)
    rewritten = re.sub(
        r"(?m)^rm\s+(?!-f\b)([^\n]*requirements[^\n]*)$",
        r"rm -f \1",
        rewritten,
    )
    return rewritten, str(requirements_path)


def _scrub_cloned_editable_installs(
    env_name: str,
    cwd: str,
    timeout: int,
    project_distribution: str = "",
) -> dict[str, Any]:
    """Remove project bindings and metadata from a disposable dependency clone."""

    script = r'''
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import sysconfig

site = pathlib.Path(sysconfig.get_paths()["purelib"])
norm = lambda value: re.sub(r"[-_.]+", "-", value).lower()
project_distribution = __BRT_PROJECT_DISTRIBUTION__
project_names = {norm(project_distribution)} if project_distribution else set()
def is_project_binding(path):
    filename = norm(path.name)
    return any(
        (path.suffix == ".egg-link" and (filename == name or filename.startswith(name + "-")))
        or ("editable" in filename and name in filename)
        or (path.name.endswith("-nspkg.pth") and filename.startswith(name + "-"))
        for name in project_names
        if name
    )
strict_tdd_scrub = os.environ.get("BRT_TDD_STRICT_LOCAL_CONDA") == "1"
workspace_markers = tuple(
    marker
    for marker in (
        os.environ.get("BRT_WORKSPACE_ROOT", "").rstrip("/"),
        "eval_clones",
        "/worktree",
    )
    if marker
)
editable = []
editable_metadata = []
project_metadata = []
for direct_url in site.glob("*.dist-info/direct_url.json"):
    try:
        data = json.loads(direct_url.read_text(encoding="utf-8"))
    except Exception:
        continue
    if not bool((data.get("dir_info") or {}).get("editable")):
        continue
    metadata = direct_url.parent / "METADATA"
    name = ""
    try:
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    if name:
        editable.append(name)
        editable_metadata.append(direct_url.parent)

for metadata_dir in list(site.glob("*.dist-info")) + list(site.glob("*.egg-info")):
    metadata = metadata_dir / "METADATA"
    if not metadata.is_file():
        metadata = metadata_dir / "PKG-INFO"
    name = ""
    try:
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    if name and norm(name) in project_names:
        project_metadata.append(metadata_dir)
        if name not in editable:
            editable.append(name)

uninstall = {"returncode": 0, "stdout": "", "stderr": ""}
if editable:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", *editable],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    uninstall = {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }

removed_editable_metadata = []
for metadata_dir in list(dict.fromkeys(editable_metadata + project_metadata)):
    if not metadata_dir.exists():
        continue
    try:
        shutil.rmtree(metadata_dir)
        removed_editable_metadata.append(str(metadata_dir))
    except OSError:
        pass

removed_pth = []
prefix = pathlib.Path(sys.prefix).resolve()
for pth in list(site.glob("*.pth")) + list(site.glob("*.egg-link")):
    try:
        text = pth.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
    except OSError:
        continue
    project_binding = is_project_binding(pth)
    external = project_binding or (strict_tdd_scrub and any(
        marker in text for marker in workspace_markers
    ))
    for raw in lines:
        line = raw.strip()
        if not line.startswith("/"):
            continue
        try:
            pathlib.Path(line).resolve().relative_to(prefix)
        except ValueError:
            external = True
            break
    if external:
        try:
            pth.unlink()
            removed_pth.append(str(pth))
        except OSError:
            pass

remaining_bindings = []
for direct_url in site.glob("*.dist-info/direct_url.json"):
    try:
        data = json.loads(direct_url.read_text(encoding="utf-8"))
    except Exception:
        continue
    if bool((data.get("dir_info") or {}).get("editable")):
        remaining_bindings.append(str(direct_url))
for binding in list(site.glob("*.pth")) + list(site.glob("*.egg-link")):
    try:
        text = binding.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
    except OSError:
        continue
    project_binding = is_project_binding(binding)
    external = project_binding or (strict_tdd_scrub and any(
        marker in text for marker in workspace_markers
    ))
    for raw in lines:
        line = raw.strip()
        if not line.startswith("/"):
            continue
        try:
            pathlib.Path(line).resolve().relative_to(prefix)
        except ValueError:
            external = True
            break
    if external:
        remaining_bindings.append(str(binding))

print(json.dumps({
    "project_distribution": project_distribution,
    "editable_distributions": editable,
    "uninstall": uninstall,
    "removed_editable_metadata": removed_editable_metadata,
    "removed_external_pth": removed_pth,
    "remaining_editable_bindings": remaining_bindings,
}))
'''
    script = script.replace(
        "__BRT_PROJECT_DISTRIBUTION__",
        repr(str(project_distribution or "")),
    )
    command = (
        f"{shlex.quote(CONDA_EXE)} run -n {shlex.quote(env_name)} "
        f"python -c {shlex.quote(script)}"
    )
    result = _run_script(command, cwd, min(max(timeout, 120), 300))
    try:
        payload = json.loads((result.get("stdout") or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {}
    result["details"] = payload
    remaining = (
        payload.get("remaining_editable_bindings")
        if isinstance(payload, dict)
        else ["scrub output could not be parsed"]
    )
    # A stale editable install can make pip report non-zero even after all
    # external worktree bindings are gone. The postcondition, rather than that
    # incidental return code, determines whether this isolated clone is safe.
    if result.get("returncode") == 0:
        result["returncode"] = 1 if remaining else 0
    return result


def _clone_validated_dependency_environment(
    source_env: str,
    target_env: str,
    cwd: str,
    timeout: int,
    project_distribution: str = "",
) -> dict[str, Any]:
    clone_command = (
        f"{shlex.quote(CONDA_EXE)} create -n {shlex.quote(target_env)} "
        f"--clone {shlex.quote(source_env)} -y"
    )
    clone = _run_script(clone_command, cwd, max(timeout, 1800))
    clone_attempts = [clone]
    retry_cleanup: dict[str, Any] = {}
    if clone.get("returncode") != 0:
        # Large scientific environments can exceed the first clone window
        # when several workers contend for the package cache.  A timed-out or
        # partial target must be removed before one bounded, longer retry;
        # otherwise Conda reports a misleading "prefix already exists" error.
        retry_cleanup = _remove_environment(
            target_env, cwd, max(timeout, 900)
        )
        retry = _run_script(clone_command, cwd, max(timeout * 2, 3600))
        clone_attempts.append(retry)
        if retry.get("returncode") != 0:
            return {
                "returncode": retry.get("returncode", 1),
                "clone": retry,
                "clone_attempts": clone_attempts,
                "retry_cleanup": retry_cleanup,
            }
        clone = retry
    scrub = _scrub_cloned_editable_installs(
        target_env,
        cwd,
        timeout,
        project_distribution=project_distribution,
    )
    if scrub.get("returncode") != 0:
        cleanup = _remove_environment(target_env, cwd, timeout)
        return {
            "returncode": scrub.get("returncode", 1),
            "clone": clone,
            "editable_scrub": scrub,
            "cleanup": cleanup,
        }
    return {
        "returncode": 0,
        "clone": clone,
        "clone_attempts": clone_attempts,
        "retry_cleanup": retry_cleanup,
        "editable_scrub": scrub,
    }


def _restore_runtime_dependency_contract(
    env_name: str,
    install_spec: dict[str, Any],
    cwd: str,
    timeout: int,
) -> dict[str, Any]:
    """Repair only packages whose versions drifted during ``conda --clone``."""

    before = dependency_env_compatibility(
        env_name, install_spec, timeout=min(max(timeout, 120), 300), refresh=True
    )
    if before.get("ok"):
        return {
            "status": "ALREADY_COMPATIBLE",
            "returncode": 0,
            "before": before,
            "requirements": [],
        }
    repairable_reasons = {
        "missing",
        "version_mismatch",
        "duplicate_metadata",
        "runtime_import_error",
        "runtime_interface_error",
        "runtime_version_mismatch",
    }
    applicable_checks = [
        check
        for check in before.get("requirement_checks", [])
        if check.get("applicable", True) and check.get("requirement")
    ]
    requirements_by_name = {
        str(check.get("name")): str(check.get("requirement"))
        for check in applicable_checks
        if check.get("name")
    }
    repair_names = {
        str(check.get("name"))
        for check in applicable_checks
        if not check.get("ok")
        and check.get("reason") in repairable_reasons
        and check.get("name")
    }
    # These packages form the ABI/build boundary implicated by the observed
    # Xarray and Scikit-learn failures.  Reinstall both sides together so a
    # valid wheel is never paired with stale headers or extension modules.
    pandas_requirement = requirements_by_name.get("pandas", "")
    try:
        pandas_is_pinned = bool(
            pandas_requirement and Requirement(pandas_requirement).specifier
        )
    except InvalidRequirement:
        pandas_is_pinned = False
    if "pandas" in repair_names or (
        "numpy" in repair_names and pandas_is_pinned
    ):
        repair_names.update(
            name for name in ("numpy", "pandas") if name in requirements_by_name
        )
    if repair_names & {"numpy", "cython"}:
        repair_names.update(
            name for name in ("numpy", "cython") if name in requirements_by_name
        )
    requirements = [
        str(check.get("requirement"))
        for check in applicable_checks
        if str(check.get("name") or "") in repair_names
    ]
    package_names = [
        str(check.get("name"))
        for check in applicable_checks
        if str(check.get("name") or "") in repair_names
    ]
    requirements = list(dict.fromkeys(requirements))
    package_names = list(dict.fromkeys(package_names))
    if not before.get("python_ok", True) or not requirements:
        return {
            "status": "UNREPAIRABLE_CONTRACT_DRIFT",
            "returncode": 1,
            "before": before,
            "requirements": requirements,
        }
    uninstalls = [
        _run_script(
            f"{shlex.quote(CONDA_EXE)} run -n {shlex.quote(env_name)} "
            f"python -m pip uninstall -y {shlex.quote(package_name)}",
            cwd,
            max(timeout, 600),
        )
        for package_name in package_names
        for _ in range(3)
    ]
    purge_script = r'''
import pathlib
import re
import shutil
import sysconfig

targets = set(__BRT_TARGET_DISTRIBUTIONS__)
norm = lambda value: re.sub(r"[-_.]+", "-", value).lower()
site = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
for metadata_dir in list(site.glob("*.dist-info")) + list(site.glob("*.egg-info")):
    metadata = metadata_dir / "METADATA"
    if not metadata.is_file():
        metadata = metadata_dir / "PKG-INFO"
    name = ""
    try:
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    if name and norm(name) in targets:
        shutil.rmtree(str(metadata_dir), ignore_errors=True)
'''.replace(
        "__BRT_TARGET_DISTRIBUTIONS__",
        repr(sorted(package_names)),
    )
    purge = _run_script(
        f"{shlex.quote(CONDA_EXE)} run -n {shlex.quote(env_name)} "
        f"python -c {shlex.quote(purge_script)}",
        cwd,
        max(timeout, 300),
    )
    install_command = (
        f"{shlex.quote(CONDA_EXE)} run -n {shlex.quote(env_name)} "
        "python -m pip install --no-cache-dir --force-reinstall --no-deps "
        + " ".join(shlex.quote(requirement) for requirement in requirements)
    )
    install = (
        _run_script(install_command, cwd, max(timeout, 600))
        if purge.get("returncode") == 0
        else {"returncode": 1, "error": "metadata purge failed"}
    )
    repair = {
        "returncode": install.get("returncode", 1),
        "uninstalls": uninstalls,
        "metadata_purge": purge,
        "install": install,
    }
    invalidate_environment_runtime_cache(env_name)
    after = dependency_env_compatibility(
        env_name, install_spec, timeout=min(max(timeout, 120), 300), refresh=True
    )
    return {
        "status": "RESTORED" if repair.get("returncode") == 0 and after.get("ok") else "RESTORE_ERROR",
        "returncode": 0 if repair.get("returncode") == 0 and after.get("ok") else 1,
        "before": before,
        "requirements": requirements,
        "repair": repair,
        "after": after,
    }


def isolated_runtime_env_name(
    template_env: str,
    instance_id: str,
    workspace: str,
    purpose: str,
) -> str:
    """Return a bounded identity for one disposable instance environment."""

    readable_instance = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id).strip("_")
    readable_purpose = re.sub(r"[^A-Za-z0-9_.-]+", "_", purpose).strip("_")
    payload = json.dumps(
        {
            "template_env": template_env,
            "instance_id": instance_id,
            "workspace": str(Path(workspace).resolve()),
            "purpose": purpose,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"brt5i_{readable_purpose[:12]}_{readable_instance[:28]}_{digest}"


def ensure_isolated_runtime_environment(
    template_env: str,
    instance_id: str,
    workspace: str,
    timeout: int,
    purpose: str,
    install_spec: dict[str, Any] | None = None,
    project_distribution: str = "",
) -> dict[str, Any]:
    """Clone an immutable dependency template for one generation/eval instance.

    Existing clones are deliberately replaced.  This prevents a resumed run
    from inheriting project installs or dependency-recovery changes made by an
    earlier attempt in the same workspace.
    """

    target_env = isolated_runtime_env_name(
        template_env, instance_id, workspace, purpose
    )
    template_manifest = environment_manifest(
        template_env, timeout=min(max(timeout, 120), 300), refresh=False
    )
    if not template_manifest.get("ok"):
        return {
            "status": "TEMPLATE_MANIFEST_ERROR",
            "returncode": 1,
            "env_name": target_env,
            "template_env_name": template_env,
            "template_manifest": template_manifest,
        }
    lock_path = environment_lock_path(target_env, "isolated_clone")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        inventory = conda_env_inventory(refresh=True)
        replacement: dict[str, Any] = {}
        if target_env in inventory:
            replacement = _remove_environment(target_env, workspace, timeout)
            if replacement.get("returncode") != 0:
                return {
                    "status": "ISOLATED_ENV_CLEANUP_ERROR",
                    "returncode": 1,
                    "env_name": target_env,
                    "template_env_name": template_env,
                    "template_manifest": template_manifest,
                    "cleanup": replacement,
                }
        clone = _clone_validated_dependency_environment(
            template_env,
            target_env,
            workspace,
            timeout,
            project_distribution=project_distribution,
        )
        if clone.get("returncode") != 0:
            return {
                "status": "ISOLATED_ENV_CLONE_ERROR",
                "returncode": 1,
                "env_name": target_env,
                "template_env_name": template_env,
                "template_manifest": template_manifest,
                "replacement": replacement,
                "clone": clone,
            }
        contract_restore: dict[str, Any] = {}
        if install_spec:
            contract_restore = _restore_runtime_dependency_contract(
                target_env, install_spec, workspace, timeout
            )
            if contract_restore.get("returncode") != 0:
                cleanup = _remove_environment(target_env, workspace, timeout)
                return {
                    "status": "ISOLATED_ENV_CONTRACT_ERROR",
                    "returncode": 1,
                    "env_name": target_env,
                    "template_env_name": template_env,
                    "template_manifest": template_manifest,
                    "clone": clone,
                    "contract_restore": contract_restore,
                    "cleanup": cleanup,
                }
        invalidate_environment_runtime_cache(target_env)
        health = env_health_check(target_env, refresh=True)
        runtime_manifest = environment_manifest(
            target_env, timeout=min(max(timeout, 120), 300), refresh=True
        )
        ready = bool(health.get("ok") and runtime_manifest.get("ok"))
        identity = {
            "kind": "isolated_runtime",
            "purpose": purpose,
            "instance_id": instance_id,
            "workspace": str(Path(workspace).resolve()),
            "template_env_name": template_env,
            "template_manifest_fingerprint": template_manifest.get("fingerprint", ""),
            "runtime_manifest_fingerprint": runtime_manifest.get("fingerprint", ""),
        }
        if ready:
            write_environment_identity(target_env, identity)
        else:
            cleanup = _remove_environment(target_env, workspace, timeout)
            return {
                "status": "ISOLATED_ENV_HEALTH_ERROR",
                "returncode": 1,
                "env_name": target_env,
                "template_env_name": template_env,
                "template_manifest": template_manifest,
                "runtime_manifest": runtime_manifest,
                "health": health,
                "cleanup": cleanup,
            }
        return {
            "status": "CREATED",
            "returncode": 0,
            "created": True,
            "env_name": target_env,
            "template_env_name": template_env,
            "template_manifest": template_manifest,
            "runtime_manifest": runtime_manifest,
            "health": health,
            "identity": identity,
            "replacement": replacement,
            "clone": clone,
            "contract_restore": contract_restore,
        }


def remove_isolated_runtime_environment(
    env_name: str,
    cwd: str,
    timeout: int,
) -> dict[str, Any]:
    """Remove only environments carrying the isolated-runtime identity."""

    identity = read_environment_identity(env_name)
    if identity.get("kind") != "isolated_runtime":
        return {
            "status": "SKIPPED_NOT_ISOLATED_RUNTIME",
            "returncode": 0,
            "env_name": env_name,
            "identity": identity,
        }
    lock_path = environment_lock_path(env_name, "isolated_clone")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not conda_env_exists(env_name):
            return {
                "status": "ALREADY_ABSENT",
                "returncode": 0,
                "env_name": env_name,
                "identity": identity,
            }
        result = _remove_environment(env_name, cwd, timeout)
        return {
            "status": "REMOVED" if result.get("returncode") == 0 else "REMOVE_ERROR",
            "returncode": result.get("returncode", 1),
            "env_name": env_name,
            "identity": identity,
            "removal": result,
        }


def make_instance_spec(
    instance_id: str,
    repo: str,
    version: str,
    base_commit: str,
    environment_setup_commit: str,
) -> Any:
    spec = make_exec_spec(
        {
            "instance_id": instance_id,
            "repo": repo,
            "version": version,
            "base_commit": base_commit,
            "environment_setup_commit": environment_setup_commit or base_commit,
            "test_patch": "",
        }
    )
    spec.test_directives = []
    return spec


def ensure_icore_environment(
    spec: Any,
    env_name: str,
    cwd: str,
    timeout: int,
) -> dict[str, Any]:
    env_script = str(getattr(spec, "env_script", "") or "")
    issue_meta = {
        "instance_id": str(getattr(spec, "instance_id", "") or ""),
        "repo": str(getattr(spec, "repo", "") or ""),
        "version": str(getattr(spec, "version", "") or ""),
        "base_commit": str(getattr(spec, "base_commit", "") or ""),
        "environment_setup_commit": str(
            getattr(spec, "environment_setup_commit", "")
            or getattr(spec, "base_commit", "")
            or ""
        ),
    }
    env_fingerprint = hashlib.sha256(env_script.encode("utf-8")).hexdigest()
    lifecycle_key = environment_cache_key(issue_meta, env_fingerprint)
    dependency_spec = dependency_install_spec(
        getattr(spec, "install", {}), env_script
    )
    lock_identity = env_name
    lock_path = environment_lock_path(lock_identity, "prepare")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        inventory = conda_env_inventory(refresh=True)
        if env_name not in inventory:
            # Recompute after taking the single-writer lock. Another worker may
            # have finished a compatible dependency environment while this
            # worker was waiting.
            seed_candidates = dependency_seed_candidates(
                issue_meta, inventory, env_name
            )
            expected_claim = {
                "repo": issue_meta["repo"],
                "version": issue_meta["version"],
                "environment_setup_commit": issue_meta["environment_setup_commit"],
                "environment_spec_fingerprint": env_fingerprint,
            }
            rejected_seeds: list[dict[str, Any]] = []
            for seed_env in seed_candidates:
                claim = read_environment_identity(seed_env)
                claim_matches = not claim or all(
                    str(claim.get(key) or "") == str(value or "")
                    for key, value in expected_claim.items()
                )
                compatibility = (
                    dependency_env_compatibility(
                        seed_env,
                        dependency_spec,
                        timeout=min(max(timeout, 120), 300),
                    )
                    if claim_matches
                    else {
                        "ok": False,
                        "category": "ENV_IDENTITY_CONFLICT",
                        "reason": "seed environment is claimed by a different dependency identity",
                        "recorded_identity": claim,
                        "expected_identity": expected_claim,
                    }
                )
                if not compatibility.get("ok"):
                    rejected_seeds.append(
                        {"env_name": seed_env, "compatibility": compatibility}
                    )
                    continue
                # A validated dependency environment is the immutable template.
                # Project installation and test execution happen only in the
                # instance-specific clone created by the caller.
                write_environment_state(
                    seed_env,
                    lifecycle_key,
                    {
                        "status": "ready",
                        **issue_meta,
                        "setup_script_fingerprint": env_fingerprint,
                        "source": "validated_dependency_reuse",
                        "requested_env": env_name,
                    },
                )
                health = env_health_check(seed_env, refresh=True)
                return {
                    "status": "REUSED",
                    "env_name": seed_env,
                    "requested_env": env_name,
                    "created": False,
                    "returncode": 0,
                    "health": health,
                    "resolution_source": "validated_dependency_reuse",
                    "compatibility": compatibility,
                    "reused_from": seed_env,
                    "rejected_seed_environments": rejected_seeds,
                }
        if conda_env_exists(env_name):
            compatibility = dependency_env_compatibility(
                env_name,
                dependency_spec,
                timeout=min(max(timeout, 120), 300),
                refresh=True,
            )
            dependency_repair: dict[str, Any] = {}
            if not compatibility.get("ok") and compatibility.get(
                "python_ok", True
            ):
                dependency_repair = _restore_runtime_dependency_contract(
                    env_name,
                    dependency_spec,
                    cwd,
                    timeout,
                )
                if dependency_repair.get("returncode") == 0:
                    compatibility = dependency_repair.get("after") or compatibility
            prior_state = read_environment_state(env_name, lifecycle_key)
            recorded_identity = read_environment_identity(env_name)
            managed_names = {
                default_env_name(issue_meta, prefix=""),
                default_env_name(
                    issue_meta,
                    prefix=os.environ.get("BRT4_CONDA_ENV_PREFIX"),
                ),
            }
            recoverable_partial = str(prior_state.get("status") or "") in {
                "creating",
                "installing",
                "validating",
                "failed",
            } or has_recoverable_environment_state(env_name)
            managed_environment = bool(recorded_identity) or env_name in managed_names
            if not compatibility.get("ok") and (
                recoverable_partial or managed_environment
            ):
                removal = _remove_environment(env_name, cwd, timeout)
                if removal.get("returncode") != 0:
                    write_environment_state(
                        env_name,
                        lifecycle_key,
                        {
                            "status": "failed",
                            **issue_meta,
                            "setup_script_fingerprint": env_fingerprint,
                            "source": "partial_environment_cleanup",
                            "compatibility": compatibility,
                            "cleanup": removal,
                        },
                    )
                    return {
                        "status": "ENV_HEALTH_ERROR",
                        "env_name": env_name,
                        "requested_env": env_name,
                        "created": False,
                        "returncode": 1,
                        "health": compatibility,
                        "resolution_source": "partial_environment_cleanup_failed",
                    }
                conda_env_inventory(refresh=True)
            elif not compatibility.get("ok"):
                write_environment_state(
                    env_name,
                    lifecycle_key,
                    {
                        "status": "failed",
                        **issue_meta,
                        "setup_script_fingerprint": env_fingerprint,
                        "source": "dependency_environment_reuse",
                        "dependency_compatibility": compatibility,
                    },
                )
                return {
                    "status": "ENV_HEALTH_ERROR",
                    "env_name": env_name,
                    "requested_env": env_name,
                    "created": False,
                    "returncode": 1,
                    "health": compatibility,
                    "resolution_source": "dependency_environment_incompatible",
                    "compatibility": compatibility,
                }
        if conda_env_exists(env_name):
            health = env_health_check(env_name, refresh=True)
            write_environment_state(
                env_name,
                lifecycle_key,
                {
                    "status": "ready" if health.get("ok") else "failed",
                    **issue_meta,
                    "setup_script_fingerprint": env_fingerprint,
                    "python_spec": health.get("version", ""),
                    "project_health": health,
                    "dependency_compatibility": compatibility,
                    "dependency_repair": dependency_repair,
                    "source": "conda_environment_reuse",
                },
            )
            return {
                "status": "REUSED" if health.get("ok") else "ENV_HEALTH_ERROR",
                "env_name": env_name,
                "requested_env": env_name,
                "created": False,
                "returncode": 0 if health.get("ok") else 1,
                "health": health,
                "resolution_source": "dependency_environment_reuse",
                "compatibility": compatibility,
                "dependency_repair": dependency_repair,
            }
        write_environment_state(
            env_name,
            lifecycle_key,
            {
                "status": "installing",
                **issue_meta,
                "setup_script_fingerprint": env_fingerprint,
                "source": "conda_environment_create",
            },
        )
        script = env_script.replace(spec.env_name, env_name)
        script = script.replace(
            "source ~/miniconda3/bin/activate", f"source {CONDA_SH}"
        )
        script, requirements_path = _isolate_environment_script_files(
            script, cwd, env_fingerprint
        )
        script_path = Path(cwd) / "brt3_icore_env_setup.sh"
        script_path.write_text(script, encoding="utf-8")
        result = _run_script(f"bash {script_path}", cwd, max(timeout, 1800))
        result.update(
            {
                "status": "CREATED" if result["returncode"] == 0 else "CREATE_ERROR",
                "env_name": env_name,
                "created": result["returncode"] == 0,
                "script_path": str(script_path),
                "requirements_path": requirements_path,
            }
        )
        if result["returncode"] == 0:
            invalidate_environment_runtime_cache(env_name)
            conda_env_inventory(refresh=True)
            write_environment_state(
                env_name,
                lifecycle_key,
                {
                    "status": "validating",
                    **issue_meta,
                    "setup_script_fingerprint": env_fingerprint,
                    "source": "conda_environment_create",
                },
            )
            health = env_health_check(env_name, refresh=True)
            compatibility = dependency_env_compatibility(
                env_name,
                dependency_spec,
                timeout=min(max(timeout, 120), 300),
                refresh=True,
            )
            dependency_repair: dict[str, Any] = {}
            if health.get("ok") and not compatibility.get("ok"):
                dependency_repair = _restore_runtime_dependency_contract(
                    env_name,
                    dependency_spec,
                    cwd,
                    timeout,
                )
                if dependency_repair.get("returncode") == 0:
                    compatibility = dependency_repair.get("after") or compatibility
            if health.get("ok") and not compatibility.get("ok"):
                health = {
                    **health,
                    "ok": False,
                    "category": "ENV_INCOMPLETE",
                    "dependency_compatibility": compatibility,
                }
            result["health"] = health
            result["compatibility"] = compatibility
            result["dependency_repair"] = dependency_repair
            if not health.get("ok"):
                result["returncode"] = 1
                result["status"] = "ENV_HEALTH_ERROR"
                result["created"] = False
        health = result.get("health") if isinstance(result.get("health"), dict) else {}
        ready = result.get("returncode") == 0 and bool(health.get("ok"))
        if ready:
            write_environment_identity(
                env_name,
                {
                    "repo": issue_meta["repo"],
                    "version": issue_meta["version"],
                    "environment_setup_commit": issue_meta["environment_setup_commit"],
                    "environment_spec_fingerprint": env_fingerprint,
                    "source": "dependency_environment_create",
                },
            )
        write_environment_state(
            env_name,
            lifecycle_key,
            {
                "status": "ready" if ready else "failed",
                **issue_meta,
                "setup_script_fingerprint": env_fingerprint,
                "python_spec": health.get("version", ""),
                "project_health": health,
                "source": "conda_environment_create",
            },
        )
        return result


def env_lock_path(env_name: str, suffix: str) -> Path:
    return environment_lock_path(env_name, suffix)


def _build_dependency_command(repo_path: str) -> str:
    pyproject = Path(repo_path) / "pyproject.toml"
    if not pyproject.exists() or tomllib is None:
        return ""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return ""
    requirements = data.get("build-system", {}).get("requires", [])
    selected = []
    for requirement in requirements:
        normalized = re.split(r"[<>=!~;\\[]", str(requirement), 1)[0]
        normalized = normalized.strip().lower().replace("_", "-")
        if normalized in {"setuptools", "oldest-supported-numpy", "numpy"}:
            continue
        selected.append(str(requirement))
    if not selected:
        return ""
    quoted = " ".join(shlex.quote(item) for item in selected)
    return f"python -m pip install {quoted}"


def _project_runtime_dependency_command(repo_path: str) -> str:
    """Install declared runtime dependencies without installing the project.

    Generation deliberately installs the checkout itself with ``--no-deps`` so
    a shared dependency environment never remains bound to a stale worktree.
    Projects such as Pylint declare essential runtime packages (notably dill)
    outside the benchmark test requirements, so PEP 621 and setup.cfg
    declarations must be installed explicitly first.
    """

    selected: list[str] = []
    pyproject = Path(repo_path) / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if tomllib is not None:
            try:
                data = tomllib.loads(text)
            except Exception:
                data = {}
            requirements = data.get("project", {}).get("dependencies", [])
        else:
            # Python 3.10 has no stdlib TOML parser. PEP 621 dependencies are
            # a literal string array, so parse only that bounded field rather
            # than adding a runtime dependency to the benchmark controller.
            section = re.search(
                r"(?ms)^\[project\]\s*(.*?)(?=^\[[^]]+\]|\Z)", text
            )
            match = re.search(
                r"(?ms)^\s*dependencies\s*=\s*(\[.*?\])",
                section.group(1) if section else "",
            )
            try:
                parsed = ast.literal_eval(match.group(1)) if match else []
            except (SyntaxError, ValueError):
                parsed = []
            requirements = parsed if isinstance(parsed, list) else []
        selected.extend(
            str(value).strip() for value in requirements if str(value).strip()
        )

    setup_cfg = Path(repo_path) / "setup.cfg"
    if setup_cfg.exists():
        parser = configparser.ConfigParser(
            interpolation=None,
            inline_comment_prefixes=("#",),
        )
        try:
            parser.read(setup_cfg, encoding="utf-8")
            install_requires = parser.get(
                "options", "install_requires", fallback=""
            )
        except (configparser.Error, OSError):
            install_requires = ""
        selected.extend(
            cleaned
            for line in install_requires.splitlines()
            if (cleaned := re.sub(r"\s+#.*$", "", line).strip())
            and not cleaned.startswith(("#", ";"))
        )

    selected = list(dict.fromkeys(selected))
    if not selected:
        return ""
    return "python -m pip install " + " ".join(shlex.quote(item) for item in selected)


def icore_setup_command(spec: Any, repo_path: str = "") -> str:
    install = spec.install
    commands = list(install.get("pre_install", []))
    is_matplotlib = getattr(spec, "repo", "") == "matplotlib/matplotlib"
    is_sphinx = getattr(spec, "repo", "") == "sphinx-doc/sphinx"
    is_legacy_astropy = (
        getattr(spec, "repo", "") == "astropy/astropy"
        and str(getattr(spec, "version", "")).startswith("1.3")
    )
    if is_matplotlib:
        # setuptools_scm otherwise runs git describe against the large shared
        # repository behind this worktree and applies its own 40-second
        # timeout. The benchmark checkout identity is already fixed, so use a
        # deterministic build version and the prepared environment's backend.
        commands.append(
            "export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MATPLOTLIB=3.6.0.dev0"
        )
        # Matplotlib 3.5 enables LTO automatically, including for its vendored
        # FreeType build.  On shared benchmark hosts this turns every link into
        # a multi-minute operation and can exhaust the setup timeout.  The test
        # behavior is independent of link-time optimization.
        commands.append(
            "export CFLAGS=\"${CFLAGS:-} -fno-lto\" CPPFLAGS=\"${CPPFLAGS:-} -fno-lto\" "
            "CXXFLAGS=\"${CXXFLAGS:-} -fno-lto\" LDFLAGS=\"${LDFLAGS:-} -fno-lto\""
        )
    if (
        getattr(spec, "repo", "") == "matplotlib/matplotlib"
        and str(getattr(spec, "version", "")) in {"3.0", "3.1", "3.2", "3.3", "3.4"}
    ):
        # These releases still import pkg_resources and run their test suite
        # with deprecation warnings promoted to errors.  Setuptools 67.5+
        # emits a pkg_resources API deprecation before the generated test can
        # be collected.
        commands.append(
            "python -m pip install --force-reinstall 'setuptools==65.5.1'"
        )
        # The requirements snapshots for these releases contain Python
        # packages but not the native FreeType headers used by build_ext.
        # Install them only when absent, and only in the disposable per-instance
        # clone (the immutable dependency template remains untouched).
        commands.append(
            "if test -f \"$CONDA_PREFIX/include/freetype2/ft2build.h\"; "
            "then :; else conda install -y -c conda-forge freetype pkg-config; fi"
        )
    if repo_path and "--no-build-isolation" in str(install.get("install", "")):
        build_deps = _build_dependency_command(repo_path)
        if build_deps:
            commands.append(build_deps)
    if repo_path:
        runtime_deps = _project_runtime_dependency_command(repo_path)
        if runtime_deps:
            commands.append(runtime_deps)
    if is_sphinx:
        # Inheritance-diagram tests are otherwise reported as a successful
        # all-skipped run when the Graphviz executable is absent.
        commands.append(
            "if command -v dot >/dev/null 2>&1; then :; "
            "else conda install -y -c conda-forge graphviz; fi"
        )
    if install.get("install"):
        project_install = str(install["install"])
        if is_legacy_astropy:
            # Modern pip routes this setup.py-era release through a PEP 517
            # editable-wheel build that fails while creating generated Cython
            # files in a temporary build-lib tree. Use setuptools' native
            # develop command, which is the editable mechanism this release
            # was written for.
            project_install = "python setup.py develop --no-deps"
        else:
            if is_matplotlib and "python -m pip install" in project_install:
                project_install = project_install.replace(
                    "python -m pip install",
                    "python -m pip install --no-build-isolation",
                    1,
                )
            if (
                "python -m pip install" in project_install
                and "--no-deps" not in project_install
                and not is_sphinx
            ):
                project_install = project_install.replace(
                    "python -m pip install",
                    "python -m pip install --no-deps",
                    1,
                )
        commands.append(project_install)
    commands.extend(install.get("eval_commands", []))
    command = " && ".join(commands)
    command = command.replace("--no-use-pep517 ", "")
    command = command.replace(
        "sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen",
        "(test ! -f /etc/locale.gen || (sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && (command -v locale-gen >/dev/null 2>&1 && locale-gen || true)))",
    )
    return command


def first_test_selector(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("test"):
                        return f"{node.name}::{child.name}"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                return node.name
    return ""


def icore_test_command(
    repo: str,
    version: str,
    test_path: str,
    selector: str = "",
) -> str:
    project = repo.split("/")[-1]
    nodeid = test_path if not selector else f"{test_path}::{selector}"
    if project == "sphinx":
        return f"tox --current-env -epy39 -v -- {nodeid}"
    if project in {
        "astropy",
        "matplotlib",
        "flask",
        "xarray",
        "pylint",
        "scikit-learn",
        "requests",
    }:
        return (
            "python -m pytest --no-header --tb=short --show-capture=no "
            f"--disable-warnings -p no:cacheprovider {nodeid}"
        )
    if project == "seaborn":
        return f"pytest --no-header --show-capture=no --disable-warnings {nodeid}"
    if project == "pytest":
        return f"pytest --disable-warnings --show-capture=no {nodeid} -v"
    if project == "django":
        label = nodeid.replace(".py", "").replace("/", ".").replace("::", ".")
        if label.startswith("tests."):
            label = label[len("tests.") :]
        if test_path.startswith("tests/gis_tests/"):
            runner = (
                Path(__file__).resolve().parents[1]
                / "runtime"
                / "django_generated_test_runner.py"
            )
            return (
                f"python {shlex.quote(str(runner))} --settings test_sqlite "
                f"--verbosity 2 --label {shlex.quote(label)}"
            )
        return f"./tests/runtests.py --settings=test_sqlite {label}"
    if project == "sympy":
        test_name = selector.split("::")[-1] if selector else ""
        suffix = f" -k {test_name}" if test_name else ""
        return (
            f"PYTHONPATH={shlex.quote(str(LEGACY_SYMPY_COMPAT_PATH))}:"
            "${PYTHONPATH:-} "
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning,"
            "ignore::DeprecationWarning' "
            f"bin/test -C {test_path}{suffix}"
        )
    raise ValueError(f"unsupported iCoRe project: {repo} version={version}")


def dump_spec(spec: Any, path: str) -> None:
    data = {
        "instance_id": getattr(spec, "instance_id", ""),
        "repo": getattr(spec, "repo", ""),
        "version": getattr(spec, "version", ""),
        "base_commit": getattr(spec, "base_commit", ""),
        "environment_setup_commit": getattr(spec, "environment_setup_commit", ""),
        "env_name": getattr(spec, "env_name", ""),
        "install": getattr(spec, "install", {}),
        "test_cmd": getattr(spec, "test_cmd", ""),
    }
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

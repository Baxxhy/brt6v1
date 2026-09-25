"""Conda environment identity, resolution, and health checks for BRT6.

The generation path is the source of truth for the environment actually used by
an instance. Formal evaluation must prefer that recorded identity instead of
reconstructing an environment name from the run name.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


DEFAULT_CONDA_EXE = next(
    (
        path
        for path in (
            shutil.which("conda"),
            str(Path.home() / "miniforge3/bin/conda"),
            str(Path.home() / "miniconda3/bin/conda"),
            "/root/conda/ENTER/bin/conda",
        )
        if path and Path(path).is_file()
    ),
    "conda",
)
CONDA_EXE = os.environ.get("CONDA_EXE", DEFAULT_CONDA_EXE)
ENV_NAMING_SCHEME_VERSION = "brt5-conda-env-v4"
ENV_CACHE_STATE_VERSION = "brt5-environment-cache-v1"
DEFAULT_CONDA_PROBE_TIMEOUT_SECONDS = 20.0

_INVENTORY_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_HEALTH_CACHE: dict[str, dict[str, Any]] = {}
_DEPENDENCY_COMPAT_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_MANIFEST_CACHE: dict[str, dict[str, Any]] = {}

# Importing these modules is part of the dependency contract.  Metadata alone
# cannot detect a NumPy/Pandas ABI mismatch or a Cython/pyparsing installation
# whose files and dist-info came from different versions.
_DEPENDENCY_IMPORT_PROBES = {
    "cython": "Cython",
    "numpy": "numpy",
    "pandas": "pandas",
    "pyparsing": "pyparsing",
    "setuptools": "setuptools",
}

_PROJECT_HEALTH_IMPORTS = {
    "matplotlib": ["pyparsing", "matplotlib", "matplotlib.pyplot"],
    "scikit-learn": ["numpy", "Cython", "sklearn"],
    "xarray": ["numpy", "pandas", "xarray"],
}


def sanitize_env_component(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:96]


def environment_cache_component(env_name: str) -> str:
    """Return a readable, collision-resistant key for cache files and locks."""

    raw = str(env_name or "")
    readable = sanitize_env_component(raw) or "unknown_env"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{readable}_{digest}"


def legacy_env_name(issue: dict[str, Any], prefix: str | None = None) -> str:
    repo = str(issue.get("repo") or "")
    version = str(issue.get("version") or "")
    if not repo or "/" not in repo or not version:
        return ""
    owner, name = repo.split("/", 1)
    if prefix is None:
        prefix = str(os.environ.get("BRT4_CONDA_ENV_PREFIX") or "")
    return (
        f"{sanitize_env_component(prefix)}"
        f"setup_{sanitize_env_component(owner)}_{sanitize_env_component(name)}__"
        f"{sanitize_env_component(version)}"
    )


def default_env_name(issue: dict[str, Any], prefix: str | None = None) -> str:
    """Return the deterministic dependency-environment identity.

    Conda dependencies are defined by the benchmark environment setup commit,
    not by every issue's buggy source commit. Source isolation remains keyed by
    ``base_commit`` in :func:`environment_cache_key` and in the clean worktree.
    """

    base = legacy_env_name(issue, prefix)
    if not base:
        return ""
    commit = str(
        issue.get("environment_setup_commit")
        or issue.get("base_commit")
        or ""
    ).strip()
    if not commit:
        return base
    digest = sanitize_env_component(commit)[:12]
    return f"{base}__{digest}"


def dependency_seed_candidates(
    issue: dict[str, Any],
    inventory: dict[str, str] | set[str],
    target_env: str,
) -> list[str]:
    """Return deterministic, exact-identity seeds for a new dependency env.

    BRT6 historically created both unprefixed dependency environments and
    ``direct_brt_we<N>_`` worker environments.  Those worker environments may
    be used only as clone sources after dependency-contract validation; an
    arbitrary run-prefixed environment is deliberately never considered.
    """

    names = set(inventory)
    canonical = default_env_name(issue, prefix="")
    legacy = legacy_env_name(issue, prefix="")
    candidates: list[str] = []
    for name in (canonical, legacy):
        if name and name != target_env and name in names and name not in candidates:
            candidates.append(name)
    if legacy:
        direct_pattern = re.compile(
            rf"^direct_brt_we[0-9]+_{re.escape(legacy)}$"
        )
        candidates.extend(
            name
            for name in sorted(names)
            if name != target_env
            and name not in candidates
            and direct_pattern.fullmatch(name)
        )
    return candidates


def environment_cache_key(
    issue: dict[str, Any],
    setup_script_fingerprint: str,
    python_spec: str = "",
) -> str:
    payload = {
        "state_version": ENV_CACHE_STATE_VERSION,
        "naming_scheme": ENV_NAMING_SCHEME_VERSION,
        "repo": str(issue.get("repo") or ""),
        "version": str(issue.get("version") or ""),
        "base_commit": str(issue.get("base_commit") or ""),
        "environment_setup_commit": str(
            issue.get("environment_setup_commit")
            or issue.get("base_commit")
            or ""
        ),
        "setup_script_fingerprint": setup_script_fingerprint,
        "python_spec": python_spec,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def environment_cache_root() -> Path:
    return Path(
        os.environ.get(
            "BRT4_ENV_CACHE_DIR",
            str(Path.home() / ".cache" / "brt5" / "environments"),
        )
    )


def environment_lock_path(env_name: str, suffix: str = "runtime") -> Path:
    safe_env = environment_cache_component(env_name)
    safe_suffix = sanitize_env_component(suffix) or "runtime"
    return environment_cache_root() / "_locks" / (
        f"brt4_env_{safe_env}_{safe_suffix}.lock"
    )


@contextlib.contextmanager
def environment_operation_lock(env_name: str, suffix: str = "runtime"):
    """Serialize mutable environment operations across BRT6 processes."""

    path = environment_lock_path(env_name, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def environment_state_path(env_name: str, cache_key: str) -> Path:
    safe_env = environment_cache_component(env_name)
    return environment_cache_root() / safe_env / f"{cache_key}.json"


def environment_identity_path(env_name: str) -> Path:
    safe_env = environment_cache_component(env_name)
    return environment_cache_root() / safe_env / "identity.json"


def read_environment_identity(env_name: str) -> dict[str, Any]:
    try:
        value = json.loads(environment_identity_path(env_name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_environment_identity(env_name: str, value: dict[str, Any]) -> Path:
    path = environment_identity_path(env_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "naming_scheme": ENV_NAMING_SCHEME_VERSION,
        "env_name": env_name,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **value,
    }
    temporary = path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "")).lower()


def _requirement_distribution_name(value: str) -> str:
    """Return the normalized distribution named by a PEP 508 requirement."""

    try:
        return _normalized_distribution_name(Requirement(str(value or "").strip()).name)
    except InvalidRequirement:
        return ""


def dependency_install_spec(
    install_spec: dict[str, Any] | None,
    environment_script: str = "",
) -> dict[str, Any]:
    """Return the dependency contract hidden behind iCoRe install metadata.

    Some benchmark specs expose ``pip_packages`` directly. Older Django specs
    instead write a requirements file from a shell heredoc and only record the
    filename in ``install['packages']``. Normalize both forms before deciding
    whether an existing dependency environment is reusable.
    """

    normalized = dict(install_spec) if isinstance(install_spec, dict) else {}
    explicit_requirements = [
        str(value).strip()
        for value in (normalized.get("pip_packages") or [])
        if str(value).strip() and not str(value).strip().startswith("-")
    ]
    requirements: list[str] = []
    package_source = str(normalized.get("packages") or "").strip()
    if package_source.lower().endswith(("requirements.txt", "requirements-dev.txt")):
        pattern = re.compile(
            r"cat\s+<<['\"]?(?P<tag>[A-Za-z0-9_]+)['\"]?\s*>\s*[^\n]*requirements[^\n]*\n"
            r"(?P<body>.*?)\n(?P=tag)\s*$",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(str(environment_script or ""))
        if match:
            requirements.extend(
                cleaned
                for line in match.group("body").splitlines()
                if (cleaned := re.sub(r"\s+#.*$", "", line).strip())
                and not cleaned.startswith("#")
                and not cleaned.startswith("-")
            )
    elif package_source and not package_source.lower().endswith(
        (".txt", ".yml", ".yaml", ".toml")
    ):
        requirements.extend(shlex.split(package_source))

    # req_install_commands installs a requirements file first and the explicit
    # pip_packages list afterwards.  Model that order in the compatibility
    # contract: a later explicit pin replaces an earlier requirement for the
    # same distribution (for example MarkupSafe 2.1.1 after 2.1.2). Pip command
    # options such as --prefer-binary influence installation but are not package
    # requirements and therefore cannot be checked against installed metadata.
    overridden_names = {
        name
        for value in explicit_requirements
        if (name := _requirement_distribution_name(value))
    }
    requirements = [
        value
        for value in requirements
        if not value.startswith("-")
        and _requirement_distribution_name(value) not in overridden_names
    ]
    requirements.extend(explicit_requirements)
    normalized["pip_packages"] = list(dict.fromkeys(requirements))
    return normalized


def _requirement_compatibility(
    requirement_text: str,
    installed_packages: dict[str, str],
    marker_environment: dict[str, str],
) -> dict[str, Any]:
    """Evaluate one PEP 508 requirement against a target environment snapshot."""

    try:
        requirement = Requirement(str(requirement_text or "").strip())
    except InvalidRequirement:
        return {
            "requirement": requirement_text,
            "applicable": True,
            "ok": False,
            "reason": "invalid_requirement",
        }
    if requirement.marker and not requirement.marker.evaluate(marker_environment):
        return {
            "requirement": requirement_text,
            "name": _normalized_distribution_name(requirement.name),
            "applicable": False,
            "ok": True,
            "reason": "marker_not_applicable",
        }
    name = _normalized_distribution_name(requirement.name)
    installed = str(installed_packages.get(name) or "")
    if not installed:
        return {
            "requirement": requirement_text,
            "name": name,
            "applicable": True,
            "ok": False,
            "reason": "missing",
        }
    try:
        version_matches = not requirement.specifier or Version(installed) in requirement.specifier
    except InvalidVersion:
        version_matches = False
    return {
        "requirement": requirement_text,
        "name": name,
        "applicable": True,
        "ok": bool(version_matches),
        "reason": "compatible" if version_matches else "version_mismatch",
        "installed": installed,
        "specifier": str(requirement.specifier),
    }


def dependency_env_compatibility(
    env_name: str,
    install_spec: dict[str, Any] | None,
    timeout: int = 120,
    refresh: bool = False,
) -> dict[str, Any]:
    """Verify a legacy dependency env before importing it into v4 metadata."""

    install_spec = install_spec if isinstance(install_spec, dict) else {}
    expected_python = str(install_spec.get("python") or "").strip()
    expected_requirements = [
        str(value) for value in (install_spec.get("pip_packages") or []) if value
    ]
    expected_names = {
        name
        for value in expected_requirements
        if (name := _requirement_distribution_name(value))
    }
    probed_names: set[str] = set()
    for requirement_text in expected_requirements:
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement:
            continue
        name = _normalized_distribution_name(requirement.name)
        # Pandas is a comparatively expensive optional test dependency for
        # several projects.  Its ABI is contractual only where the benchmark
        # pins it (notably Xarray).
        if name == "pandas" and not requirement.specifier:
            continue
        probed_names.add(name)
    import_probes = {
        name: module
        for name, module in _DEPENDENCY_IMPORT_PROBES.items()
        if name in probed_names
    }
    expectation_key = hashlib.sha256(
        json.dumps(
            {
                "python": expected_python,
                "requirements": expected_requirements,
                "integrity_probe": "metadata-runtime-interface-v3",
                "import_probes": import_probes,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if refresh:
        _DEPENDENCY_COMPAT_CACHE.pop((env_name, expectation_key), None)
    cached = _DEPENDENCY_COMPAT_CACHE.get((env_name, expectation_key))
    if cached is not None:
        return {**cached, "cache_hit": True}
    inventory = conda_env_inventory(refresh=refresh)
    env_path = inventory.get(env_name, "")
    if not env_path:
        return {
            "ok": False,
            "category": "ENV_NOT_FOUND",
            "env_name": env_name,
            "env_path": "",
            "reason": "environment is absent from active Conda envs_dirs",
        }
    script = """
import json
import os
import platform
import re
import subprocess
import sys

norm = lambda value: re.sub(r"[-_.]+", "-", value).lower()
package_records = {}
try:
    from importlib.metadata import distributions
except ImportError:
    try:
        from importlib_metadata import distributions
    except ImportError:
        distributions = None
if distributions is not None:
    for distribution in distributions():
        name = norm(distribution.metadata.get("Name") or "")
        if not name:
            continue
        record = {
            "version": str(distribution.version or ""),
            "metadata_path": str(getattr(distribution, "_path", "") or ""),
        }
        if record not in package_records.setdefault(name, []):
            package_records[name].append(record)
else:
    import pkg_resources
    for distribution in pkg_resources.working_set:
        name = norm(distribution.project_name)
        record = {
            "version": str(distribution.version or ""),
            "metadata_path": str(getattr(distribution, "egg_info", "") or ""),
        }
        if record not in package_records.setdefault(name, []):
            package_records[name].append(record)
packages = {
    name: records[-1]["version"]
    for name, records in package_records.items()
    if records
}
duplicate_metadata = {
    name: records for name, records in package_records.items() if len(records) > 1
}
probe_modules = __BRT_IMPORT_PROBES__
runtime_probes = {}
probe_code = r'''
import importlib
import json
import sys
module_name = sys.argv[1]
module = importlib.import_module(module_name)
print(json.dumps({
    "version": str(getattr(module, "__version__", "") or ""),
    "module_file": str(getattr(module, "__file__", "") or ""),
}))
'''
for distribution_name, module_name in probe_modules.items():
    try:
        probe = subprocess.run(
            [sys.executable, "-c", probe_code, module_name],
            env=dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1"),
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        try:
            payload = json.loads((probe.stdout or "").strip().splitlines()[-1])
        except (IndexError, ValueError):
            payload = {}
        runtime_probes[distribution_name] = dict({
            "ok": probe.returncode == 0 and bool(payload),
            "module": module_name,
            "version": str(payload.get("version") or ""),
            "module_file": str(payload.get("module_file") or ""),
            "returncode": probe.returncode,
            "stderr": (probe.stderr or "")[-4000:],
        })
    except subprocess.TimeoutExpired as exc:
        runtime_probes[distribution_name] = {
            "ok": False,
            "module": module_name,
            "version": "",
            "module_file": "",
            "timeout": True,
            "error": repr(exc),
        }
interface_probes = {}
if (
    "cython" in probe_modules
    and "numpy" in probe_modules
    and runtime_probes.get("cython", {}).get("ok")
    and runtime_probes.get("numpy", {}).get("ok")
):
    cython_numpy_probe = r'''
import pathlib
import tempfile
from Cython.Build import cythonize

with tempfile.TemporaryDirectory(prefix="brt-cython-numpy-") as root:
    source = pathlib.Path(root) / "probe.pyx"
    source.write_text(
        "cimport numpy as cnp\\n"
        "cdef cnp.int_t brt_integrity_value\\n",
        encoding="utf-8",
    )
    cythonize(
        str(source),
        quiet=True,
        compiler_directives={"language_level": 3},
    )
'''
    try:
        interface = subprocess.run(
            [sys.executable, "-c", cython_numpy_probe],
            env=dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1"),
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        interface_probes["cython_numpy_pxd"] = {
            "ok": interface.returncode == 0,
            "returncode": interface.returncode,
            "stdout": (interface.stdout or "")[-4000:],
            "stderr": (interface.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        interface_probes["cython_numpy_pxd"] = {
            "ok": False,
            "timeout": True,
            "error": repr(exc),
        }
marker_environment = {
    "implementation_name": getattr(sys.implementation, "name", ""),
    "implementation_version": platform.python_version(),
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "platform_python_implementation": platform.python_implementation(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "sys_platform": sys.platform,
    "extra": "",
}
print(json.dumps({
    "python": platform.python_version(),
    "packages": packages,
    "package_records": package_records,
    "duplicate_metadata": duplicate_metadata,
    "runtime_probes": runtime_probes,
    "interface_probes": interface_probes,
    "marker_environment": marker_environment,
}))
"""
    script = script.replace(
        "__BRT_IMPORT_PROBES__",
        json.dumps(import_probes, sort_keys=True),
    )
    try:
        proc = subprocess.run(
            [CONDA_EXE, "run", "-p", env_path, "python", "-c", script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "category": "ENV_INCOMPLETE",
            "reason": "dependency inventory timed out",
            "stderr": str(exc),
        }
    try:
        snapshot = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        snapshot = {}
    packages = snapshot.get("packages") if isinstance(snapshot.get("packages"), dict) else {}
    duplicate_metadata = (
        snapshot.get("duplicate_metadata")
        if isinstance(snapshot.get("duplicate_metadata"), dict)
        else {}
    )
    runtime_probes = (
        snapshot.get("runtime_probes")
        if isinstance(snapshot.get("runtime_probes"), dict)
        else {}
    )
    interface_probes = (
        snapshot.get("interface_probes")
        if isinstance(snapshot.get("interface_probes"), dict)
        else {}
    )
    marker_environment = (
        snapshot.get("marker_environment")
        if isinstance(snapshot.get("marker_environment"), dict)
        else {}
    )
    missing: list[str] = []
    mismatches: list[dict[str, str]] = []
    unsupported: list[str] = []
    requirement_checks: list[dict[str, Any]] = []
    for requirement in expected_requirements:
        check = _requirement_compatibility(requirement, packages, marker_environment)
        name = str(check.get("name") or "")
        duplicate_records = duplicate_metadata.get(name)
        runtime_probe = runtime_probes.get(name)
        if check.get("applicable", True) and duplicate_records:
            check.update(
                {
                    "ok": False,
                    "reason": "duplicate_metadata",
                    "metadata_records": duplicate_records,
                }
            )
        elif check.get("ok") and isinstance(runtime_probe, dict):
            if not runtime_probe.get("ok"):
                check.update(
                    {
                        "ok": False,
                        "reason": "runtime_import_error",
                        "runtime_probe": runtime_probe,
                    }
                )
            else:
                imported_version = str(runtime_probe.get("version") or "")
                try:
                    parsed_requirement = Requirement(requirement)
                    imported_matches = (
                        not imported_version
                        or not parsed_requirement.specifier
                        or Version(imported_version) in parsed_requirement.specifier
                    )
                except (InvalidRequirement, InvalidVersion):
                    imported_matches = False
                if not imported_matches:
                    check.update(
                        {
                            "ok": False,
                            "reason": "runtime_version_mismatch",
                            "runtime_probe": runtime_probe,
                            "imported": imported_version,
                        }
                    )
        cython_numpy_probe = interface_probes.get("cython_numpy_pxd")
        if (
            check.get("ok")
            and name in {"cython", "numpy"}
            and isinstance(cython_numpy_probe, dict)
            and not cython_numpy_probe.get("ok")
        ):
            check.update(
                {
                    "ok": False,
                    "reason": "runtime_interface_error",
                    "interface_probe": cython_numpy_probe,
                }
            )
        requirement_checks.append(check)
        if check.get("reason") == "invalid_requirement":
            unsupported.append(requirement)
            continue
        if not check.get("applicable", True):
            continue
        if check.get("reason") == "missing":
            missing.append(requirement)
        elif not check.get("ok"):
            mismatches.append(
                {
                    "requirement": requirement,
                    "expected": str(check.get("specifier") or ""),
                    "installed": str(
                        check.get("imported") or check.get("installed") or ""
                    ),
                    "reason": str(check.get("reason") or ""),
                }
            )
    actual_python = str(snapshot.get("python") or "")
    python_ok = (
        not expected_python
        or actual_python.startswith(expected_python + ".")
        or actual_python == expected_python
    )
    ok = proc.returncode == 0 and python_ok and not missing and not mismatches and not unsupported
    result = {
        "ok": ok,
        "category": "" if ok else "ENV_INCOMPLETE",
        "env_name": env_name,
        "expected_python": expected_python,
        "env_path": env_path,
        "actual_python": actual_python,
        "python_ok": python_ok,
        "expected_requirement_count": len(expected_requirements),
        "missing": missing,
        "version_mismatches": mismatches,
        "unsupported_requirements": unsupported,
        "duplicate_metadata": {
            name: records
            for name, records in duplicate_metadata.items()
            if name in expected_names
        },
        "runtime_probes": runtime_probes,
        "interface_probes": interface_probes,
        "requirement_checks": requirement_checks,
        "returncode": proc.returncode,
        "stderr": proc.stderr[-4000:],
        "cache_hit": False,
    }
    _DEPENDENCY_COMPAT_CACHE[(env_name, expectation_key)] = dict(result)
    return result


def environment_manifest(
    env_name: str,
    timeout: int = 120,
    refresh: bool = False,
) -> dict[str, Any]:
    """Capture a reproducible package snapshot for one Conda environment."""

    if not env_name:
        return {
            "ok": False,
            "category": "COMMAND_RESOLUTION_FAILURE",
            "env_name": env_name,
            "reason": "empty environment name",
        }
    inventory = conda_env_inventory(refresh=refresh)
    if env_name not in inventory:
        return {
            "ok": False,
            "category": "ENV_NOT_FOUND",
            "env_name": env_name,
            "reason": "environment is absent from Conda inventory",
        }
    if refresh:
        _MANIFEST_CACHE.pop(env_name, None)
    cached = _MANIFEST_CACHE.get(env_name)
    if cached is not None:
        return {**cached, "cache_hit": True}
    script = r'''
import json
import platform
import re
import subprocess
import sys

norm = lambda value: re.sub(r"[-_.]+", "-", value).lower()
try:
    from importlib.metadata import distributions
    packages = {
        norm(distribution.metadata.get("Name") or ""): distribution.version
        for distribution in distributions()
        if distribution.metadata.get("Name")
    }
except ImportError:
    import pkg_resources
    packages = {
        norm(distribution.project_name): distribution.version
        for distribution in pkg_resources.working_set
    }
check = subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    # Python 3.6 templates do not support subprocess.run(text=...).
    # Keep the injected manifest probe compatible with every SWT runtime.
    universal_newlines=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
print(json.dumps({
    "python": platform.python_version(),
    "executable": sys.executable,
    "prefix": sys.prefix,
    "packages": dict(sorted(packages.items())),
    "pip_check_returncode": check.returncode,
    "pip_check_stdout": check.stdout[-8000:],
    "pip_check_stderr": check.stderr[-8000:],
}))
'''
    try:
        proc = subprocess.run(
            [
                CONDA_EXE,
                "run",
                "-p",
                inventory[env_name],
                "python",
                "-c",
                script,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "category": "ENV_INCOMPLETE",
            "env_name": env_name,
            "reason": "environment manifest timed out",
            "stderr": str(exc),
        }
    try:
        snapshot = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        snapshot = {}
    fingerprint_payload = {
        "python": snapshot.get("python", ""),
        "packages": snapshot.get("packages", {}),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    result = {
        "ok": proc.returncode == 0 and bool(snapshot),
        "category": "" if proc.returncode == 0 and snapshot else "ENV_INCOMPLETE",
        "env_name": env_name,
        "env_path": inventory.get(env_name, ""),
        "fingerprint": fingerprint,
        "python": snapshot.get("python", ""),
        "executable": snapshot.get("executable", ""),
        "prefix": snapshot.get("prefix", ""),
        "packages": snapshot.get("packages", {}),
        "pip_check_ok": snapshot.get("pip_check_returncode") == 0,
        "pip_check_returncode": snapshot.get("pip_check_returncode"),
        "pip_check_stdout": snapshot.get("pip_check_stdout", ""),
        "pip_check_stderr": snapshot.get("pip_check_stderr", ""),
        "returncode": proc.returncode,
        "stderr": proc.stderr[-4000:],
        "cache_hit": False,
    }
    _MANIFEST_CACHE[env_name] = dict(result)
    return result


def invalidate_environment_runtime_cache(env_name: str) -> None:
    """Forget process-local observations after an env is removed or replaced."""

    _HEALTH_CACHE.pop(env_name, None)
    _MANIFEST_CACHE.pop(env_name, None)
    for key in [key for key in _DEPENDENCY_COMPAT_CACHE if key[0] == env_name]:
        _DEPENDENCY_COMPAT_CACHE.pop(key, None)


def read_environment_state(env_name: str, cache_key: str) -> dict[str, Any]:
    path = environment_state_path(env_name, cache_key)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def has_recoverable_environment_state(env_name: str) -> bool:
    """Return whether any cached lifecycle for this env ended incomplete.

    A corrected setup script has a new fingerprint and therefore a new cache
    key. The old failed lifecycle must still authorize cleanup of the partial
    Conda environment before the corrected script can rebuild it.
    """

    state_dir = environment_identity_path(env_name).parent
    for path in state_dir.glob("*.json"):
        if path.name == "identity.json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and str(value.get("status") or "") in {
            "creating",
            "installing",
            "validating",
            "failed",
        }:
            return True
    return False


def write_environment_state(
    env_name: str,
    cache_key: str,
    state: dict[str, Any],
) -> Path:
    path = environment_state_path(env_name, cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "state_version": ENV_CACHE_STATE_VERSION,
        "env_name": env_name,
        "cache_key": cache_key,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **state,
    }
    temporary = path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def project_import_name(repo: str) -> str:
    project = str(repo or "").split("/")[-1]
    return {
        "scikit-learn": "sklearn",
        "pytest": "pytest",
        "sphinx": "sphinx",
        "matplotlib": "matplotlib",
        "astropy": "astropy",
        "django": "django",
        "sympy": "sympy",
        "seaborn": "seaborn",
        "xarray": "xarray",
        "flask": "flask",
        "requests": "requests",
        "pylint": "pylint",
    }.get(project, project.replace("-", "_"))


def project_health_check(
    env_name: str,
    repo: str,
    repo_dir: str,
    timeout: int = 90,
) -> dict[str, Any]:
    project = str(repo or "").split("/")[-1]
    module = project_import_name(repo)
    modules = _PROJECT_HEALTH_IMPORTS.get(project, [module])
    pythonpath = f"{repo_dir}:{repo_dir}/src:{repo_dir}/lib"
    command = (
        f"{conda_activate_cmd(env_name)} && "
        f"export PYTHONPATH={shlex.quote(pythonpath)}:$PYTHONPATH && "
        "python -c "
        + shlex.quote(
            "import importlib,json,sys; "
            f"names={modules!r}; "
            "loaded=[importlib.import_module(name) for name in names]; "
            "print(json.dumps({'python':sys.version.split()[0],"
            "'module':loaded[-1].__name__,"
            "'module_file':getattr(loaded[-1],'__file__',''),"
            "'imports':[{'module':item.__name__,"
            "'module_file':getattr(item,'__file__',''),"
            "'version':str(getattr(item,'__version__','') or '')}"
            " for item in loaded]}))"
        )
    )
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=repo_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "category": "ENV_INCOMPLETE",
            "timeout": True,
            "module": module,
            "stderr": str(exc),
        }
    return {
        "ok": proc.returncode == 0,
        "category": "" if proc.returncode == 0 else classify_env_error(proc.stdout + "\n" + proc.stderr),
        "timeout": False,
        "module": module,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def conda_activate_cmd(env_name: str) -> str:
    return f'eval "$({shlex.quote(CONDA_EXE)} shell.bash hook)" && conda activate {shlex.quote(env_name)}'


def _parse_conda_env_list_text(stdout: str) -> list[tuple[str, str]]:
    envs: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        if "*" in parts:
            parts = [part for part in parts if part != "*"]
        if len(parts) >= 2 and parts[-1].startswith("/"):
            envs.append((parts[0], parts[-1]))
    return envs


def conda_env_inventory(refresh: bool = False, timeout: int = 30) -> dict[str, str]:
    cache_key = CONDA_EXE
    cached = _INVENTORY_CACHE.get(cache_key)
    if cached and not refresh and time.time() - cached[0] < 30:
        return dict(cached[1])
    env_records: list[tuple[str, str]] = []
    try:
        proc = subprocess.run(
            [CONDA_EXE, "env", "list", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        data = json.loads(proc.stdout or "{}")
        for path in data.get("envs", []):
            name = Path(str(path)).name
            if name:
                # Keep duplicate names until after prefix ownership is known.
                # Conda's global environments.txt can contain both
                # /root/miniconda3/envs/X and /root/brt5-conda/envs/X. A dict
                # at this point lets the stale path overwrite the active one.
                env_records.append((name, str(path)))
    except Exception:
        env_records = []
    if not env_records:
        try:
            proc = subprocess.run(
                [CONDA_EXE, "env", "list"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            env_records = _parse_conda_env_list_text(proc.stdout or "")
        except Exception:
            env_records = []
    # ``conda env list`` includes every prefix recorded in the user's global
    # environments.txt, including environments owned by a different Conda
    # installation.  A name from /root/miniconda3 is not addressable through
    # ``/root/brt5-conda/bin/conda run -n`` even though it appears in that
    # global list.  Keep only prefixes that the active Conda declares in its
    # own envs_dirs (plus its base prefix).
    envs: dict[str, str] = {}
    if env_records:
        try:
            info_proc = subprocess.run(
                [CONDA_EXE, "info", "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            info = json.loads(info_proc.stdout or "{}")
            root_prefix_text = str(info.get("root_prefix") or "").strip()
            root_prefix = Path(root_prefix_text).expanduser()
            allowed_env_dirs = [
                Path(str(path)).expanduser().resolve()
                for path in (info.get("envs_dirs") or [])
                if str(path or "").strip()
            ]
            resolved_root = root_prefix.resolve() if root_prefix_text else None
            if allowed_env_dirs:
                directory_rank = {
                    path: index for index, path in enumerate(allowed_env_dirs)
                }
                active_env_dir = (
                    (resolved_root / "envs").resolve()
                    if resolved_root is not None
                    else None
                )
                ranked: dict[str, tuple[int, str]] = {}
                for name, path in env_records:
                    resolved_path = Path(path).expanduser().resolve()
                    if resolved_root is not None and resolved_path == resolved_root:
                        rank = -2
                    elif (
                        active_env_dir is not None
                        and resolved_path.parent == active_env_dir
                    ):
                        rank = -1
                    elif resolved_path.parent in directory_rank:
                        rank = directory_rank[resolved_path.parent]
                    else:
                        continue
                    previous = ranked.get(name)
                    if previous is None or rank < previous[0]:
                        ranked[name] = (rank, path)
                envs = {name: value[1] for name, value in ranked.items()}
            else:
                envs = dict(env_records)
        except Exception:
            # Older Conda clients may not expose envs_dirs in JSON. Preserve
            # their historical behavior rather than hiding every environment.
            envs = dict(env_records)
    _INVENTORY_CACHE[cache_key] = (time.time(), dict(envs))
    return envs


def env_exists(env_name: str, inventory: dict[str, str] | None = None) -> bool:
    return bool(env_name) and env_name in (inventory if inventory is not None else conda_env_inventory())


def env_health_check(env_name: str, timeout: int = 60, refresh: bool = False) -> dict[str, Any]:
    if not env_name:
        return {"ok": False, "category": "COMMAND_RESOLUTION_FAILURE", "reason": "empty env name"}
    inventory = conda_env_inventory(refresh=refresh)
    env_path = inventory.get(env_name, "")
    if not env_path:
        return {
            "ok": False,
            "category": "ENV_NOT_FOUND",
            "env_name": env_name,
            "env_path": "",
            "reason": "conda environment not found",
        }
    if env_name in _HEALTH_CACHE and not refresh:
        return dict(_HEALTH_CACHE[env_name])
    proc = subprocess.run(
        [
            CONDA_EXE,
            "run",
            "-p",
            env_path,
            "python",
            "-c",
            (
                "import json,sys,sysconfig; "
                "print(json.dumps({'executable': sys.executable, "
                "'version': sys.version.split()[0], "
                "'purelib': sysconfig.get_paths().get('purelib','')}))"
            ),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    ok = proc.returncode == 0
    category = "" if ok else classify_env_error(proc.stdout + "\n" + proc.stderr)
    info: dict[str, Any] = {
        "ok": ok,
        "category": category,
        "env_name": env_name,
        "env_path": env_path,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }
    if ok:
        try:
            parsed = json.loads((proc.stdout or "").strip().splitlines()[-1])
            info.update(parsed)
        except Exception:
            pass
    _HEALTH_CACHE[env_name] = dict(info)
    return info


def classify_env_error(text: str) -> str:
    low = (text or "").lower()
    if "environmentnamenotfound" in low or "could not find conda environment" in low:
        return "ENV_NOT_FOUND"
    if "no space left on device" in low or "disk quota exceeded" in low:
        return "DISK_FULL"
    if "lockerror" in low or "failed to acquire lock" in low or "index.lock" in low:
        return "CONDA_LOCK"
    if "command not found" in low or "conda:" in low and "not found" in low:
        return "COMMAND_RESOLUTION_FAILURE"
    if "modulenotfounderror" in low or "importerror" in low:
        return "ENV_INCOMPLETE"
    if "egg-link" in low and "does not match installed location" in low:
        return "INSTALL_FAILURE"
    if "failed building wheel" in low or "subprocess-exited-with-error" in low:
        return "INSTALL_FAILURE"
    if "setup.py" in low or "pip install" in low:
        return "INSTALL_FAILURE"
    return "ENV_INCOMPLETE"


def extract_env_records(instance_id: str, generated_dir: str) -> list[dict[str, Any]]:
    instance_dir = Path(generated_dir) / instance_id
    records: list[dict[str, Any]] = []
    for filename in ("repo_prepare.json", "summary.json"):
        path = instance_dir / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data.get("template_env_name"), str) and data["template_env_name"]:
            records.append(
                {
                    "env_name": data["template_env_name"],
                    "source": f"{filename}:template_env_name",
                    "environment_role": "dependency_template",
                }
            )
        runtime_environment = data.get("runtime_environment")
        if isinstance(runtime_environment, dict) and isinstance(
            runtime_environment.get("template_env_name"), str
        ) and runtime_environment.get("template_env_name"):
            records.append(
                {
                    "env_name": runtime_environment["template_env_name"],
                    "source": f"{filename}:runtime_environment.template_env_name",
                    "environment_role": "dependency_template",
                }
            )
        if isinstance(data.get("env_name"), str) and data["env_name"]:
            records.append({"env_name": data["env_name"], "source": f"{filename}:env_name"})
        environment = data.get("environment")
        if isinstance(environment, dict) and isinstance(environment.get("env_name"), str):
            records.append(
                {
                    "env_name": environment["env_name"],
                    "source": f"{filename}:environment.env_name",
                    "setup_status": environment.get("status"),
                }
            )
        repo_prepare = data.get("repo_prepare")
        if isinstance(repo_prepare, dict):
            if isinstance(repo_prepare.get("template_env_name"), str) and repo_prepare["template_env_name"]:
                records.append(
                    {
                        "env_name": repo_prepare["template_env_name"],
                        "source": f"{filename}:repo_prepare.template_env_name",
                        "environment_role": "dependency_template",
                    }
                )
            if isinstance(repo_prepare.get("env_name"), str) and repo_prepare["env_name"]:
                records.append({"env_name": repo_prepare["env_name"], "source": f"{filename}:repo_prepare.env_name"})
            nested = repo_prepare.get("environment")
            if isinstance(nested, dict) and isinstance(nested.get("env_name"), str):
                records.append(
                    {
                        "env_name": nested["env_name"],
                        "source": f"{filename}:repo_prepare.environment.env_name",
                        "setup_status": nested.get("status"),
                    }
                )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.get("env_name", ""), record.get("source", ""))
        if key not in seen:
            deduped.append(record)
            seen.add(key)
    return deduped


@dataclass
class EnvResolution:
    requested_env: str
    recorded_env: str
    resolved_env: str
    resolution_source: str
    env_exists: bool
    env_path: str = ""
    env_health: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    legacy_fallback_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_generation_env(issue: dict[str, Any], explicit_env: str = "", run_prefix: str | None = None) -> EnvResolution:
    requested = explicit_env or default_env_name(issue, run_prefix)
    health = env_health_check(requested) if requested else {"ok": False, "category": "COMMAND_RESOLUTION_FAILURE"}
    return EnvResolution(
        requested_env=requested,
        recorded_env=requested,
        resolved_env=requested,
        resolution_source="explicit_cli" if explicit_env else "default_generation_name",
        env_exists=bool(health.get("ok") or health.get("env_path")),
        env_path=str(health.get("env_path") or ""),
        env_health=health,
    )


def resolve_eval_env(
    issue: dict[str, Any],
    generated_dir: str,
    run_prefix: str | None = None,
    allow_legacy: bool = True,
    health_timeout: int = 60,
) -> EnvResolution:
    instance_id = str(issue.get("instance_id") or "")
    requested = default_env_name(issue, run_prefix)
    records = extract_env_records(instance_id, generated_dir)
    inventory = conda_env_inventory()
    if records:
        candidates: list[dict[str, Any]] = []
        for record in records:
            env_name = str(record.get("env_name") or "")
            exists = env_name in inventory
            candidate = dict(record)
            candidate.update({"exists": exists, "env_path": inventory.get(env_name, "")})
            candidates.append(candidate)
            if exists:
                health = env_health_check(env_name, timeout=health_timeout)
                return EnvResolution(
                    requested_env=requested,
                    recorded_env=env_name,
                    resolved_env=env_name,
                    resolution_source=str(record.get("source") or "generation_metadata"),
                    env_exists=True,
                    env_path=inventory.get(env_name, ""),
                    env_health=health,
                    candidates=candidates,
                )
        first = str(records[0].get("env_name") or "")
        return EnvResolution(
            requested_env=requested,
            recorded_env=first,
            resolved_env=first,
            resolution_source="generation_metadata_missing_env",
            env_exists=False,
            env_path="",
            env_health={
                "ok": False,
                "category": "ENV_NOT_FOUND",
                "env_name": first,
                "reason": "generation metadata records an environment that does not exist",
            },
            candidates=candidates,
            errors=["recorded environment does not exist; refusing fallback to another run env"],
        )
    candidates = []
    legacy_names = []
    if requested:
        legacy_names.append((requested, "legacy_default_run_prefix"))
    # Metadata-free runs are legacy by definition.  Only exact historical
    # naming rules are tried; never scan for a similar env from another run.
    unprefixed = legacy_env_name(issue, prefix="")
    if allow_legacy and unprefixed and unprefixed != requested:
        legacy_names.append((unprefixed, "legacy_unprefixed_default"))
    prefixed_legacy = legacy_env_name(issue, prefix=run_prefix)
    if allow_legacy and prefixed_legacy and prefixed_legacy not in {
        name for name, _ in legacy_names
    }:
        legacy_names.append((prefixed_legacy, "legacy_prefixed_default"))
    for env_name, source in legacy_names:
        candidate = {"env_name": env_name, "source": source, "exists": env_name in inventory, "env_path": inventory.get(env_name, "")}
        candidates.append(candidate)
        if candidate["exists"]:
            health = env_health_check(env_name, timeout=health_timeout)
            return EnvResolution(
                requested_env=requested,
                recorded_env="",
                resolved_env=env_name,
                resolution_source=source,
                env_exists=True,
                env_path=inventory.get(env_name, ""),
                env_health=health,
                candidates=candidates,
                warnings=["generation env metadata missing; legacy fallback used"],
                legacy_fallback_used=True,
            )
    fallback = legacy_names[0][0] if legacy_names else requested
    return EnvResolution(
        requested_env=requested,
        recorded_env="",
        resolved_env=fallback,
        resolution_source="legacy_resolution_failed",
        env_exists=False,
        env_health={"ok": False, "category": "ENV_NOT_FOUND", "env_name": fallback},
        candidates=candidates,
        errors=["no generation env metadata and no legacy candidate exists"],
        legacy_fallback_used=True,
    )


def environment_identity_metadata(
    issue: dict[str, Any],
    env_name: str,
    run_id: str = "",
    setup_status: str = "",
    setup_script_fingerprint: str = "",
    source: str = "generation",
) -> dict[str, Any]:
    health = env_health_check(env_name) if env_name else {"ok": False, "category": "COMMAND_RESOLUTION_FAILURE"}
    return {
        "scheme_version": ENV_NAMING_SCHEME_VERSION,
        "env_name": env_name,
        "env_path": health.get("env_path", ""),
        "repo": issue.get("repo", ""),
        "version": issue.get("version", ""),
        "base_commit": issue.get("base_commit", ""),
        "environment_setup_commit": issue.get("environment_setup_commit") or issue.get("base_commit", ""),
        "run_id": run_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "setup_status": setup_status,
        "python_version": health.get("version", ""),
        "env_health": health,
        "setup_script_fingerprint": setup_script_fingerprint,
        "source": source,
    }


def project_cache_ready(
    state: dict[str, Any], repo_dir: str, health: dict[str, Any]
) -> bool:
    """Return whether project setup can be reused in this clean workspace.

    Dependency environments are shared, but an editable project install is
    workspace-specific.  A healthy import alone is insufficient because it may
    resolve through an editable link owned by another instance worktree.
    """

    if state.get("status") != "ready" or not health.get("ok"):
        return False
    prepared_raw = str(state.get("prepared_source_path") or "")
    if state.get("editable_install") is False:
        return True
    if not prepared_raw:
        return False
    try:
        return Path(prepared_raw).resolve() == Path(repo_dir).resolve()
    except OSError:
        return False


def preflight_system(paths: list[str], min_free_gb: float = 2.0, min_free_inodes: int = 10000) -> dict[str, Any]:
    min_free_gb = float(os.environ.get("BRT4_MIN_FREE_GB", min_free_gb))
    min_free_inodes = int(
        os.environ.get("BRT4_MIN_FREE_INODES", min_free_inodes)
    )
    checks: list[dict[str, Any]] = []
    ok = True
    for raw in paths:
        path = Path(raw).expanduser()
        probe = path if path.exists() else path.parent
        try:
            usage = shutil.disk_usage(probe)
            statvfs = os.statvfs(probe)
            free_gb = usage.free / (1024**3)
            free_inodes = statvfs.f_favail
            item = {
                "path": str(path),
                "probe": str(probe),
                "free_gb": round(free_gb, 3),
                "free_inodes": int(free_inodes),
                "ok": free_gb >= min_free_gb and free_inodes >= min_free_inodes,
            }
        except OSError as exc:
            item = {"path": str(path), "ok": False, "error": repr(exc)}
        checks.append(item)
        ok = ok and bool(item.get("ok"))
    raw_conda_probe_timeout = os.environ.get(
        "BRT_CONDA_PROBE_TIMEOUT_SECONDS",
        str(DEFAULT_CONDA_PROBE_TIMEOUT_SECONDS),
    )
    try:
        conda_probe_timeout = float(raw_conda_probe_timeout)
    except (TypeError, ValueError):
        conda_probe_timeout = DEFAULT_CONDA_PROBE_TIMEOUT_SECONDS
    if conda_probe_timeout <= 0:
        conda_probe_timeout = DEFAULT_CONDA_PROBE_TIMEOUT_SECONDS

    conda_ok = Path(CONDA_EXE).exists()
    if conda_ok:
        try:
            proc = subprocess.run(
                [CONDA_EXE, "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=conda_probe_timeout,
                check=False,
            )
            conda_ok = proc.returncode == 0
            conda_version = (proc.stdout or proc.stderr).strip()
        except Exception as exc:
            conda_ok = False
            conda_version = repr(exc)
    else:
        conda_version = "missing"
    ok = ok and conda_ok
    return {
        "ok": ok,
        "minimum_free_gb": min_free_gb,
        "minimum_free_inodes": min_free_inodes,
        "checks": checks,
        "conda_exe": CONDA_EXE,
        "conda_probe_timeout_seconds": conda_probe_timeout,
        "conda_ok": conda_ok,
        "conda_version": conda_version,
    }

"""Offline SWT-Bench compatibility for the pinned local image cache.

This is the self-contained BRT6 counterpart of iCoRe native's cached-image
contract.  It keeps image hashes aligned with the images under
``/root/lby-docker`` and resolves raw-GitHub metadata from local repositories.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def _append_once(spec: dict, key: str, command: str) -> None:
    values = spec.setdefault(key, [])
    if command not in values:
        values.append(command)


_OFFLINE_EXPORT = (
    "export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "
    "HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1"
)


def _vendored_metadata_path(
    root: Path,
    owner: str,
    repository_name: str,
    commit: str,
    relative: str,
) -> Path:
    """Map one raw-GitHub object to the project-local metadata cache."""

    return root / f"{owner}__{repository_name}" / commit / relative


def offline_eval_commands(commands: list[str], *, base_commit: str) -> list[str]:
    """Reuse the official image build and keep optional installs offline.

    ``git clean -fdx`` removes ignored extension modules and generated source
    files baked into old project images.  Keep ignored files while still
    deleting ordinary untracked test files between evaluation states.
    """

    diagnostics = {"git status", "git show", f"git diff {base_commit}"}
    capture_pre_state = re.compile(
        rf"^git diff HEAD {re.escape(base_commit)} >> /root/pre_state\.patch$"
    )
    restore_pre_state = "git apply /root/pre_state.patch"
    checkout_base = f"git checkout {base_commit}"
    clean_checkout = [
        f"git reset --hard {base_commit}",
        "git clean -fd",
    ]
    converted = [_OFFLINE_EXPORT]
    for command in commands:
        if command in diagnostics:
            continue
        if capture_pre_state.fullmatch(command):
            converted.extend(clean_checkout)
            continue
        if command == checkout_base:
            converted.extend(clean_checkout)
            continue
        if command == restore_pre_state:
            continue
        command = re.sub(r"\bgit clean -fdx\b", "git clean -fd", command)
        if "pip install" in command and re.search(
            r"(?:^|\s)-e\s+\.(?:\[[^]]+\])?(?:\s|$)", command
        ):
            continue
        if "pip install" in command:
            command = re.sub(
                r"python\s+-m\s+pip\s+install\b",
                "python -m pip install --no-index --disable-pip-version-check "
                "--no-build-isolation",
                command,
            )
            command = command.replace(
                "--no-build-isolation --no-build-isolation",
                "--no-build-isolation",
            )
        if command != _OFFLINE_EXPORT:
            converted.append(command)
    return converted


def apply_environment_compatibility(version_map: dict) -> None:
    """Apply the environment contracts used by the cached BRT6 images."""

    for version, spec in version_map["pytest-dev/pytest"].items():
        release = version if version.count(".") >= 2 else f"{version}.0"
        install = (
            f"SETUPTOOLS_SCM_PRETEND_VERSION={release} "
            "python -m pip install -e ."
        )
        spec["install"] = install
        _append_once(spec, "eval_commands", install)

    for version in ("1.3", "1.4"):
        spec = version_map["scikit-learn/scikit-learn"][version]
        spec["install"] = spec["install"].replace(" --no-use-pep517", "")

    pylint = version_map["pylint-dev/pylint"]["2.15"]
    _append_once(
        pylint,
        "pre_install",
        "python -m pip install --retries 10 --timeout 120 "
        "'pip<25' 'setuptools<70' wheel",
    )
    pylint["install"] = (
        "python -m pip install --retries 10 --timeout 120 "
        "--no-build-isolation -e ."
    )

    matplotlib_config = (
        "config_template=setup.cfg.template; "
        "if [ -f mplsetup.cfg.template ]; then "
        "config_template=mplsetup.cfg.template; fi; "
        'test -f "$config_template" && cp "$config_template" setup.cfg && '
        "sed -i 's/#system_freetype = False/system_freetype = True/; "
        "s/#system_qhull = False/system_qhull = True/' setup.cfg"
    )
    for version in ("3.3", "3.5", "3.6", "3.7"):
        spec = version_map["matplotlib/matplotlib"][version]
        spec["pre_install"] = [matplotlib_config]
        spec["install"] = (
            "python -m pip install --retries 10 --timeout 120 -v "
            "--no-build-isolation -e ."
        )

    sphinx_setuptools = (
        'python -c "from pathlib import Path; p=Path(\'tox.ini\'); '
        "s=p.read_text(); p.write_text(s if '\\n    setuptools<81\\n' in s "
        "else s.replace('deps =', 'deps =\\n    setuptools<81', 1))\""
    )
    for spec in version_map["sphinx-doc/sphinx"].values():
        _append_once(spec, "pre_install", sphinx_setuptools)
        _append_once(
            spec,
            "eval_commands",
            "python -c 'import roman' 2>/dev/null || "
            "python -m pip install --retries 10 --timeout 120 'roman==3.3'",
        )

    for version in ("4.3", "5.1", "5.2"):
        spec = version_map["astropy/astropy"][version]
        pin_build_setuptools = (
            r'''sed -i 's/requires = \["setuptools",/requires = ["setuptools==68.0.0",/' pyproject.toml'''
        )
        spec["pre_install"] = [pin_build_setuptools]
        spec["install"] = (
            f"{pin_build_setuptools} && "
            "python -m pip install --retries 10 --timeout 120 "
            "-e .[test] --verbose && "
            "python -m pip install --retries 10 --timeout 120 "
            "'setuptools==59.8.0'"
        )


def retryable_repo_commands(
    commands: list[str],
    *,
    repo: str,
    repo_directory: str,
    base_commit: str,
) -> list[str]:
    converted = list(commands)
    clone = (
        "cd / && git config --global http.version HTTP/1.1 && "
        f"rm -rf {repo_directory} && mkdir -p {repo_directory} && "
        f"cd {repo_directory} && git init && "
        f"git remote add origin https://github.com/{repo} && "
        "for attempt in 1 2 3 4 5; do "
        f"timeout --kill-after=15s 300s git fetch --depth=1 --filter=blob:none "
        f"origin {base_commit} && break; "
        'test "$attempt" = 5 && exit 1; sleep $((attempt * 5)); done && '
        f"git checkout --detach {base_commit} && cd /"
    )
    converted[0] = clone
    converted.insert(
        1,
        "export PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 "
        "PIP_DISABLE_PIP_VERSION_CHECK=1",
    )
    converted.insert(2, "echo brt6-environment-compat-v3")
    return converted


def compatible_test_command(
    command: str,
    *,
    repo: str,
    version: str,
    compute_coverage: bool,
    test_directives: tuple[str, ...] | list[str],
) -> str:
    if repo == "sphinx-doc/sphinx" and not compute_coverage:
        directives = " ".join(test_directives)
        command = (
            "python -m pytest --no-header -rA --tb=no "
            f"-p no:cacheprovider {directives}"
        ).strip()
    if repo == "astropy/astropy" and version == "1.3":
        command = re.sub(r"(?<!\S)--no-header(?!\S)\s*", "", command)
    if repo == "sympy/sympy" and compute_coverage:
        for directive in test_directives:
            if directive.endswith(".py"):
                command = re.sub(
                    rf"(?<!\S){re.escape(directive[:-1])}(?!\S)",
                    directive,
                    command,
                )
    return command.replace("--tb=no", "--tb=short")


def install_cached_image_contract(exec_spec_class: type) -> None:
    original_repo_commands = exec_spec_class.repo_script_list.fget
    original_env_image_key = exec_spec_class.env_image_key.fget
    original_test_command = exec_spec_class.test_command.fget
    original_eval_commands = exec_spec_class.eval_script_list.fget

    def repo_script_list(self):
        return retryable_repo_commands(
            original_repo_commands(self),
            repo=self.repo,
            repo_directory=self.repo_directory,
            base_commit=self.base_commit,
        )

    def env_image_key(self):
        key = original_env_image_key(self)
        if self.repo == "matplotlib/matplotlib" and self.version in {
            "3.3",
            "3.5",
            "3.6",
            "3.7",
        }:
            return f"brt6.compat.{key}"
        return key

    def test_command(self):
        return compatible_test_command(
            original_test_command(self),
            repo=self.repo,
            version=self.version,
            compute_coverage=self.compute_coverage,
            test_directives=tuple(self.test_directives or ()),
        )

    def eval_script_list(self):
        return offline_eval_commands(
            original_eval_commands(self),
            base_commit=self.base_commit,
        )

    exec_spec_class.repo_script_list = property(repo_script_list)
    exec_spec_class.env_image_key = property(env_image_key)
    exec_spec_class.test_command = property(test_command)
    exec_spec_class.eval_script_list = property(eval_script_list)


def install_local_source_fetches(repo_root: str | Path) -> None:
    """Resolve official raw-GitHub metadata from local git only."""

    import requests

    root = Path(repo_root).resolve()

    def local_get(url: str, *args, **kwargs):
        del args, kwargs
        parsed = urlparse(url)
        parts = parsed.path.lstrip("/").split("/", 3)
        if parsed.netloc != "raw.githubusercontent.com" or len(parts) != 4:
            raise RuntimeError(f"offline source fetch rejected: {parsed.netloc}")
        owner, repository_name, commit, relative = parts
        relative = posixpath.normpath(relative)
        if relative == ".." or relative.startswith("../"):
            raise RuntimeError("offline source path escapes repository")
        metadata_file = _vendored_metadata_path(
            root, owner, repository_name, commit, relative
        )
        if metadata_file.is_file():
            response = requests.Response()
            response.status_code = 200
            response.url = url
            response._content = metadata_file.read_bytes()
            response.encoding = "utf-8"
            return response
        candidates = (root / repository_name, root / f"{owner}__{repository_name}")
        for repository in candidates:
            if not (repository / ".git").is_dir():
                continue
            result = subprocess.run(
                ["git", "-C", str(repository), "show", f"{commit}:{relative}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode == 0:
                response = requests.Response()
                response.status_code = 200
                response.url = url
                response._content = result.stdout
                response.encoding = "utf-8"
                return response
        response = requests.Response()
        response.status_code = 404
        response.url = url
        response._content = b""
        response.encoding = "utf-8"
        return response

    requests.get = local_get


def configure_cached_official_runtime(
    version_map: dict,
    exec_spec_class: type,
    repo_root: str | Path,
) -> None:
    os.environ["SWT_LOCAL_REPO_ROOT"] = str(Path(repo_root).resolve())
    install_local_source_fetches(repo_root)
    apply_environment_compatibility(version_map)
    install_cached_image_contract(exec_spec_class)

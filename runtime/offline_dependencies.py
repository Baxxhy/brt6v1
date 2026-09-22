"""Provision already-pinned cached-image dependencies without network access."""
from __future__ import annotations

import io
from pathlib import Path
import tarfile


ROMAN_WHEEL = Path(__file__).parent / "wheels" / "roman-3.3-py2.py3-none-any.whl"


def ensure_cached_dependencies(container, *, repo: str, env_name: str = "testbed") -> dict:
    """Honor the existing Sphinx roman==3.3 recipe in offline cached images.

    Do not upgrade installed packages, edit source, or change test commands.
    Failure to provision is infrastructure failure, never a test verdict.
    """
    if repo != "sphinx-doc/sphinx":
        return {"status": "NOT_REQUIRED", "changes": []}
    if not env_name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in env_name):
        raise ValueError("Invalid conda environment name")
    python = f"/opt/miniconda3/envs/{env_name}/bin/python"
    check = container.exec_run([python, "-c", "import roman"])
    if check.exit_code == 0:
        return {"status": "ALREADY_AVAILABLE", "changes": []}
    if not ROMAN_WHEEL.is_file():
        raise RuntimeError(f"Offline dependency wheel missing: {ROMAN_WHEEL}")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        bundle.add(ROMAN_WHEEL, arcname=ROMAN_WHEEL.name)
    if not container.put_archive("/root", archive.getvalue()):
        raise RuntimeError("Could not stage the pinned roman wheel")
    installed = container.exec_run([
        python, "-m", "pip", "install", "--no-index", "--no-deps",
        "--disable-pip-version-check", f"/root/{ROMAN_WHEEL.name}",
    ])
    if installed.exit_code != 0:
        output = installed.output.decode("utf-8", errors="replace")
        raise RuntimeError(f"Offline roman==3.3 installation failed: {output[-2000:]}")
    verified = container.exec_run([python, "-c", "import roman"])
    if verified.exit_code != 0:
        raise RuntimeError("roman import still fails after offline installation")
    return {"status": "REPAIRED", "changes": ["installed roman==3.3 from bundled wheel"]}

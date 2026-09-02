"""Command execution and result classification."""

from __future__ import annotations

import re
import shlex
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from ..core.behavior_evidence import (
    BehaviorEvidence,
    error_symptom_text,
    target_apis,
)
from ..core.schema import ExecutionResult


def no_tests_executed(stdout: str, stderr: str = "") -> bool:
    """Return true when a nominally successful runner executed no test body."""

    low = f"{stdout}\n{stderr}".lower()
    if any(
        marker in low
        for marker in (
            "no tests ran",
            "collected 0 items",
            "no tests collected",
            "found 0 test.",
            "found 0 tests.",
        )
    ):
        return True
    if re.search(r"\bran\s+0\s+tests?\b", low):
        return True
    # SymPy's custom runner returns rc=0 for an unmatched path and prints this
    # exact terminal summary. Do not confuse it with a real failing run such
    # as "0 passed, 1 failed".
    if re.search(
        r"tests finished:\s*0 passed,\s*in\s+[0-9.]+\s+seconds",
        low,
    ):
        return True
    # pytest, tox-wrapped pytest, and SymPy all report an explicit passed count
    # when at least one test body ran successfully.  A zero-success run whose
    # only terminal outcome is skip must not qualify as PASS/F2P.
    if re.search(r"\b[1-9]\d*\s+passed\b", low):
        return False
    if re.search(r"\b[1-9]\d*\s+skipped\b", low):
        return True
    unittest_run = re.search(r"ran\s+(\d+)\s+tests?\b", low)
    unittest_skipped = re.search(r"ok\s*\(skipped=(\d+)\)", low)
    return bool(
        unittest_run
        and unittest_skipped
        and int(unittest_run.group(1)) == int(unittest_skipped.group(1))
    )


def run_subprocess_tree(
    command: str | list[str],
    cwd: str,
    timeout: int,
    *,
    shell: bool = False,
    executable: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run in a process group and terminate the full runner tree on timeout."""
    proc = subprocess.Popen(
        command,
        shell=shell,
        executable=executable,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        exc.stdout = stdout
        exc.stderr = stderr
        raise exc
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _pythonpath_export(cwd: str, extra_entries: list[str] | None = None) -> str:
    root = Path(cwd)
    entries = list(extra_entries or []) + [str(root)]
    for relative in ("src", "lib"):
        path = root / relative
        if path.is_dir():
            entries.append(str(path))
    joined = ":".join(shlex.quote(entry) for entry in entries)
    return f"export PYTHONPATH={joined}:${{PYTHONPATH:-}}"


def _conda_init_script() -> str:
    candidates: list[Path] = []
    configured_script = os.environ.get("BRT3_CONDA_SH")
    if configured_script:
        candidates.append(Path(configured_script).expanduser())
    conda_exe = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if conda_exe:
        candidates.append(Path(conda_exe).resolve().parent.parent / "etc/profile.d/conda.sh")
    candidates.extend(
        [
            Path.home() / "miniforge3/etc/profile.d/conda.sh",
            Path.home() / "miniconda3/etc/profile.d/conda.sh",
            # Compatibility fallback for the original experiment machine.
            Path("/root/conda/ENTER/etc/profile.d/conda.sh"),
        ]
    )
    for path in candidates:
        if path.is_file():
            return str(path)
    return ""


def classify_execution(returncode: int, stdout: str, stderr: str, timeout: bool, behavior: BehaviorEvidence | None = None) -> str:
    text = f"{stdout}\n{stderr}"
    low = text.lower()
    if timeout:
        return "TIMEOUT"
    if returncode == 0 and no_tests_executed(stdout, stderr):
        return "COLLECT_ERROR"
    if returncode == 0:
        return "PASS"
    if "syntaxerror" in low or "indentationerror" in low:
        return "SYNTAX_ERROR"
    dependency_setup_markers = [
        "module 'numpy' has no attribute 'int'",
        "module 'numpy' has no attribute 'float'",
        "module 'numpy' has no attribute 'complex'",
        "failed to import the compiled extension",
        "cannot import name",
    ]
    issue_terms: list[str] = []
    symptom = ""
    if behavior:
        for obj in target_apis(behavior):
            name = str(obj.get("name") or "")
            if name:
                issue_terms.append(name.split(".")[-1])
        symptom = error_symptom_text(behavior)
        issue_terms += [
            x for x in re.split(r"[^A-Za-z0-9_]+", symptom) if len(x) >= 4
        ]
    symptom_low = symptom.lower()
    nested_collection_observed = any(
        marker in low for marker in ("error collecting", "collected 0 items")
    )
    outer_test_executed = bool(
        re.search(r"collected\s+[1-9]\d*\s+items?", low)
        and re.search(r"(?m)^failed\s+\S+::\S+", low)
    )
    outer_assertion_failed = any(
        marker in low
        for marker in ("assertionerror", "failed: nomatch", "remains unmatched")
    )
    if nested_collection_observed and outer_test_executed and outer_assertion_failed:
        if issue_terms and any(term.lower() in low for term in issue_terms[:20]):
            return "ISSUE_ALIGNED_FAIL"
        return "ASSERTION_FAIL"
    if "nameerror:" in low and "nameerror" not in symptom_low:
        return "SETUP_ERROR"
    if (
        re.search(
            r"attributeerror:\s+['\"](?:test|test_)[^'\"]*['\"]"
            r"\s+object has no attribute",
            low,
        )
        and "attributeerror" not in symptom_low
    ):
        return "SETUP_ERROR"
    django_harness_markers = [
        "doesn't declare an explicit app_label",
        "isn't in an application in installed_apps",
        "apps aren't loaded yet",
        "appregistrynotready",
    ]
    if any(marker in low for marker in django_harness_markers):
        return "SETUP_ERROR"
    if (
        "django/core/management/__init__.py" in low
        and "fetch_command" in low
        and ("keyerror:" in low or "unknown command:" in low)
    ):
        return "SETUP_ERROR"
    if "noreversematch" in low and "noreversematch" not in symptom_low:
        return "SETUP_ERROR"
    if (
        ("sqlite3.operationalerror" in low or "django.db.utils.operationalerror" in low)
        and 'near "[]": syntax error' in low
        and not any(
            marker in symptom_low
            for marker in ("sqlite", "syntax error", "operationalerror")
        )
    ):
        return "SETUP_ERROR"
    if (
        "systemcheckerror" in low
        and "system check identified" in low
        and not any(
            marker in symptom_low
            for marker in ("system check", "systemcheckerror", "fields.e", "models.e")
        )
    ):
        return "SETUP_ERROR"
    fixture_resolution_error = bool(
        re.search(
            r"\bfixture\s+(?:['\"][^'\"]+['\"]|[A-Za-z_][A-Za-z0-9_]*)"
            r"\s+not\s+found\b",
            low,
        )
    )
    setup_markers = [
        "importerror",
        "modulenotfounderror",
        "settings are not configured",
        "improperlyconfigured",
    ]
    if any(s in low for s in dependency_setup_markers):
        return "SETUP_ERROR"
    if fixture_resolution_error or any(s in low for s in setup_markers):
        issue_describes_import_failure = any(
            marker in symptom_low
            for marker in ("importerror", "module not found", "cannot import")
        )
        issue_describes_fixture_failure = (
            fixture_resolution_error
            and "fixture" in symptom_low
            and "not found" in symptom_low
        )
        if (
            issue_describes_import_failure or issue_describes_fixture_failure
        ) and any(
            term.lower() in low for term in issue_terms[:20]
        ):
            return "ISSUE_ALIGNED_FAIL"
        return "SETUP_ERROR"
    if any(
        s in low
        for s in [
            "collection error",
            "error collecting",
            "collected 0 items",
            "no tests ran",
            "not found:",
        ]
    ):
        return "COLLECT_ERROR"
    if issue_terms and any(term.lower() in low for term in issue_terms[:20]):
        return "ISSUE_ALIGNED_FAIL"
    if "assertionerror" in low or re.search(r"\bassert\b", low):
        return "ASSERTION_FAIL"
    return "UNRELATED_FAIL"


def run_command_in_conda(
    command: str,
    cwd: str,
    conda_env: str = "",
    timeout: int = 120,
    no_conda: bool = False,
    behavior: BehaviorEvidence | None = None,
    instance_id: str = "",
) -> ExecutionResult:
    # Keep the historical function name as the single execution seam used by
    # generation.  Under the paper-facing contract an instance-scoped official
    # Docker runtime is registered by pipeline.run, so no host Conda command is
    # reached.  The local branch remains only for old unit-level callers.
    if instance_id:
        from ..runtime.official_docker_runtime import active_runtime

        runtime = active_runtime(instance_id)
        if runtime is not None:
            return runtime.execute(command, cwd, timeout, behavior)
    if os.environ.get("BRT_REQUIRE_OFFICIAL_DOCKER") == "1":
        raise RuntimeError(
            "official Docker execution is required, but no instance runtime "
            f"is active for {instance_id or '<missing instance_id>'}"
        )
    started = time.time()
    pythonpath = _pythonpath_export(cwd)
    base_command = f"{pythonpath} && {command}"
    if no_conda or not conda_env:
        activated = base_command
    else:
        conda_init = _conda_init_script()
        if conda_init:
            activated = (
                f"source {shlex.quote(conda_init)} && "
                f"conda activate {shlex.quote(conda_env)} && {base_command}"
            )
        else:
            activated = (
                f"conda run -n {shlex.quote(conda_env)} bash -lc "
                f"{shlex.quote(base_command)}"
            )
    shell_cmd = f"bash -lc {shlex.quote(activated)}"
    timed_out = False
    try:
        proc = run_subprocess_tree(
            shell_cmd,
            cwd,
            timeout,
            shell=True,
            executable="/bin/bash",
        )
        stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        returncode = 124
    status = classify_execution(returncode, stdout, stderr, timed_out, behavior)
    return ExecutionResult(
        instance_id=instance_id,
        command=shell_cmd,
        cwd=cwd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration=time.time() - started,
        timeout=timed_out,
        status=status,
        error_reason=(
            "command timed out"
            if timed_out
            else "" if returncode == 0 else (stdout + "\n" + stderr)[-4000:]
        ),
    )

#!/usr/bin/env python3
"""Create the canonical SWT dependency-template environments with bounded parallelism.

This intentionally reuses the same iCoRe execution specifications and
``ensure_icore_environment`` implementation as generation.  It does not
install benchmark projects into the templates; generation creates an isolated
clone and installs the corresponding checkout there.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from brt6.retrieval.icore_runtime import (  # noqa: E402
    ensure_icore_environment,
    env_name_for,
    make_instance_spec,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prebuild SWT dependency-template Conda environments concurrently."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "data/issues/swt276_issues.json",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=PROJECT_ROOT / ".bootstrap/swt-template-environments",
    )
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent template preparations (default: 4, maximum: 8).",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Print the canonical environments without creating them.",
    )
    return parser.parse_args()


def load_unique_templates(dataset_path: Path) -> list[dict[str, str]]:
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"dataset must contain a JSON list: {dataset_path}")

    templates: dict[str, dict[str, str]] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        repo = str(raw.get("repo") or "")
        version = str(raw.get("version") or "")
        base_commit = str(raw.get("base_commit") or "")
        environment_setup_commit = str(
            raw.get("environment_setup_commit") or base_commit
        )
        instance_id = str(raw.get("instance_id") or "")
        if not all((repo, version, base_commit, environment_setup_commit, instance_id)):
            raise ValueError(f"incomplete environment metadata: {raw!r}")
        env_name = env_name_for(
            repo,
            version,
            base_commit,
            environment_setup_commit,
        )
        templates.setdefault(
            env_name,
            {
                "env_name": env_name,
                "instance_id": instance_id,
                "repo": repo,
                "version": version,
                "base_commit": base_commit,
                "environment_setup_commit": environment_setup_commit,
            },
        )
    return sorted(
        templates.values(),
        key=lambda item: (item["repo"], item["version"], item["env_name"]),
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def failure_diagnostic(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize one environment failure without hiding its raw evidence."""

    try:
        returncode = int(result.get("returncode", 1))
    except (TypeError, ValueError):
        returncode = 1
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    traceback_text = str(result.get("traceback") or "")
    combined = "\n".join((stdout, stderr, traceback_text))
    lowered = combined.lower()
    health = result.get("health") if isinstance(result.get("health"), dict) else {}
    compatibility = (
        result.get("compatibility")
        if isinstance(result.get("compatibility"), dict)
        else {}
    )
    category = str(health.get("category") or compatibility.get("category") or "")
    if not category:
        if re.search(r"line \d+:\s*\d+: no such file or directory", lowered):
            category = "SHELL_REDIRECTION"
        elif (
            "retry.__init__() got an unexpected keyword argument" in lowered
            or "respect_retry_after_header" in lowered
        ):
            category = "CONDA_RUNTIME_DEPENDENCY_CONFLICT"
        elif "no space left on device" in lowered or "disk quota exceeded" in lowered:
            category = "DISK_FULL"
        elif "numpy.dtype size changed" in lowered:
            category = "BINARY_ABI_MISMATCH"
        elif "proxyerror" in lowered or "proxy configuration" in lowered:
            category = "PROXY_CONFIGURATION"
        elif "packagesnotfounderror" in lowered or "resolvepackagenotfound" in lowered:
            category = "CONDA_SOLVER"
        elif (
            "environmentlocationnotfound" in lowered
            or "could not find conda environment" in lowered
        ):
            category = "ENV_NOT_FOUND"
        elif any(
            marker in lowered
            for marker in (
                "temporary failure in name resolution",
                "connectionerror",
                "read timed out",
                "sslerror",
                "could not fetch url",
            )
        ):
            category = "NETWORK"
        elif "requires-python" in lowered or "no matching distribution found" in lowered:
            category = "PYTHON_COMPATIBILITY"
        elif (
            "subprocess-exited-with-error" in lowered
            or "failed building wheel" in lowered
            or "metadata-generation-failed" in lowered
        ):
            category = "PIP_BUILD"
        elif "modulenotfounderror" in lowered or "importerror" in lowered:
            category = "IMPORT_FAILURE"
        else:
            category = str(result.get("status") or "UNKNOWN")

    error_line = str(health.get("reason") or compatibility.get("reason") or "")
    if not error_line:
        for stream in (stderr, stdout, traceback_text):
            for line in reversed(stream.splitlines()):
                stripped = line.strip()
                if stripped and not stripped.startswith("+"):
                    error_line = stripped
                    break
            if error_line:
                break

    return {
        "status": str(result.get("status") or ""),
        "returncode": returncode,
        "category": category,
        "error_line": error_line,
        "error": str(result.get("error") or ""),
        "resolution_source": str(result.get("resolution_source") or ""),
        "script_path": str(result.get("script_path") or ""),
        "requirements_path": str(result.get("requirements_path") or ""),
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
        "traceback_tail": traceback_text[-4000:],
        "health": health,
        "compatibility": compatibility,
        "dependency_repair": (
            result.get("dependency_repair")
            if isinstance(result.get("dependency_repair"), dict)
            else {}
        ),
    }


def attempt_diagnostics(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Retain retry history and select the first substantive root cause."""

    history: list[dict[str, Any]] = []
    for attempt in attempts:
        result = attempt.get("result") if isinstance(attempt, dict) else {}
        result = result if isinstance(result, dict) else {}
        history.append(
            {
                "attempt": int(attempt.get("attempt") or len(history) + 1),
                "elapsed_seconds": attempt.get("elapsed_seconds"),
                **failure_diagnostic(result),
            }
        )
    root_cause = next(
        (
            item
            for item in history
            if item["category"] not in {"ENV_NOT_FOUND", "ENV_INCOMPLETE"}
        ),
        history[0] if history else {},
    )
    return {"root_cause": root_cause, "attempt_history": history}


def prepare_template(
    template: dict[str, str],
    work_root: Path,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    env_name = template["env_name"]
    workdir = work_root / env_name
    workdir.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []

    for attempt_number in range(1, retries + 1):
        started = time.time()
        try:
            spec = make_instance_spec(
                template["instance_id"],
                template["repo"],
                template["version"],
                template["base_commit"],
                template["environment_setup_commit"],
            )
            result = ensure_icore_environment(
                spec,
                env_name,
                str(workdir),
                timeout,
            )
        except Exception as exc:  # Preserve the complete failure for resume/audit.
            result = {
                "status": "EXCEPTION",
                "returncode": 1,
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }
        attempt = {
            "attempt": attempt_number,
            "elapsed_seconds": round(time.time() - started, 3),
            "result": result,
        }
        attempts.append(attempt)
        write_json(workdir / f"attempt_{attempt_number}.json", attempt)
        if int(result.get("returncode", 1)) == 0:
            return {
                **template,
                "status": "READY",
                "attempts": attempts,
                "result": result,
            }
        if attempt_number < retries:
            time.sleep(min(5 * attempt_number, 15))

    return {
        **template,
        "status": "FAILED",
        "attempts": attempts,
        "result": attempts[-1]["result"] if attempts else {},
    }


def prepare_templates(
    templates: list[dict[str, str]],
    work_root: Path,
    timeout: int,
    retries: int,
    workers: int,
) -> list[dict[str, Any]]:
    """Prepare independent templates concurrently and persist ordered progress."""

    results: list[dict[str, Any] | None] = [None] * len(templates)
    future_metadata: dict[Future[dict[str, Any]], tuple[int, dict[str, str]]] = {}
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="brt5-swt-env",
    ) as executor:
        for index, template in enumerate(templates):
            print(
                f"[{index + 1:02d}/{len(templates):02d}] "
                f"QUEUED {template['env_name']}",
                flush=True,
            )
            future = executor.submit(
                prepare_template,
                template,
                work_root,
                timeout,
                retries,
            )
            future_metadata[future] = (index, template)

        completed = 0
        for future in as_completed(future_metadata):
            index, template = future_metadata[future]
            try:
                result = future.result()
            except Exception as exc:  # Preserve failures outside prepare_template.
                result = {
                    **template,
                    "status": "FAILED",
                    "attempts": [],
                    "result": {
                        "status": "EXCEPTION",
                        "returncode": 1,
                        "error": repr(exc),
                        "traceback": traceback.format_exc(),
                    },
                }
            results[index] = result
            completed += 1
            ordered_completed = [item for item in results if item is not None]
            write_json(work_root / "prewarm_results.json", ordered_completed)
            print(
                f"[{completed:02d}/{len(templates):02d}] {result['status']} "
                f"{template['env_name']}",
                flush=True,
            )
            if result["status"] != "READY":
                diagnostic_bundle = attempt_diagnostics(result.get("attempts") or [])
                diagnostic = diagnostic_bundle["root_cause"]
                print(
                    "  failure: "
                    f"status={diagnostic.get('status', '')} "
                    f"rc={diagnostic.get('returncode', 1)} "
                    f"category={diagnostic.get('category', 'UNKNOWN')}",
                    flush=True,
                )
                if diagnostic.get("error_line"):
                    print(f"  error: {diagnostic['error_line']}", flush=True)

    return [item for item in results if item is not None]


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.retries <= 0:
        raise ValueError("--timeout and --retries must be positive")
    if not 1 <= args.workers <= 8:
        raise ValueError("--workers must be between 1 and 8")
    dataset = args.dataset.expanduser().resolve()
    work_root = args.work_root.expanduser().resolve()
    templates = load_unique_templates(dataset)

    print(f"dataset={dataset}", flush=True)
    print(f"unique_template_environments={len(templates)}", flush=True)
    print(f"parallel_workers={args.workers}", flush=True)
    if args.list_only:
        for index, template in enumerate(templates, 1):
            print(
                f"[{index:02d}/{len(templates):02d}] "
                f"{template['env_name']} ({template['repo']} {template['version']})"
            )
        return 0

    work_root.mkdir(parents=True, exist_ok=True)
    results = prepare_templates(
        templates,
        work_root,
        args.timeout,
        args.retries,
        args.workers,
    )

    ready = [item["env_name"] for item in results if item["status"] == "READY"]
    failed = [item["env_name"] for item in results if item["status"] != "READY"]
    failure_details = [
        {
            "env_name": item["env_name"],
            "instance_id": item["instance_id"],
            "repo": item["repo"],
            "version": item["version"],
            **attempt_diagnostics(item.get("attempts") or []),
        }
        for item in results
        if item["status"] != "READY"
    ]
    summary = {
        "dataset": str(dataset),
        "work_root": str(work_root),
        "workers": args.workers,
        "total": len(results),
        "ready_count": len(ready),
        "failed_count": len(failed),
        "ready": ready,
        "failed": failed,
        "failure_details": failure_details,
    }
    write_json(work_root / "summary.json", summary)
    write_json(work_root / "failure_diagnostics.json", failure_details)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

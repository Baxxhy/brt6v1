#!/usr/bin/env python3
"""Read-only gate for running TDD-Bench through BRT's local Conda backend."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from brt6.retrieval.icore_exec_spec import make_exec_spec  # noqa: E402
from brt6.retrieval.icore_runtime import (  # noqa: E402
    ensure_isolated_runtime_environment,
    remove_isolated_runtime_environment,
)
from brt6.runtime.conda_env_manager import (  # noqa: E402
    CONDA_EXE,
    conda_env_inventory,
    default_env_name,
    dependency_env_compatibility,
    dependency_install_spec,
    dependency_seed_candidates,
    environment_lock_path,
    environment_operation_lock,
    read_environment_identity,
)


RUNTIME_BACKEND = "local_conda"


def load_rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
    elif isinstance(data, dict):
        rows = [
            dict(value, instance_id=value.get("instance_id", key))
            for key, value in data.items()
            if isinstance(value, dict)
        ]
    else:
        raise ValueError(f"unsupported dataset structure: {type(data).__name__}")
    instance_ids = [str(row.get("instance_id") or "") for row in rows]
    if not rows or any(not instance_id for instance_id in instance_ids):
        raise ValueError("dataset contains an empty or invalid instance_id")
    if len(instance_ids) != len(set(instance_ids)):
        raise ValueError("dataset contains duplicate instance_id values")
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def repo_path(repo_root_base: Path, repo: str) -> Path:
    return repo_root_base / repo.split("/")[-1]


def audit_repository_commits(
    rows: list[dict[str, Any]], repo_root_base: Path
) -> list[dict[str, Any]]:
    by_repo: dict[str, set[str]] = {}
    for row in rows:
        repo = str(row.get("repo") or "")
        commits = by_repo.setdefault(repo, set())
        for key in ("base_commit", "environment_setup_commit"):
            commit = str(row.get(key) or "")
            if commit:
                commits.add(commit)
    records = []
    for repo, commits in sorted(by_repo.items()):
        path = repo_path(repo_root_base, repo)
        record: dict[str, Any] = {
            "repo": repo,
            "path": str(path),
            "exists": path.is_dir(),
            "git_repository": (path / ".git").exists(),
            "requested_commits": len(commits),
            "missing_commits": [],
            "index_lock_present": (path / ".git" / "index.lock").exists(),
        }
        if record["exists"] and record["git_repository"]:
            ordered = sorted(commits)
            proc = subprocess.run(
                ["git", "-C", str(path), "cat-file", "--batch-check"],
                input="".join(f"{commit}\n" for commit in ordered),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
                check=False,
            )
            output = (proc.stdout or "").splitlines()
            missing = [
                commit
                for commit, line in zip(ordered, output)
                if line.endswith(" missing")
            ]
            if len(output) != len(ordered):
                missing.extend(ordered[len(output) :])
            record.update(
                {
                    "git_returncode": proc.returncode,
                    "git_stderr": proc.stderr[-4000:],
                    "missing_commits": sorted(set(missing)),
                }
            )
        record["ready"] = bool(
            record["exists"]
            and record["git_repository"]
            and not record["index_lock_present"]
            and not record["missing_commits"]
        )
        records.append(record)
    return records


def inspect_local_bindings(env_name: str) -> dict[str, Any]:
    script = r'''
import json
import os
import pathlib
import sysconfig

site = pathlib.Path(sysconfig.get_paths()["purelib"])
workspace_markers = tuple(
    marker
    for marker in (
        os.environ.get("BRT_WORKSPACE_ROOT", "").rstrip("/"),
        "eval_clones",
        "/worktree",
    )
    if marker
)
local_direct_urls = []
for path in site.glob("*.dist-info/direct_url.json"):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    url = str(data.get("url") or "")
    editable = bool((data.get("dir_info") or {}).get("editable"))
    workspace_url = any(marker in url for marker in workspace_markers)
    if editable or workspace_url:
        local_direct_urls.append({
            "path": str(path),
            "url": url,
            "editable": editable,
        })

external_binding_files = []
for path in list(site.glob("*.pth")) + list(site.glob("*.egg-link")):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    if any(marker in text for marker in workspace_markers):
        external_binding_files.append(str(path))

print(json.dumps({
    "site_packages": str(site),
    "local_direct_urls": local_direct_urls,
    "external_binding_files": external_binding_files,
}))
'''
    proc = subprocess.run(
        [CONDA_EXE, "run", "-n", env_name, "python", "-c", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    try:
        details = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        details = {}
    clean = bool(
        proc.returncode == 0
        and details
        and not str(proc.stderr or "").strip()
        and not details.get("local_direct_urls")
        and not details.get("external_binding_files")
    )
    return {
        "clean": clean,
        "returncode": proc.returncode,
        "stderr": proc.stderr[-4000:],
        "details": details,
    }


def audit_environment_groups(
    rows: list[dict[str, Any]], limit_groups: int = 0
) -> tuple[list[dict[str, Any]], int]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(default_env_name(row, prefix=""), []).append(row)
    all_group_count = len(groups)
    selected_groups = sorted(groups.items())
    if limit_groups:
        selected_groups = selected_groups[:limit_groups]
    inventory = conda_env_inventory(refresh=True)
    records = []
    for index, (canonical_env, group_rows) in enumerate(selected_groups, start=1):
        row = group_rows[0]
        record: dict[str, Any] = {
            "canonical_env": canonical_env,
            "repo": row.get("repo"),
            "version": row.get("version"),
            "environment_setup_commit": row.get("environment_setup_commit"),
            "instance_count": len(group_rows),
            "instance_ids": [item["instance_id"] for item in group_rows],
            "prepare_lock": str(environment_lock_path(canonical_env, "prepare")),
            "runtime_lock": str(environment_lock_path(canonical_env, "runtime")),
            "candidates": [],
            "ready": False,
        }
        try:
            spec = make_exec_spec(row)
            dependency_spec = dependency_install_spec(spec.install, spec.env_script)
            candidates = [canonical_env]
            candidates.extend(
                dependency_seed_candidates(row, inventory, canonical_env)
            )
            for candidate in dict.fromkeys(candidates):
                if candidate not in inventory:
                    continue
                compatibility = dependency_env_compatibility(
                    candidate,
                    dependency_spec,
                    timeout=300,
                    refresh=True,
                )
                candidate_record: dict[str, Any] = {
                    "env_name": candidate,
                    "identity": read_environment_identity(candidate),
                    "compatibility": compatibility,
                }
                if compatibility.get("ok"):
                    bindings = inspect_local_bindings(candidate)
                    candidate_record["bindings"] = bindings
                    clean_startup = not str(
                        compatibility.get("stderr") or bindings.get("stderr") or ""
                    ).strip()
                    candidate_record["clean_startup"] = clean_startup
                    binding_inspection_ok = bool(
                        bindings.get("returncode") == 0
                        and (bindings.get("details") or {}).get("site_packages")
                    )
                    candidate_record[
                        "template_binding_clean"
                    ] = bool(bindings.get("clean"))
                    if binding_inspection_ok and clean_startup:
                        record["selected_env"] = candidate
                        record["ready"] = True
                        record["template_binding_clean"] = bool(
                            bindings.get("clean")
                        )
                        record["isolated_clone_lock_pattern"] = str(
                            environment_lock_path(
                                f"brt5i_tdd_{canonical_env}", "isolated_clone"
                            )
                        )
                        record["candidates"].append(candidate_record)
                        break
                record["candidates"].append(candidate_record)
        except Exception as exc:  # noqa: BLE001
            record["error"] = repr(exc)
        records.append(record)
        if index == 1 or index % 10 == 0 or index == len(selected_groups):
            print(
                json.dumps(
                    {
                        "__BRT_TDD_PREFLIGHT__": True,
                        "checked_groups": index,
                        "scheduled_groups": len(selected_groups),
                        "ready_groups": sum(bool(item.get("ready")) for item in records),
                    }
                ),
                flush=True,
            )
    return records, all_group_count


def verify_runtime_isolation_smoke(
    rows: list[dict[str, Any]],
    environments: list[dict[str, Any]],
    workdir: Path,
) -> dict[str, Any]:
    candidates = [
        record
        for record in environments
        if record.get("ready") and not record.get("template_binding_clean", True)
    ]
    if not candidates:
        candidates = [record for record in environments if record.get("ready")]
    if not candidates:
        return {"ok": False, "error": "no ready template available for clone smoke"}
    selected = candidates[0]
    by_id = {str(row["instance_id"]): row for row in rows}
    row = by_id[str(selected["instance_ids"][0])]
    spec = make_exec_spec(row)
    dependency_spec = dependency_install_spec(spec.install, spec.env_script)
    workdir.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("BRT_TDD_STRICT_LOCAL_CONDA")
    os.environ["BRT_TDD_STRICT_LOCAL_CONDA"] = "1"
    runtime: dict[str, Any] = {}
    cleanup: dict[str, Any] = {}
    try:
        runtime = ensure_isolated_runtime_environment(
            str(selected["selected_env"]),
            str(row["instance_id"]),
            str(workdir),
            1800,
            "tdd_preflight",
            spec.install,
            project_distribution=str(row.get("repo") or "").split("/")[-1],
        )
        runtime_env = str(runtime.get("env_name") or "")
        bindings = (
            inspect_local_bindings(runtime_env)
            if runtime.get("returncode") == 0 and runtime_env
            else {}
        )
        compatibility = (
            dependency_env_compatibility(
                runtime_env,
                dependency_spec,
                timeout=300,
                refresh=True,
            )
            if runtime.get("returncode") == 0 and runtime_env
            else {}
        )
        if runtime_env:
            cleanup = remove_isolated_runtime_environment(
                runtime_env, str(workdir), 1800
            )
        ok = bool(
            runtime.get("returncode") == 0
            and bindings.get("clean")
            and compatibility.get("ok")
            and cleanup.get("returncode") == 0
        )
        return {
            "ok": ok,
            "instance_id": row["instance_id"],
            "template_env": selected["selected_env"],
            "template_binding_clean": selected.get("template_binding_clean"),
            "runtime_environment": runtime,
            "runtime_bindings": bindings,
            "runtime_compatibility": compatibility,
            "cleanup": cleanup,
        }
    finally:
        if previous is None:
            os.environ.pop("BRT_TDD_STRICT_LOCAL_CONDA", None)
        else:
            os.environ["BRT_TDD_STRICT_LOCAL_CONDA"] = previous


def run_preflight(
    instances_path: Path,
    repo_root_base: Path,
    output_path: Path,
    limit_groups: int = 0,
) -> dict[str, Any]:
    started = time.time()
    rows = load_rows(instances_path)
    missing_patch = [
        str(row["instance_id"]) for row in rows if not str(row.get("patch") or "").strip()
    ]
    missing_setup_commit = [
        str(row["instance_id"])
        for row in rows
        if not str(row.get("environment_setup_commit") or "").strip()
    ]
    framework_env = Path(sys.executable).resolve().parent.parent.name
    framework_ok = framework_env.lower() == "icore"
    conda_ok = Path(CONDA_EXE).is_file()
    lock_probe = ""
    lock_ok = False
    try:
        with environment_operation_lock("__tdd_local_conda_preflight__", "probe") as path:
            lock_probe = str(path)
            lock_ok = path.is_file()
    except OSError:
        lock_ok = False
    repositories = audit_repository_commits(rows, repo_root_base)
    environments, all_group_count = audit_environment_groups(rows, limit_groups)
    ready_groups = sum(bool(record.get("ready")) for record in environments)
    audited_instances = sum(int(record["instance_count"]) for record in environments)
    ready_instances = sum(
        int(record["instance_count"])
        for record in environments
        if record.get("ready")
    )
    scheduled_groups = len(environments)
    isolation_smoke = verify_runtime_isolation_smoke(
        rows,
        environments,
        output_path.parent / ".tdd_local_conda_isolation_smoke",
    )
    contaminated_groups = sum(
        bool(record.get("ready") and not record.get("template_binding_clean", True))
        for record in environments
    )
    ok = bool(
        framework_ok
        and conda_ok
        and lock_ok
        and not missing_patch
        and not missing_setup_commit
        and all(record.get("ready") for record in repositories)
        and ready_groups == scheduled_groups
        and ready_instances == audited_instances
        and isolation_smoke.get("ok")
    )
    payload = {
        "status": "READY" if ok else "NOT_READY",
        "runtime_backend": RUNTIME_BACKEND,
        "docker_required": False,
        "docker_harness_invoked": False,
        "instances_path": str(instances_path.resolve()),
        "repo_root_base": str(repo_root_base.resolve()),
        "dataset_instances": len(rows),
        "environment_groups": all_group_count,
        "audited_groups": scheduled_groups,
        "ready_groups": ready_groups,
        "audited_instances": audited_instances,
        "ready_instances": ready_instances,
        "template_contaminated_groups": contaminated_groups,
        "runtime_isolation_smoke": isolation_smoke,
        "full_dataset_audit": not limit_groups,
        "framework_python": sys.executable,
        "framework_environment": framework_env,
        "framework_is_icore": framework_ok,
        "conda_executable": CONDA_EXE,
        "conda_available": conda_ok,
        "lock_probe": lock_probe,
        "lock_writable": lock_ok,
        "missing_patch": missing_patch,
        "missing_environment_setup_commit": missing_setup_commit,
        "repositories": repositories,
        "environments": environments,
        "duration_seconds": round(time.time() - started, 3),
    }
    write_json(output_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate TDD-Bench for BRT local-Conda execution without Docker."
    )
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--repo_root_base", required=True)
    parser.add_argument("--output_path", required=True)
    parser.add_argument(
        "--limit_groups",
        type=int,
        default=0,
        help="Audit only the first N groups for a bounded smoke test.",
    )
    args = parser.parse_args()
    payload = run_preflight(
        Path(args.instances_path),
        Path(args.repo_root_base),
        Path(args.output_path),
        max(0, args.limit_groups),
    )
    print(
        json.dumps(
            {
                "status": payload["status"],
                "runtime_backend": payload["runtime_backend"],
                "dataset_instances": payload["dataset_instances"],
                "environment_groups": payload["environment_groups"],
                "audited_groups": payload["audited_groups"],
                "ready_groups": payload["ready_groups"],
                "ready_instances": payload["ready_instances"],
                "template_contaminated_groups": payload[
                    "template_contaminated_groups"
                ],
                "runtime_isolation_smoke_ok": payload[
                    "runtime_isolation_smoke"
                ].get("ok"),
                "docker_harness_invoked": payload["docker_harness_invoked"],
                "output_path": str(Path(args.output_path).resolve()),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if payload["status"] == "READY" else 3


if __name__ == "__main__":
    raise SystemExit(main())

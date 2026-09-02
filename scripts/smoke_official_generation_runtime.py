#!/usr/bin/env python3
"""Real no-LLM smoke test for the official generation Docker seam."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from brt6.core.schema import InstanceContext
from brt6.core.utils import safe_json_dump
from brt6.execution.executor import run_command_in_conda
from brt6.execution.feedback import prepare_instance_worktree
from brt6.io.io_utils import infer_repo_path, load_issue_data
from brt6.runtime.official_docker_runtime import official_generation_runtime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances-path", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--repo-root-base", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--official-python", required=True)
    parser.add_argument("--harness-root", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    issues = load_issue_data(args.instances_path)
    row = issues[args.instance_id]
    source_repo = infer_repo_path(
        args.repo_root_base, row, args.instance_id
    )
    context = InstanceContext(
        instance_id=args.instance_id,
        issue_text=str(row.get("issue_text") or ""),
        repo=str(row["repo"]),
        base_commit=str(row["base_commit"]),
        buggy_repo_path=source_repo,
        metadata={
            "version": str(row.get("version") or ""),
            "environment_setup_commit": str(
                row.get("environment_setup_commit") or row["base_commit"]
            ),
        },
    )
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    worktree = ""
    result = None
    try:
        with official_generation_runtime(
            dataset_mode="swt",
            issue_row=row,
            official_python=args.official_python,
            harness_root=args.harness_root,
            source_repo=source_repo,
            output_dir=str(output),
            startup_timeout=args.timeout,
        ):
            worktree, prepare = prepare_instance_worktree(
                context, str(output), "", args.timeout, True
            )
            safe_json_dump(prepare, str(output / "repo_prepare.json"))
            result = run_command_in_conda(
                "python -c 'assert 6 * 7 == 42'",
                worktree,
                "forbidden-host-env",
                args.timeout,
                False,
                None,
                args.instance_id,
            )
            safe_json_dump(result.to_dict(), str(output / "smoke_result.json"))
    finally:
        if worktree:
            subprocess.run(
                ["git", "-C", source_repo, "worktree", "remove", "--force", worktree],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
            )
    if result is None or result.status != "PASS":
        raise SystemExit(
            "official generation Docker smoke failed: "
            + json.dumps(result.to_dict() if result else {}, ensure_ascii=False)
        )
    print(
        json.dumps(
            {
                "status": result.status,
                "returncode": result.returncode,
                "runtime_backend": "official_docker",
                "instance_id": args.instance_id,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

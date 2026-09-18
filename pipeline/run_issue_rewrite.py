"""CLI for the BRT6 issue rewrite stage."""

from __future__ import annotations

import argparse
import json
import os
import traceback
import threading
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..core.config import DEFAULT_MAX_TOKENS, DEFAULT_MAX_WORKERS, DEFAULT_TEMPERATURE, DEFAULT_TOP_CODE, DEFAULT_TOP_TESTS
from ..io.io_utils import build_instance_context, load_issue_data
from ..issue.issue_rewriter import rewrite_issue
from ..llm.llm_client import LLMClient
from ..llm.errors import LLMUnavailableError
from ..runtime.step_journal import init_resume_run, instance_journal, pause_status, atomic_json, read_json
from ..core.utils import ensure_dir, safe_json_dump
from ..runtime.conda_env_manager import preflight_system


def _has_valid_behavior_target(path: Path, instance_id: str) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("instance_id") == instance_id
        and isinstance(payload.get("trigger"), dict)
        and isinstance(payload.get("oracle"), dict)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the BRT6 issue rewrite stage.")
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--code_retrieval_path", required=True)
    parser.add_argument("--test_retrieval_path", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--llm-provider",
        choices=("deepseek", "gpt"),
        default="deepseek",
    )
    parser.add_argument("--api_key", default=None)
    parser.add_argument("--base_url", default=None)
    parser.add_argument("--max_workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--top_code", type=int, default=DEFAULT_TOP_CODE)
    parser.add_argument("--top_tests", type=int, default=DEFAULT_TOP_TESTS)
    parser.add_argument("--instance_id", default=None)
    parser.add_argument(
        "--instance_ids_file",
        default="",
        help="Optional newline-delimited retry subset.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    return parser


def run_one(args: argparse.Namespace, instance_id: str, issue_row: dict) -> dict:
    try:
        event = getattr(args, "_api_pause_event", None)
        if event is not None and event.is_set():
            raise LLMUnavailableError("Model service paused; queued instance was not started")
        return _run_one_impl(args, instance_id, issue_row)
    except LLMUnavailableError as exc:
        return pause_status(args, instance_id, exc)


def _run_one_impl(args: argparse.Namespace, instance_id: str, issue_row: dict) -> dict:
    out_dir = Path(args.output_dir) / instance_id
    context = build_instance_context(
        instance_id,
        issue_row,
        args.code_retrieval_path,
        args.test_retrieval_path,
        top_code=args.top_code,
        top_tests=args.top_tests,
    )
    client = LLMClient(
        provider=args.llm_provider,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        cost_dir=args.output_dir,
        usage_context={"stage": "design1", "instance_id": instance_id},
    )
    journal = instance_journal(args, context, client, str(out_dir)) if getattr(args, "_resume_revision", None) else None
    completed = read_json(journal.directory / "target_complete.json") if journal else None
    target_path = out_dir / "behavior_target.json"
    if args.resume and completed and _has_valid_behavior_target(target_path, instance_id):
        if completed["target"] == target_path.read_bytes().decode("utf-8"):
            return {"instance_id": instance_id, "status": "SKIP"}
    try:
        with journal.activate() if journal else nullcontext():
            rewrite_issue(context, client, str(out_dir))
        if journal:
            atomic_json(journal.directory / "target_complete.json", {"target": target_path.read_bytes().decode("utf-8")})
        return {"instance_id": instance_id, "status": "OK"}
    except LLMUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"instance_id": instance_id, "status": "ERROR", "error": str(exc), "traceback": traceback.format_exc()}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    ensure_dir(args.output_dir)
    args._resume_revision = init_resume_run(args.output_dir, args.resume)
    args._api_pause_event = threading.Event()
    (Path(args.output_dir) / "api_paused.json").unlink(missing_ok=True)
    tmp_root = os.environ.get("TMPDIR") or str(Path(args.output_dir) / "tmp")
    Path(tmp_root).mkdir(parents=True, exist_ok=True)
    preflight = preflight_system(
        [args.output_dir, args.instances_path, args.code_retrieval_path, tmp_root]
    )
    safe_json_dump(
        preflight, str(Path(args.output_dir) / "environment_preflight.json")
    )
    if not preflight.get("ok"):
        raise SystemExit(
            "issue rewrite environment preflight failed; see environment_preflight.json"
        )
    issues = load_issue_data(args.instances_path)
    if args.instance_id and str(args.instance_ids_file or "").strip():
        parser.error("--instance_id and --instance_ids_file are mutually exclusive")
    if args.instance_id:
        ids = [args.instance_id]
    elif str(args.instance_ids_file or "").strip():
        ids = [
            line.strip()
            for line in Path(args.instance_ids_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(ids) != len(set(ids)):
            parser.error("--instance_ids_file contains duplicate IDs")
        missing_ids = [instance_id for instance_id in ids if instance_id not in issues]
        if missing_ids:
            parser.error(
                "--instance_ids_file contains IDs absent from the dataset: "
                + ", ".join(missing_ids)
            )
    else:
        ids = list(issues)
    if args.limit:
        ids = ids[: args.limit]
    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(run_one, args, iid, issues[iid]): iid for iid in ids if iid in issues}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(result["instance_id"], result["status"], flush=True)
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "OK"),
        "skip": sum(1 for r in results if r["status"] == "SKIP"),
        "error": sum(1 for r in results if r["status"] == "ERROR"),
        "paused_api": sum(1 for r in results if r["status"] == "PAUSED_API"),
        "results": sorted(results, key=lambda x: x["instance_id"]),
        "defaults": {"max_workers": DEFAULT_MAX_WORKERS, "top_code": DEFAULT_TOP_CODE, "top_tests": DEFAULT_TOP_TESTS},
    }
    safe_json_dump(summary, str(Path(args.output_dir) / "summary.json"))
    if summary["paused_api"]:
        raise SystemExit(75)


if __name__ == "__main__":
    main()

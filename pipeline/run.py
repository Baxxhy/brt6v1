"""Full BRT6 pipeline CLI."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
import os
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from ..core.ablation import AblationConfig, ablation_signature_from_summary
from ..core.behavior_target_cache import (
    behavior_cache_source_signature,
    validate_behavior_target_cache,
)
from ..core.config import (
    DEFAULT_MAX_SEMANTIC_ROUNDS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_WORKERS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    DEFAULT_TOP_CODE,
    DEFAULT_TOP_TESTS,
)
from ..execution.feedback import run_instance_pipeline
from ..retrieval.icore_runtime import remove_isolated_runtime_environment
from ..io.io_utils import build_instance_context, load_issue_data
from ..llm.llm_client import LLMClient
from ..runtime.infrastructure_errors import InfrastructureUnavailableError
from ..llm.errors import LLMUnavailableError
from ..runtime.step_journal import init_resume_run, instance_journal, pause_status
from ..core.utils import ensure_dir, safe_json_dump
from ..runtime.conda_env_manager import (
    default_env_name,
    environment_operation_lock,
    preflight_system,
)
from ..runtime.official_docker_runtime import (
    OFFICIAL_RUNTIME_BACKEND,
    official_docker_preflight,
    official_generation_runtime,
)


_CONDA_ENV_LOCKS: dict[str, threading.Lock] = {}
_CONDA_ENV_LOCKS_GUARD = threading.Lock()
_VENDORED_SWTBENCH_ROOT = (
    Path(__file__).resolve().parents[1] / "evaluation/vendor/swtbench"
)


def _conda_env_lock(env_name: str) -> threading.Lock:
    """Serialize installs and tests that mutate the same conda environment."""
    key = env_name or "__direct_host__"
    with _CONDA_ENV_LOCKS_GUARD:
        return _CONDA_ENV_LOCKS.setdefault(key, threading.Lock())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the full BRT6 bug reproduction pipeline.")
    parser.add_argument("--instances_path", required=True)
    parser.add_argument("--code_retrieval_path", required=True)
    parser.add_argument("--test_retrieval_path", required=True)
    parser.add_argument("--repo_root_base", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument(
        "--behavior-target-cache",
        "--behavior_target_cache",
        dest="behavior_target_cache",
        default="",
        help=(
            "Explicit frozen BehaviorTarget cache. The manifest, dataset, "
            "retrieval inputs, instance set, and per-file hashes are validated "
            "before generation. If omitted, the launcher regenerates targets."
        ),
    )
    parser.add_argument("--alignment-verifier", choices=("strict", "issue2test"), default="strict")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--llm-provider",
        choices=("deepseek", "gpt"),
        default="deepseek",
        help="Select the isolated API pool used by this run.",
    )
    parser.add_argument("--api_key", default=None)
    parser.add_argument("--base_url", default=None)
    parser.add_argument("--conda_env", default="")
    parser.add_argument("--max_workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max_semantic_rounds", type=int, default=DEFAULT_MAX_SEMANTIC_ROUNDS)
    parser.add_argument("--num_candidates", type=int, default=1)
    parser.add_argument("--instance_id", default=None)
    parser.add_argument(
        "--instance_ids",
        default="",
        help=(
            "Comma-separated recovery subset. The full dataset is still used "
            "to validate a frozen BehaviorTarget cache, but only these IDs are run."
        ),
    )
    parser.add_argument(
        "--instance_ids_file",
        default="",
        help="Newline-delimited recovery subset; mutually exclusive with ID flags.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--top_code", type=int, default=DEFAULT_TOP_CODE)
    parser.add_argument("--top_tests", type=int, default=DEFAULT_TOP_TESTS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no_conda", action="store_true")
    parser.add_argument("--dataset_mode", choices=("swt", "tdd"), default="swt")
    parser.add_argument(
        "--runtime_backend",
        choices=(OFFICIAL_RUNTIME_BACKEND,),
        default=OFFICIAL_RUNTIME_BACKEND,
        help="Generation executes only in official benchmark instance Docker images.",
    )
    parser.add_argument(
        "--official_harness_python",
        default=os.environ.get("OFFICIAL_HARNESS_PYTHON", ""),
    )
    parser.add_argument(
        "--swtbench_root",
        default=os.environ.get(
            "SWTBENCH_ROOT", str(_VENDORED_SWTBENCH_ROOT)
        ),
    )
    parser.add_argument(
        "--tddbench_root",
        default=os.environ.get(
            "TDD_BENCH_ROOT", "/root/Baxxhy/BugReproduce/TDD-Bench-Verified"
        ),
    )
    parser.add_argument(
        "--validation_mode",
        choices=["buggy_only"],
        default="buggy_only",
        help="Execute generated tests only on the buggy source during generation/selection.",
    )
    parser.add_argument(
        "--generate_only",
        action="store_true",
        help=(
            "Only prepare the selected evidence representation, recover static "
            "host context, and generate one test; do not execute pytest."
        ),
    )
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--enable_protocol_recovery", type=_parse_bool, default=True)
    parser.add_argument(
        "--mutation",
        "--enable_seed_mutation",
        dest="enable_seed_mutation",
        type=_parse_bool,
        default=True,
    )
    parser.add_argument(
        "--specialized-feedback",
        "--enable_specialized_feedback",
        dest="enable_specialized_feedback",
        type=_parse_bool,
        default=True,
    )
    parser.add_argument(
        "--environment-feedback",
        "--enable_environment_feedback",
        dest="enable_environment_feedback",
        type=_parse_bool,
        default=True,
    )
    parser.add_argument(
        "--trigger-feedback",
        "--enable_trigger_feedback",
        dest="enable_trigger_feedback",
        type=_parse_bool,
        default=True,
    )
    parser.add_argument(
        "--assertion-feedback",
        "--enable_assertion_feedback",
        dest="enable_assertion_feedback",
        type=_parse_bool,
        default=True,
    )
    parser.add_argument(
        "--semantic-delta",
        "--enable_semantic_delta",
        dest="enable_semantic_delta",
        type=_parse_bool,
        default=True,
    )
    parser.add_argument("--enable_strict_semantic_verifier", type=_parse_bool, default=True)
    parser.add_argument(
        "--behavior-target",
        "--enable_behavior_target",
        dest="enable_behavior_target",
        type=_parse_bool,
        default=True,
        help=(
            "Enable the structured BehaviorTarget. Set false for the "
            "'w/o Behavior Target' ablation; IssueRewrite/cache use is disabled."
        ),
    )
    return parser


def ablation_config_from_args(args: argparse.Namespace) -> AblationConfig:
    """Build and validate the one-factor-at-a-time experiment signature."""

    return AblationConfig(
        behavior_target=args.enable_behavior_target,
        mutation=args.enable_seed_mutation,
        specialized_feedback=args.enable_specialized_feedback,
        environment_feedback=args.enable_environment_feedback,
        trigger_feedback=args.enable_trigger_feedback,
        assertion_feedback=args.enable_assertion_feedback,
        semantic_delta=args.enable_semantic_delta,
    ).validate()


def resume_matches_ablation(
    summary: dict,
    ablation_config: AblationConfig,
    behavior_target_source_signature: str = "",
) -> bool:
    """Only reuse output from the same ablation and frozen behavior source."""

    if ablation_signature_from_summary(summary) != ablation_config.signature:
        return False
    if behavior_target_source_signature:
        return (
            summary.get("behavior_target_source_signature")
            == behavior_target_source_signature
        )
    return True


def configure_behavior_target_source(args: argparse.Namespace) -> dict:
    """Resolve an explicit cache or describe the launcher's regenerated source."""

    raw_cache = str(args.behavior_target_cache or "").strip()
    if raw_cache and not args.enable_behavior_target:
        raise ValueError(
            "--behavior-target-cache cannot be combined with --behavior-target off"
        )
    if not args.enable_behavior_target:
        return {
            "mode": "disabled",
            "cache_id": "",
            "source_signature": "behavior_target_disabled",
        }
    if not raw_cache:
        if args.alignment_verifier == "issue2test" and os.environ.get("BRT4_BEHAVIOR_CACHE_DIR"):
            return {
                "mode": "frozen_c1_issue_rewrite",
                "cache_path": os.environ["BRT4_BEHAVIOR_CACHE_DIR"],
                "cache_id": "semantic_delta_swt_full_20260903_155525",
                "source_signature": "",
            }
        return {
            "mode": "regenerated",
            "cache_id": "",
            "source_signature": "",
        }
    provenance = validate_behavior_target_cache(
        raw_cache,
        args.instances_path,
        dataset_mode=args.dataset_mode,
        code_retrieval_path=args.code_retrieval_path,
        test_retrieval_path=args.test_retrieval_path,
    )
    os.environ["BRT4_BEHAVIOR_CACHE_DIR"] = provenance["cache_path"]
    provenance["source_signature"] = behavior_cache_source_signature(provenance)
    return provenance


def configure_runtime_contract(args: argparse.Namespace) -> dict:
    contract = {
        "dataset_mode": args.dataset_mode,
        "runtime_backend": args.runtime_backend,
        "docker_harness_invoked": True,
        "host_project_environment_created": False,
        "gold_fields_allowed": [],
    }
    if args.runtime_backend != OFFICIAL_RUNTIME_BACKEND:
        raise ValueError(
            "brt6 generation only supports runtime_backend=official_docker"
        )
    if str(getattr(args, "conda_env", "") or "").strip():
        raise ValueError(
            "--conda_env is forbidden with the official Docker generation contract"
        )
    if not args.official_harness_python:
        raise ValueError("--official_harness_python is required")
    output_dir = str(getattr(args, "output_dir", "") or "").strip()
    runtime_root = Path(output_dir).resolve().parent if output_dir else None
    runtime_tmp = runtime_root / "tmp" if runtime_root else None
    runtime_cache = runtime_root / "cache" if runtime_root else None
    if runtime_tmp is not None and runtime_cache is not None:
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        runtime_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DOCKER_HOST", "unix:///run/mutate-docker.sock")
    if runtime_tmp is not None and runtime_cache is not None:
        os.environ.setdefault("TMPDIR", str(runtime_tmp))
        os.environ.setdefault("TEMP", str(runtime_tmp))
        os.environ.setdefault("TMP", str(runtime_tmp))
        os.environ.setdefault("XDG_CACHE_HOME", str(runtime_cache))
    os.environ["BRT_REQUIRE_OFFICIAL_DOCKER"] = "1"
    contract["official_harness_python"] = str(
        Path(args.official_harness_python).resolve()
    )
    contract["harness_root"] = str(
        Path(
            args.swtbench_root
            if args.dataset_mode == "swt"
            else args.tddbench_root
        ).resolve()
    )
    contract["host_execution_fallback_allowed"] = False
    contract["docker_host"] = os.environ["DOCKER_HOST"]
    contract["runtime_tmp"] = str(runtime_tmp or "")
    contract["runtime_cache"] = str(runtime_cache or "")
    return contract


def _parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def _default_env_name(issue_row: dict) -> str:
    return default_env_name(
        issue_row, prefix=os.environ.get("BRT4_CONDA_ENV_PREFIX")
    )


def _interleave_by_conda_env(
    instance_ids: list[str], issues: dict[str, dict]
) -> list[str]:
    """Keep workers busy by avoiding adjacent tasks that share one env lock."""
    buckets: dict[str, deque[str]] = defaultdict(deque)
    env_order: list[str] = []
    for instance_id in instance_ids:
        env_name = _default_env_name(issues.get(instance_id, {})) or instance_id
        if env_name not in buckets:
            env_order.append(env_name)
        buckets[env_name].append(instance_id)
    scheduled: list[str] = []
    while len(scheduled) < len(instance_ids):
        for env_name in env_order:
            if buckets[env_name]:
                scheduled.append(buckets[env_name].popleft())
    return scheduled


def select_instance_ids(args: argparse.Namespace, issues: dict[str, dict]) -> list[str]:
    """Select one auditable generation subset without changing cache identity."""

    selectors = [
        bool(args.instance_id),
        bool(str(args.instance_ids or "").strip()),
        bool(str(args.instance_ids_file or "").strip()),
    ]
    if sum(selectors) > 1:
        raise ValueError(
            "--instance_id, --instance_ids, and --instance_ids_file are mutually exclusive"
        )
    if args.instance_id:
        requested = [str(args.instance_id)]
    elif str(args.instance_ids_file or "").strip():
        requested = [
            line.strip()
            for line in Path(args.instance_ids_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(requested) != len(set(requested)):
            raise ValueError("--instance_ids_file contains duplicate instance IDs")
    elif str(args.instance_ids or "").strip():
        requested = [
            item.strip()
            for item in str(args.instance_ids).split(",")
            if item.strip()
        ]
        if len(requested) != len(set(requested)):
            raise ValueError("--instance_ids contains duplicate instance IDs")
    else:
        requested = list(issues)
    missing = [instance_id for instance_id in requested if instance_id not in issues]
    if missing:
        raise ValueError(
            "--instance_ids contains IDs absent from the dataset: "
            + ", ".join(missing)
        )
    return requested


def _resolve_conda_env(env_name: str) -> str:
    if not env_name:
        return ""
    names: list[str] = []
    try:
        proc = subprocess.run(
            ["conda", "env", "list", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        data = json.loads(proc.stdout or "{}")
        names = [Path(path).name for path in data.get("envs", [])]
    except Exception:
        names = []
    if not names:
        try:
            proc = subprocess.run(
                ["conda", "env", "list"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                names.append(line.split()[0])
        except Exception:
            return env_name
    matches = sorted(name for name in names if name.endswith(env_name))
    if env_name in names:
        return env_name
    for preferred in ("direct_brt_ecg_we1_", "direct_brt_ecg_we0_"):
        for name in matches:
            if name.startswith(preferred):
                return name
    if not matches:
        return env_name
    return matches[-1]


def _run_one(args: argparse.Namespace, instance_id: str, issue_row: dict) -> dict:
    try:
        event = getattr(args, "_api_pause_event", None)
        if event is not None and event.is_set():
            raise LLMUnavailableError("Model service paused; queued instance was not started")
        return _run_one_impl(args, instance_id, issue_row)
    except InfrastructureUnavailableError as exc:
        result = {"instance_id": instance_id, "status": "PAUSED_INFRA", "error": str(exc)}
        directory = Path(args.output_dir) / instance_id
        ensure_dir(directory)
        safe_json_dump(result, str(directory / "infrastructure_pause.json"))
        safe_json_dump(result, str(directory / "summary.json"))
        return result
    except LLMUnavailableError as exc:
        return pause_status(args, instance_id, exc)


def _run_one_impl(args: argparse.Namespace, instance_id: str, issue_row: dict) -> dict:
    ablation_config = ablation_config_from_args(args)
    out_dir = Path(args.output_dir) / instance_id
    summary_path = out_dir / "summary.json"
    if args.num_candidates != 1:
        raise ValueError("BRT6 supports exactly --num_candidates 1")
    context = build_instance_context(
        instance_id,
        issue_row,
        args.code_retrieval_path,
        args.test_retrieval_path,
        args.repo_root_base,
        args.top_code,
        args.top_tests,
    )
    client_type = LLMClient
    client = client_type(
        provider=args.llm_provider,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        cost_dir=args.output_dir,
        usage_context={"stage": "design2", "instance_id": instance_id},
    )
    client.resume_revision = getattr(args, "_resume_revision", None)
    journal = instance_journal(args, context, client, str(out_dir)) if client.resume_revision else None
    if args.resume and journal is not None and journal.completed(out_dir) is not None:
        return {"instance_id": instance_id, "status": "SKIP"}
    client.ensure_available()
    ensure_dir(out_dir)
    running_marker = out_dir / ".running"
    running_marker.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).astimezone().isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        conda_env = ""
        harness_root = (
            args.swtbench_root
            if args.dataset_mode == "swt"
            else args.tddbench_root
        )
        runtime_context = official_generation_runtime(
            dataset_mode=args.dataset_mode,
            issue_row=issue_row,
            official_python=args.official_harness_python,
            harness_root=harness_root,
            source_repo=context.buggy_repo_path,
            output_dir=str(out_dir),
            startup_timeout=max(
                args.timeout,
                int(os.environ.get("BRT_OFFICIAL_DOCKER_STARTUP_TIMEOUT", "7200")),
            ),
        )
        with runtime_context:
            result = run_instance_pipeline(
                context,
                client,
                str(out_dir),
                conda_env=conda_env,
                timeout=args.timeout,
                no_conda=True,
                max_semantic_rounds=args.max_semantic_rounds,
                validation_mode=args.validation_mode,
                generate_only=args.generate_only,
                enable_protocol_recovery=args.enable_protocol_recovery,
                enable_seed_mutation=args.enable_seed_mutation,
                enable_strict_semantic_verifier=args.enable_strict_semantic_verifier,
                enable_behavior_target=args.enable_behavior_target,
                ablation_config=ablation_config,
                alignment_verifier=args.alignment_verifier,
            )
        result_payload = result.to_dict()
        result_payload["alignment_verifier"] = args.alignment_verifier
        result_payload["llm_provider"] = args.llm_provider
        result_payload["llm_model"] = args.model
        result_payload["behavior_target_source"] = args.behavior_target_source
        result_payload[
            "behavior_target_source_signature"
        ] = args.behavior_target_source_signature
        safe_json_dump(result_payload, str(summary_path))
        if journal is not None:
            journal.finish(result, out_dir)
        return {
            "instance_id": instance_id,
            "status": result.status,
            "summary": result_payload,
        }
    except (LLMUnavailableError, InfrastructureUnavailableError):
        raise
    except Exception as exc:  # noqa: BLE001
        ensure_dir(out_dir)
        err = {
            "instance_id": instance_id,
            "status": "ERROR",
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "protocol_recovery_enabled": args.enable_protocol_recovery,
            "seed_mutation_enabled": args.enable_seed_mutation,
            "strict_verifier_enabled": args.enable_strict_semantic_verifier,
            "behavior_target_enabled": args.enable_behavior_target,
            "behavior_target_source": args.behavior_target_source,
            "behavior_target_source_signature": (
                args.behavior_target_source_signature
            ),
            "llm_provider": args.llm_provider,
            "llm_model": args.model,
            "method_variant": ablation_config.method_variant,
            "ablation_id": ablation_config.ablation_id,
            "ablation_signature": ablation_config.signature,
            "ablation_config": ablation_config.to_dict(),
            "selected_seed_file": "",
            "selected_seed_name": "",
            "seed_fallback_used": False,
            "delta_calls": 0,
            "repair_route_counts": {},
            "oracle_type": "",
            "strict_verifier_decision": "",
            "strict_failure_class": "",
            "final_reason": str(exc),
        }
        safe_json_dump(err, str(out_dir / "summary.json"))
        return err
    finally:
        if args.runtime_backend != OFFICIAL_RUNTIME_BACKEND and not args.no_conda:
            prepare_path = out_dir / "repo_prepare.json"
            try:
                prepare = json.loads(prepare_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                prepare = {}
            runtime_environment = (
                prepare.get("runtime_environment")
                if isinstance(prepare.get("runtime_environment"), dict)
                else {}
            )
            runtime_env = str(runtime_environment.get("env_name") or "")
            template_env = str(prepare.get("template_env_name") or "")
            if runtime_env and runtime_env != template_env:
                try:
                    cleanup = remove_isolated_runtime_environment(
                        runtime_env, str(out_dir), args.timeout
                    )
                except Exception as exc:  # noqa: BLE001
                    cleanup = {
                        "status": "REMOVE_ERROR",
                        "returncode": 1,
                        "env_name": runtime_env,
                        "error": repr(exc),
                    }
                safe_json_dump(
                    cleanup,
                    str(out_dir / "runtime_environment_cleanup.json"),
                )
        try:
            running_marker.unlink()
        except OSError:
            pass


def _load_instance_summary(output_dir: Path, instance_id: str, fallback: dict | None = None) -> dict:
    if fallback and fallback.get("status") in {"PAUSED_API", "PAUSED_INFRA"}:
        return dict(fallback)
    summary_path = output_dir / instance_id / "summary.json"
    if summary_path.is_file():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("instance_id", instance_id)
                return data
        except (OSError, ValueError, TypeError):
            pass
    if fallback:
        data = dict(fallback.get("summary") or fallback)
        data.setdefault("instance_id", instance_id)
        return data
    return {"instance_id": instance_id, "status": "MISSING_SUMMARY"}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.alignment_verifier == "issue2test":
        args.enable_strict_semantic_verifier = False
    try:
        ablation_config = ablation_config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        behavior_target_source = configure_behavior_target_source(args)
    except ValueError as exc:
        parser.error(str(exc))
    args.behavior_target_source = behavior_target_source
    args.behavior_target_source_signature = str(
        behavior_target_source.get("source_signature") or ""
    )
    ensure_dir(args.output_dir)
    args._resume_revision = init_resume_run(args.output_dir, args.resume)
    args._api_pause_event = threading.Event()
    (Path(args.output_dir) / "api_paused.json").unlink(missing_ok=True)
    safe_json_dump(
        {
            "dataset_mode": args.dataset_mode,
            "llm_provider": args.llm_provider,
            "llm_model": args.model,
            "alignment_verifier": args.alignment_verifier,
            "ranking": "unchanged_component1",
            "runtime_backend": args.runtime_backend,
            "max_workers": args.max_workers,
            "patch_cov_enabled": ablation_config.compute_patch_coverage,
            "behavior_target_source": behavior_target_source,
            "behavior_target_source_signature": (
                args.behavior_target_source_signature
            ),
            **ablation_config.to_dict(),
        },
        str(Path(args.output_dir) / "run_config.json"),
    )
    try:
        runtime_contract = configure_runtime_contract(args)
    except ValueError as exc:
        raise SystemExit(f"runtime contract error: {exc}") from exc
    safe_json_dump(
        runtime_contract, str(Path(args.output_dir) / "runtime_contract.json")
    )
    preflight = official_docker_preflight(
        args.dataset_mode,
        args.official_harness_python,
        args.swtbench_root,
        args.tddbench_root,
        [args.output_dir, args.instances_path, args.repo_root_base],
    )
    safe_json_dump(preflight, str(Path(args.output_dir) / "environment_preflight.json"))
    if not preflight.get("ok"):
        raise SystemExit(
            "generation environment preflight failed; see environment_preflight.json"
        )
    issues = load_issue_data(args.instances_path)
    try:
        ids = select_instance_ids(args, issues)
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit:
        ids = ids[: args.limit]
    if (
        args.runtime_backend != OFFICIAL_RUNTIME_BACKEND
        and not args.instance_id
        and not args.conda_env
    ):
        ids = _interleave_by_conda_env(ids, issues)
    results = []
    by_id: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(_run_one, args, iid, issues[iid]): iid for iid in ids if iid in issues}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            by_id[result["instance_id"]] = result
            print(result["instance_id"], result["status"], flush=True)
    output_dir = Path(args.output_dir)
    ordered_results = [
        {
            "instance_id": iid,
            "status": _load_instance_summary(output_dir, iid, by_id.get(iid)).get("status", "UNKNOWN"),
            "summary": _load_instance_summary(output_dir, iid, by_id.get(iid)),
        }
        for iid in ids
        if iid in issues
    ]
    summary = {
        "max_workers": args.max_workers,
        "total": len(ordered_results),
        "ok": sum(1 for r in ordered_results if r.get("status") not in {"ERROR", "PAUSED_API", "PAUSED_INFRA", "SKIP", "MISSING_SUMMARY"}),
        "skip": sum(1 for r in results if r.get("status") == "SKIP"),
        "error": sum(1 for r in ordered_results if r.get("status") in {"ERROR", "MISSING_SUMMARY"}),
        "paused_infra": sum(1 for r in ordered_results if r.get("status") == "PAUSED_INFRA"),
        "paused_api": sum(1 for r in ordered_results if r.get("status") == "PAUSED_API"),
        "completed_this_invocation": len(results),
        "results": ordered_results,
        "defaults": {
            "max_workers": DEFAULT_MAX_WORKERS,
            "num_candidates": 1,
            "max_semantic_rounds": DEFAULT_MAX_SEMANTIC_ROUNDS,
            "validation_mode": "buggy_only",
            "generate_only": False,
            "enable_protocol_recovery": args.enable_protocol_recovery,
            "enable_seed_mutation": args.enable_seed_mutation,
            "enable_specialized_feedback": args.enable_specialized_feedback,
            "enable_environment_feedback": args.enable_environment_feedback,
            "enable_trigger_feedback": args.enable_trigger_feedback,
            "enable_assertion_feedback": args.enable_assertion_feedback,
            "enable_semantic_delta": args.enable_semantic_delta,
            "enable_strict_semantic_verifier": args.enable_strict_semantic_verifier,
            "enable_behavior_target": args.enable_behavior_target,
            "behavior_target_source": behavior_target_source,
            "behavior_target_source_signature": (
                args.behavior_target_source_signature
            ),
            "method_variant": ablation_config.method_variant,
            "ablation_id": ablation_config.ablation_id,
            "ablation_signature": ablation_config.signature,
            "ablation_config": ablation_config.to_dict(),
            "patch_cov_enabled": ablation_config.compute_patch_coverage,
        },
    }
    safe_json_dump(summary, str(Path(args.output_dir) / "summary.json"))
    if summary["paused_api"] or summary["paused_infra"]:
        return 75
    return 1 if summary["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

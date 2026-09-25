"""Freeze strict-verifier accepted candidates and apply iCoRe ranking.

The generation pipeline has already judged whether each buggy execution is
issue-aligned.  This stage therefore consumes that frozen verdict instead of
asking a second LLM judge the same question.  Selection remains blind to the
fixed version, gold patch, test patch, and official labels.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from brt6.validation.component2_input import load_safe_issues
from brt6.validation.icore_heuristic_rank import PROTOCOL as ICORE_PROTOCOL
from brt6.validation.icore_heuristic_rank import rank_candidates
from brt6.core.utils import safe_json_dump


STRICT_ACCEPTED_STATUS = "ISSUE_ALIGNED_FAIL"
SELECTION_PROTOCOL = "strict_preferred_icore_exhausted_fallback_v3"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        safe_json_dump(value, path)


def matched_execution(seed: Path, code: str, summary: dict) -> dict | None:
    """Return the buggy execution that corresponds byte-for-byte to ``code``."""
    matches: list[tuple[int, dict]] = []
    for checkpoint in (seed / "checkpoints").glob("candidate_attempt_*.json"):
        candidate = checkpoint.with_suffix(".py")
        if not candidate.is_file() or candidate.read_text(encoding="utf-8") != code:
            continue
        record = read(checkpoint)
        execution = record.get("execution")
        if isinstance(execution, dict) and execution.get("returncode") is not None:
            matches.append((int(record.get("round_id", -1)), execution))
    if matches:
        return dict(max(matches, key=lambda item: item[0])[1])

    for candidate in sorted(seed.glob("candidate_round_*.py"), reverse=True):
        if candidate.read_text(encoding="utf-8") != code:
            continue
        round_id = candidate.stem.removeprefix("candidate_round_")
        execution_path = seed / f"execution_round_{round_id}.json"
        if execution_path.is_file():
            execution = read(execution_path)
            if execution.get("returncode") is not None:
                return dict(execution)

    # Do not fall back to ``summary.buggy_execution`` without a code identity
    # check.  A resumed or manually repaired seed can otherwise pair the new
    # final test with an execution produced by older source code.
    return None


def reset_selection_cache(output: Path) -> None:
    """Remove only derived selection artifacts before a deterministic rebuild."""

    for name in ("frozen", "decisions", "generation", "errors"):
        path = output / name
        if path.is_dir():
            shutil.rmtree(path)
    for name in (
        "manifest.json",
        "predictions_frozen.json",
        "progress.json",
        "completed.json",
    ):
        path = output / name
        if path.is_file():
            path.unlink()


def freeze_candidates(generation: Path, output: Path, issues: dict) -> dict:
    manifest_path = output / "manifest.json"
    instances: list[dict] = []
    missing: list[dict] = []
    for instance_id in issues:
        instance_dir = generation / instance_id
        top_summary_path = instance_dir / "summary.json"
        top_summary = read(top_summary_path) if top_summary_path.is_file() else {}
        direct_fallback_used = bool(top_summary.get("direct_fallback_used"))
        if direct_fallback_used:
            route_root = instance_dir / "direct_fallback"
            candidate_prefix = "direct_seed"
            generation_route = "direct_fallback_after_target_exhaustion"
        else:
            route_root = instance_dir
            candidate_prefix = "seed"
            generation_route = "target_primary"
        selected_index = None
        selected_path = route_root / "selected_seed_summary.json"
        if selected_path.is_file():
            selected_index = read(selected_path).get("selected_seed_index")

        candidates: list[dict] = []
        for seed_index in range(3):
            source_seed_id = f"seed_{seed_index}"
            candidate_id = f"{candidate_prefix}_{seed_index}"
            seed = route_root / "seed_candidates" / source_seed_id
            summary_path = seed / "summary.json"
            candidate_path = seed / "final_test.py"
            if not summary_path.is_file() or not candidate_path.is_file():
                missing.append(
                    {
                        "instance_id": instance_id,
                        "candidate_id": candidate_id,
                        "reason": "MISSING_COMPLETE_SEED",
                    }
                )
                continue

            summary = read(summary_path)
            code = candidate_path.read_text(encoding="utf-8")
            execution = matched_execution(seed, code, summary)
            if execution is None:
                missing.append(
                    {
                        "instance_id": instance_id,
                        "candidate_id": candidate_id,
                        "reason": "NO_CODE_MATCHED_BUGGY_EXECUTION",
                    }
                )
                continue

            execution.setdefault("instance_id", instance_id)
            destination = output / "frozen" / instance_id / candidate_id
            write(destination / "candidate.py", code)
            write(destination / "buggy_execution.json", execution)
            write(destination / "source_summary.json", summary)
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "seed_index": seed_index,
                    "component1_rank": 0 if seed_index == selected_index else seed_index + 1,
                    "strict_status": str(summary.get("status") or ""),
                }
            )

        candidates.sort(key=lambda row: (row["component1_rank"], row["seed_index"]))
        for rank, candidate in enumerate(candidates):
            candidate["component1_rank"] = rank
        instances.append(
            {
                "instance_id": instance_id,
                "generation_route": generation_route,
                "component1_selected_candidate": (
                    f"{candidate_prefix}_{selected_index}"
                    if selected_index in (0, 1, 2)
                    else None
                ),
                "candidates": candidates,
            }
        )

    manifest = {
        "source_generation": str(generation),
        "instances": instances,
        "candidates": sum(len(row["candidates"]) for row in instances),
        "missing_candidates": missing,
        "selection_inputs": [
            "issue",
            "candidate",
            "matched_buggy_execution",
            "frozen_strict_verifier_status",
        ],
        "accepted_status": STRICT_ACCEPTED_STATUS,
        "selection_protocol": SELECTION_PROTOCOL,
        "rank_protocol": ICORE_PROTOCOL,
        "generation_routes": {
            "target_primary": sum(
                row["generation_route"] == "target_primary" for row in instances
            ),
            "direct_fallback_after_target_exhaustion": sum(
                row["generation_route"]
                == "direct_fallback_after_target_exhaustion"
                for row in instances
            ),
        },
        "additional_llm_judge": False,
        "fixed_side_used_for_selection": False,
        "gold_fields_loaded": [],
    }
    write(manifest_path, manifest)
    return manifest


def process_instance(output: Path, issues: dict, row: dict) -> dict:
    instance_id = row["instance_id"]
    decision_path = output / "decisions" / instance_id / "decision.json"
    accepted: list[dict] = []
    excluded: list[dict] = []
    available: list[dict] = []
    for candidate in sorted(row["candidates"], key=lambda item: item["component1_rank"]):
        if candidate["strict_status"] != STRICT_ACCEPTED_STATUS:
            excluded.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "strict_status": candidate["strict_status"],
                }
            )
        frozen = output / "frozen" / instance_id / candidate["candidate_id"]
        available.append(
            {
                "candidate_id": candidate["candidate_id"],
                "component1_rank": candidate["component1_rank"],
                "code": (frozen / "candidate.py").read_text(encoding="utf-8"),
                "buggy_execution": read(frozen / "buggy_execution.json"),
            }
        )

    accepted = [item for item in available if any(
        c["candidate_id"] == item["candidate_id"] and c["strict_status"] == STRICT_ACCEPTED_STATUS
        for c in row["candidates"]
    )]
    if accepted:
        selected, ranking = rank_candidates(
            issues[instance_id]["problem_statement"], accepted
        )
        route = "STRICT_ACCEPTED_THEN_ICORE_RANK"
    elif available:
        selected, ranking = rank_candidates(issues[instance_id]["problem_statement"], available)
        route = "EXHAUSTED_NO_STRICT_ACCEPTED_THEN_ICORE_RANK"
        excluded = []
    else:
        selected, ranking = None, []
        route = "NO_AVAILABLE_CANDIDATE"

    result = {
        "instance_id": instance_id,
        "generation_route": row.get("generation_route", "target_primary"),
        "component1_selected_candidate": row["component1_selected_candidate"],
        "selected": selected,
        "route": route,
        "accepted_candidates": [row["candidate_id"] for row in accepted],
        "excluded_candidates": excluded,
        "ranking": ranking,
    }
    write(decision_path, result)
    return result


def materialize(output: Path, predictions: list[dict]) -> None:
    generation = output / "generation"
    for prediction in predictions:
        selected = prediction["selected"]
        if selected is None:
            continue
        instance_id = prediction["instance_id"]
        frozen = output / "frozen" / instance_id / selected
        destination = generation / instance_id
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(frozen / "candidate.py", destination / "final_test.py")
        summary = read(frozen / "source_summary.json")
        summary.update(
            {
                "instance_id": instance_id,
                "strict_status": summary.get("status", ""),
                "selection_route": prediction["route"],
                "strict_accepted": summary.get("status") == STRICT_ACCEPTED_STATUS,
                "final_test_path": str(destination / "final_test.py"),
                "buggy_execution": read(frozen / "buggy_execution.json"),
                "selected_seed_name": selected,
                "selection_protocol": SELECTION_PROTOCOL,
            }
        )
        write(destination / "summary.json", summary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--issues", type=Path, default=ROOT / "data/issues/swt276_issues.json"
    )
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--instance-ids-file", type=Path, default=None)
    args = parser.parse_args()
    args.generation = args.generation.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)

    issues = load_safe_issues(args.issues)
    if args.instance_ids_file is not None:
        requested = [
            line.strip()
            for line in args.instance_ids_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(requested) != len(set(requested)):
            raise ValueError("--instance-ids-file contains duplicate IDs")
        missing = [instance_id for instance_id in requested if instance_id not in issues]
        if missing:
            raise ValueError(
                "--instance-ids-file contains IDs absent from the dataset: "
                + ", ".join(missing)
            )
        issues = {instance_id: issues[instance_id] for instance_id in requested}

    # This stage is local and deterministic. Rebuilding is cheap and prevents
    # a resumed generation from being ranked against stale frozen candidates.
    reset_selection_cache(args.output)
    manifest = freeze_candidates(args.generation, args.output, issues)
    print(
        json.dumps(
            {
                "stage": "frozen",
                "instances": len(manifest["instances"]),
                "candidates": manifest["candidates"],
                "missing_candidates": len(manifest["missing_candidates"]),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.prepare_only:
        return

    predictions: list[dict] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_instance, args.output, issues, row): row
            for row in manifest["instances"]
        }
        for completed, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                prediction = future.result()
                predictions.append(prediction)
                print(
                    json.dumps(
                        {
                            "stage": "rank",
                            "completed": completed,
                            "total": len(futures),
                            "instance_id": row["instance_id"],
                            "selected": prediction["selected"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                error = {"instance_id": row["instance_id"], "error": repr(exc)}
                errors.append(error)
                write(args.output / "errors" / f"{row['instance_id']}.json", error)
            write(
                args.output / "progress.json",
                {
                    "completed": len(predictions),
                    "total": len(futures),
                    "errors": errors,
                },
            )
    if errors:
        raise RuntimeError("Strict-verifier ranking is incomplete; see progress.json")

    predictions.sort(key=lambda row: row["instance_id"])
    write(
        args.output / "predictions_frozen.json",
        {
            "predictions": predictions,
            "fixed_side_used_for_selection": False,
            "official_results_loaded": False,
            "selection_protocol": SELECTION_PROTOCOL,
            "rank_protocol": ICORE_PROTOCOL,
            "additional_llm_judge": False,
        },
    )
    materialize(args.output, predictions)
    write(
        args.output / "completed.json",
        {
            "instances": len(predictions),
            "selected": sum(row["selected"] is not None for row in predictions),
            "no_submission": sum(row["selected"] is None for row in predictions),
        },
    )


if __name__ == "__main__":
    main()

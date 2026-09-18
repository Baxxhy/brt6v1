"""Frozen-run target revision pilot. Generation never loads official outcomes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "scripts"))
from brt6.core.utils import extract_json_object, safe_json_dump, truncate_text
from brt6.io.io_utils import build_instance_context
from brt6.issue.issue_rewriter import behavior_from_dict
from brt6.issue.feedback_target_revision import SYSTEM, eligibility, apply_revision
from brt6.llm.llm_client import LLMClient
from brt6.llm.cost_tracking import write_cost_summary
from brt6.validation.component2_input import load_safe_issues
from run_generation_strict_icore import freeze_candidates, process_instance, materialize

ISSUES = ROOT / "data/issues/swt276_issues.json"
CODE = ROOT / "retrieval_results/code/code_retrieval_results_gpt.json"
TESTS = ROOT / "retrieval_results/test/icore/gpt/related_tests.json"
SWT = "/root/miniconda3/envs/swtbench/bin/python"


def read(p):
    return json.loads(Path(p).read_text())


def save(p, value):
    safe_json_dump(value, p)


def prepare(source, output):
    """Select failures from generation verdicts, not historical F2P labels."""
    path = output / "manifest.json"
    if path.exists():
        manifest = read(path)
        if manifest["source_run"] != str(source):
            raise ValueError("source changed on resume")
        return manifest
    issues = load_safe_issues(ISSUES)
    baseline = source / "strict_icore_exhausted_fallback/generation"
    if len(list(baseline.glob("*/final_test.py"))) != len(issues):
        raise ValueError("complete frozen baseline required")
    rows = []
    for iid in sorted(issues):
        primary = source / "design2_individual_adaptation" / iid
        top = read(primary / "summary.json")
        selected = read(baseline / iid / "summary.json")
        if selected.get("strict_accepted") or selected.get("status") == "ISSUE_ALIGNED_FAIL":
            continue
        target = behavior_from_dict(iid, read(primary / "behavior_target.json"))
        branches = {}
        for k in range(3):
            sid = f"seed_{k}"
            seed = primary / "seed_candidates" / sid
            if not (seed / "summary.json").exists():
                continue
            summary = read(seed / "summary.json")
            # Only generation fields; no dual_version_result or official labels.
            checkpoints = []
            for p in sorted((seed / "checkpoints").glob("candidate_attempt_*.json"),
                            key=lambda p: int(p.stem.rsplit("_", 1)[1])):
                ck = read(p)
                verifier = ck.get("verifier") or {}
                execution = ck.get("execution") or {}
                checkpoints.append({
                    "round_id": ck.get("round_id"),
                    "verifier": {key: verifier.get(key) for key in (
                        "decision", "failure_class", "reason", "semantic_gap", "failure_origin")},
                    "execution": {key: execution.get(key) for key in (
                        "status", "returncode", "timeout")},
                    "log": truncate_text(str(execution.get("stdout", "")) + "\n" + str(execution.get("stderr", "")), 2000),
                })
            feedback = json.dumps({"final_reason": summary.get("final_reason"),
                                   "checkpoints": checkpoints[-2:]}, ensure_ascii=False)
            branches[sid] = {"status": summary.get("status"),
                             "rounds_used": summary.get("rounds_used"), "feedback": feedback}
        context = build_instance_context(iid, issues[iid], str(CODE), str(TESTS),
                                         str(ROOT.parent / "swe_repos"), 6, 5)
        sources = {"issue": issues[iid]["problem_statement"]}
        for k, item in enumerate(context.retrieved_code):
            sources[f"code_{k}:{item.path}:{item.obj_name}"] = truncate_text(item.code_content, 12000)
        for k, item in enumerate(context.retrieved_tests):
            sources[f"test_{k}:{item.file}:{item.name}"] = truncate_text(item.code_content, 8000)
        folder = output / "inputs" / iid
        save(folder / "target.json", target.to_dict())
        save(folder / "sources.json", sources)
        save(folder / "branches.json", branches)
        rows.append({"instance_id": iid, "skip_reason": eligibility(target, branches),
                     "direct_fallback_already_attempted": top.get("direct_fallback_attempted"),
                     "direct_fallback_status": top.get("direct_fallback_status")})
    shutil.copytree(baseline, output / "baseline", dirs_exist_ok=True)
    manifest = {"protocol": "uncertainty_linked_one_target_revision_v1",
                "source_run": str(source), "instances": rows,
                "official_labels_loaded": False, "workers": 20, "max_target_revisions": 1,
                "max_semantic_rounds": 5, "direct_fallback": False,
                "adoption": "strict accepted revised branch -> unchanged iCoRe rank; otherwise frozen baseline",
                "limitations": "citation existence and scope are mechanical; semantic entailment and common-cause attribution are model judgments"}
    save(path, manifest)
    for name in ("issue/feedback_target_revision.py", "scripts/run_feedback_target_revision.py",
                 "scripts/evaluate_feedback_target_revision.py", "execution/feedback.py",
                 "scripts/run_generation_strict_icore.py"):
        dest = output / "code_snapshot" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    return manifest


def review(output, row):
    iid = row["instance_id"]
    dest = output / "reviews" / iid
    decision = dest / "decision.json"
    if decision.exists():
        return read(decision)
    if row["skip_reason"]:
        result = {"instance_id": iid, "action": "KEEP_TARGET", "reason": row["skip_reason"]}
        save(decision, result)
        return result
    inputs = output / "inputs" / iid
    target = behavior_from_dict(iid, read(inputs / "target.json"))
    sources, branches = read(inputs / "sources.json"), read(inputs / "branches.json")
    flat = dataclasses.asdict(target)
    flat.pop("raw", None)
    prompt = json.dumps({"primary_target": flat, "sources": sources, "branches": branches}, ensure_ascii=False)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "prompt.txt").write_text(SYSTEM + "\n\n" + prompt)
    response_path = dest / "response.txt"
    if not response_path.exists():
        response = LLMClient(provider="deepseek", model="deepseek-v4-flash", temperature=0,
                             max_tokens=4096, cost_dir=str(output),
                             usage_context={"stage": "target_review", "instance_id": iid}).chat(SYSTEM, prompt)
        response_path.write_text(response)
    try:
        proposal = extract_json_object(response_path.read_text())
        save(dest / "proposal.json", proposal)
        revised = apply_revision(target, proposal, sources, branches)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        result = {"instance_id": iid, "action": "KEEP_TARGET", "reason": str(exc), "review_error": True}
    else:
        result = {"instance_id": iid, "action": "REVISE_TARGET" if revised else "KEEP_TARGET",
                  "reason": proposal.get("reason")}
        if revised:
            save(output / "target_cache" / iid / "behavior_target.json", revised.to_dict())
    save(decision, result)
    return result


def run_adaptation(output, ids):
    if not ids:
        return
    ids_path = output / "revised_ids.txt"
    ids_path.write_text("\n".join(ids) + "\n")
    cmd = [sys.executable, "-u", "-m", "brt6.pipeline.run",
           "--instances_path", str(ISSUES), "--instance_ids_file", str(ids_path),
           "--code_retrieval_path", str(CODE), "--test_retrieval_path", str(TESTS),
           "--repo_root_base", str(ROOT.parent / "swe_repos"), "--output_dir", str(output / "adaptation"),
           "--model", "deepseek-v4-flash", "--llm-provider", "deepseek",
           "--temperature", "0.1", "--max_tokens", "4096", "--max_workers", "20",
           "--max_semantic_rounds", "5", "--timeout", "7200", "--resume",
           "--alignment-verifier", "strict", "--validation_mode", "buggy_only",
           "--runtime_backend", "official_docker", "--dataset_mode", "swt",
           "--official_harness_python", SWT, "--swtbench_root", str(ROOT / "evaluation/vendor/swtbench")]
    save(output / "adaptation_command.json", cmd)
    env = dict(os.environ, BRT4_BEHAVIOR_CACHE_DIR=str(output / "target_cache"),
               BRT_DISABLE_DIRECT_FALLBACK="1", BRT_COST_DIR=str(output))
    with (output / "adaptation.log").open("a") as log:
        subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    if (output / "adaptation/api_paused.json").exists():
        raise RuntimeError("adaptation paused on API; resume without regenerating review")
    for iid in ids:
        if not (output / "adaptation" / iid / "summary.json").exists():
            raise RuntimeError(f"adaptation incomplete: {iid}")


def freeze_outputs(output, ids):
    issues = load_safe_issues(ISSUES)
    selection = output / "selection"
    manifest = freeze_candidates(output / "adaptation", selection, {i: issues[i] for i in ids})
    predictions = [process_instance(selection, issues, row) for row in manifest["instances"]]
    # Revised-but-rejected tests never replace the completed baseline.
    adopted = [p for p in predictions if p["route"] == "STRICT_ACCEPTED_THEN_ICORE_RANK"]
    materialize(selection, adopted)
    final = output / "final/generation"
    shutil.copytree(output / "baseline", final, dirs_exist_ok=True)
    for p in adopted:
        shutil.copytree(selection / "generation" / p["instance_id"], final / p["instance_id"], dirs_exist_ok=True)
    changed = []
    from brt6.evaluation.official_benchmarks import export_official_predictions
    # Compare complete patches, including placement, using buggy-only metadata.
    export_official_predictions(output / "baseline", ISSUES, output / "baseline_predictions.json")
    export_official_predictions(final, ISSUES, output / "final_predictions.json")
    old = {r["instance_id"]: r["model_patch"] for r in read(output / "baseline_predictions.json")}
    for r in read(output / "final_predictions.json"):
        if r["model_patch"] != old[r["instance_id"]]:
            changed.append(r["instance_id"])
    result = {"revised_instances": ids, "adopted_instances": [p["instance_id"] for p in adopted],
              "changed_instances": changed, "predictions": predictions, "official_labels_loaded": False}
    save(output / "outputs_frozen.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = prepare(args.source_run.resolve(), output)
    print(json.dumps({"stage": "prepared", "instances": len(manifest["instances"]),
                      "review_eligible": sum(not r["skip_reason"] for r in manifest["instances"])}), flush=True)
    if args.prepare_only:
        return
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(review, output, r): r["instance_id"] for r in manifest["instances"]}
        reviews = []
        errors = []
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"instance_id": futures[future], "action": "ERROR", "error_type": type(exc).__name__}
                errors.append(result)
            reviews.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    save(output / "review_summary.json", reviews)
    write_cost_summary(output)
    if errors:
        raise RuntimeError("API/review failure: resume before adaptation; see review_summary.json")
    if args.review_only:
        return
    ids = sorted(r["instance_id"] for r in reviews if r["action"] == "REVISE_TARGET")
    if not (output / "outputs_frozen.json").exists():
        run_adaptation(output, ids)
        frozen = freeze_outputs(output, ids)
        print(json.dumps({"stage": "outputs_frozen", "changed": frozen["changed_instances"]}), flush=True)
    write_cost_summary(output)
    # Labels become available only inside this independent evaluation process.
    subprocess.run([SWT, "-u", str(ROOT / "scripts/evaluate_feedback_target_revision.py"),
                    "--output", str(output)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()

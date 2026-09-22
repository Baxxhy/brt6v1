"""Offline prompt audit and bounded real-request cost comparison.

This deliberately does not alter the production pipeline. Live mode compares
the verifier and next planner on frozen buggy-only contexts, not end-to-end F2P.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from brt6.core.prompts import (SEED_MUTATION_PLAN_SYSTEM_PROMPT,
                             STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT)
from brt6.core.schema import CandidateTest, ExecutionResult, ProtocolRecovery, HostContext
from brt6.core.utils import extract_json_object
from brt6.issue.issue_rewriter import behavior_from_dict
from brt6.llm.llm_client import LLMClient
from brt6.llm.cost_tracking import write_cost_summary
from brt6.llm.prompt_cost_experiment import (
    compact_prompt, json_after, merged_request, replace_json_after,
    unpack_merged_response,
)
from brt6.validation.delta_guard import normalize_delta
from brt6.validation.strict_semantic_verifier import verify_strict_semantics
from brt6.execution.feedback import (
    _candidate_residual_state, _seed_residual_state, _residual_transition,
    _semantic_feedback_payload,
)


DEFAULT_SOURCE = ROOT / "results/runs/trait_xiaojing_deepseekv4flash_full_20260918_051829/design2_individual_adaptation"
DEFAULT_OUT = ROOT / "analysis/prompt_cost_pilot_20260918"
ARMS = ("baseline", "dedup", "merged", "merged_dedup")


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def section(text, left, right):
    return text.split(left, 1)[1].split(right, 1)[0].strip()


def split_prompt(text, system):
    prefix = system + "\n\n"
    if not text.startswith(prefix):
        raise ValueError("saved prompt differs from the current system prompt")
    return text[len(prefix):]


def prepare(source, out):
    if (out / "manifest.json").exists():
        manifest = read(out / "manifest.json")
        if Path(manifest["source"]).resolve() != source.resolve():
            raise ValueError("frozen source changed; use a new output directory")
        return manifest
    eligible, counts, audits = [], Counter(), []
    for path in sorted(source.glob("*/seed_candidates/seed_*/strict_verifier_round_*.json")):
        seed = path.parent
        round_id = int(path.stem.rsplit("_", 1)[1])
        verdict = read(path)
        if verdict.get("decision") not in {"repair_setup", "repair_trigger", "repair_oracle"}:
            continue
        vp = seed / "prompts" / f"strict_verifier_round_{round_id}.txt"
        dp = seed / "prompts" / f"delta_round_{round_id + 1}.txt"
        delta = seed / f"delta_round_{round_id + 1}.json"
        if not vp.is_file() or not dp.is_file() or not delta.is_file() or read(delta).get("status") != "VALID":
            continue
        counts["complete_transitions"] += 1
        # Audit complete transitions only. Quota-error retries are a separate
        # runtime problem and would bias this prompt-cost comparison.
        for prompt_path in (vp, dp, seed / "prompts" / f"generation_round_{round_id + 1}.txt"):
            if prompt_path.is_file():
                original = prompt_path.read_text()
                _, stats = compact_prompt(original)
                audits.append({"path":str(prompt_path), "kind":prompt_path.stem.split("_round_")[0], **stats})
        try:
            vu = split_prompt(vp.read_text(), STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT)
            du = split_prompt(dp.read_text(), SEED_MUTATION_PLAN_SYSTEM_PROMPT)
        except ValueError:
            counts["system_prompt_mismatch"] += 1
            continue
        vcode = section(vu, "Test code: ", "\nCommand:")
        dcode = section(du, "Current test (first round: retrieved seed; subsequent rounds: previous candidate):\n", "\nPrevious actual execution:")
        if vcode != dcode:
            counts["different_parent_checkpoint"] += 1
            continue
        counts["same_parent_checkpoint"] += 1
        if round_id != 0:
            continue
        if (seed / "candidate_round_0.py").read_text().strip() != vcode:
            counts["candidate_artifact_mismatch"] += 1
            continue
        iid = seed.parent.parent.name
        eligible.append({"instance_id": iid, "seed": seed.name, "round": round_id,
                         "source": str(seed), "verifier_user": vu, "planner_user": du})
    chosen, repos = [], set()
    for row in eligible:
        repo = row["instance_id"].split("__")[0]
        if repo in repos:
            continue
        chosen.append(row)
        repos.add(repo)
        if len(chosen) == 3:
            break
    for row in eligible:
        if len(chosen) == 3:
            break
        if row["instance_id"] not in {item["instance_id"] for item in chosen}:
            chosen.append(row)
    for row in chosen:
        folder = out / "inputs" / row["instance_id"]
        folder.mkdir(parents=True, exist_ok=True)
        source_seed = Path(row["source"])
        for name in ("behavior_target.json", "protocol_recovery.json", "host_context.json", "execution_round_0.json", "candidate_round_0.py"):
            (folder / name).write_bytes((source_seed / name).read_bytes())
        (folder / "verifier_user.txt").write_text(row.pop("verifier_user"))
        (folder / "planner_user.txt").write_text(row.pop("planner_user"))
        row["input"] = str(folder)
    totals = {}
    for kind in sorted({r["kind"] for r in audits}):
        group = [r for r in audits if r["kind"] == kind]
        a, b = sum(r["original_chars"] for r in group), sum(r["compact_chars"] for r in group)
        totals[kind] = {"prompts": len(group), "original_chars": a, "compact_chars": b,
                        "reduction_fraction": (a-b)/a if a else 0,
                        "all_exactly_reconstructed": all(r["exact_reconstruction"] for r in group)}
    manifest = {"source": str(source), "arms": list(ARMS), "instances": chosen,
                "selection": "first round, non-accepted verifier, valid next plan, same parent; prefer repository diversity, then distinct instances, no official labels",
                "interpretation": "diagnostic only, not F2P non-inferiority", "transitions": dict(counts),
                "model": "deepseek-v4-flash", "temperature": 0.1, "max_tokens": 4096,
                "all_prompt_statistics": totals}
    save(out / "manifest.json", manifest)
    save(out / "prompt_audit.json", audits)
    return manifest


class OneResponse:
    def __init__(self, value):
        self.value = value

    def chat(self, *_args, **_kwargs):
        return json.dumps(self.value, ensure_ascii=False)


def gate_verifier(row, data, destination):
    """Reuse the current actual acceptance gates; no new acceptance rules."""
    folder = Path(row["input"])
    iid = row["instance_id"]
    behavior = behavior_from_dict(iid, read(folder / "behavior_target.json"))
    protocol_data = read(folder / "protocol_recovery.json")
    protocol = ProtocolRecovery(**{k:v for k,v in protocol_data.items() if k in ProtocolRecovery.__dataclass_fields__})
    execution = ExecutionResult(**{k:v for k,v in read(folder / "execution_round_0.json").items() if k in ExecutionResult.__dataclass_fields__})
    user = (folder / "verifier_user.txt").read_text()
    issue = section(user, "Issue: ", "\nBehaviorTarget:")
    candidate = CandidateTest(iid, code=(folder / "candidate_round_0.py").read_text())
    decision, verified = verify_strict_semantics(issue, behavior, protocol, candidate, execution,
                                                "", OneResponse(data), str(destination), 0)
    host_data = read(folder / "host_context.json")
    host = HostContext(**{k:v for k,v in host_data.items() if k in HostContext.__dataclass_fields__})
    current = _candidate_residual_state(0, execution, decision, verified)
    transition = _residual_transition(_seed_residual_state(host), current, initialization=True)
    feedback = _semantic_feedback_payload(decision, verified, current, transition)
    return verified.to_dict(), feedback


def request(out, iid, arm, name, system, user):
    directory = out / "live" / iid / arm / name
    directory.mkdir(parents=True, exist_ok=True)
    prompt = system + "\n\n" + user
    prompt_file = directory / "prompt.txt"
    result_file = directory / "result.json"
    if result_file.exists():
        if prompt_file.read_text() != prompt:
            raise ValueError("resume input changed; use a new output directory")
        return read(result_file)["data"]
    prompt_file.write_text(prompt)
    client = LLMClient(provider="deepseek", model="deepseek-v4-flash", temperature=0.1,
                       max_tokens=4096, cost_dir=str(out / "costs" / arm),
                       usage_context={"stage":name, "instance_id":iid})
    try:
        response = client.chat(system, user, attempt_limit=2)
    except Exception as exc:
        message = str(exc)
        status = re.search(r"HTTP (\d{3})", message)
        save(directory / "error.json", {
            "error_type":type(exc).__name__,
            "http_status":int(status.group(1)) if status else None,
            "quota_exhausted":"insufficient_user_quota" in message or "余额不足" in message,
            "context_limit":"context_length" in message,
        })
        raise
    (directory / "response.txt").write_text(response)
    data = extract_json_object(response)
    save(result_file, {"data":data, "usage":getattr(client, "last_usage", {}),
                       "prompt_chars":len(prompt), "returned_model":getattr(client,"last_model",None)})
    return data


def run_case(row, out, index):
    iid = row["instance_id"]
    folder = Path(row["input"])
    vu = (folder / "verifier_user.txt").read_text()
    du = (folder / "planner_user.txt").read_text()
    results = {}
    # Rotate arm order to reduce a fixed provider-cache/time ordering advantage.
    arms = ARMS[index % len(ARMS):] + ARMS[:index % len(ARMS)]
    for arm in arms:
        output = out / "live" / iid / arm
        record = {"instance_id":iid, "arm":arm}
        try:
            vs = STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT
            ds = SEED_MUTATION_PLAN_SYSTEM_PROMPT
            if arm.startswith("merged"):
                system, user = merged_request(vs, vu, ds, du)
                if arm == "merged_dedup":
                    user, _ = compact_prompt(user)
                response = request(out, iid, arm, "verify_and_plan", system, user)
                raw_verifier, delta = unpack_merged_response(response)
            else:
                user = compact_prompt(vu)[0] if arm == "dedup" else vu
                raw_verifier = request(out, iid, arm, "verify", vs, user)
                delta = None
            verifier, feedback = gate_verifier(row, raw_verifier, output / "gated")
            has_plan = isinstance(delta, dict) and normalize_delta(iid, 1, delta).status == "VALID"
            needs_plan = verifier["decision"] != "accept" and (not arm.startswith("merged") or not has_plan)
            if needs_plan:
                # Use the real controller's fresh feedback, including residual
                # fields. Never feed the saved baseline diagnosis to a new arm.
                user = replace_json_after(du, "Previous Verifier:\n", feedback)
                if arm in {"dedup", "merged_dedup"}:
                    user, _ = compact_prompt(user)
                delta = request(out, iid, arm, "plan", ds, user)
                record["planner_fallback"] = arm.startswith("merged")
            valid = verifier["decision"] == "accept"
            normalized = None
            if not valid and isinstance(delta, dict):
                normalized = normalize_delta(iid, 1, delta).to_dict()
                valid = normalized["status"] == "VALID"
            record.update(status="OK" if valid else "INVALID_PLAN", verifier=verifier,
                          raw_verifier=raw_verifier, next_delta=normalized)
        except Exception as exc:
            # Credentials and provider response bodies are never printed.
            record.update(status="ERROR", error_type=type(exc).__name__)
        results[arm] = record
        save(output / "comparison.json", record)
        print(iid, arm, record["status"], record.get("verifier", {}).get("decision"), flush=True)
    return {"instance_id":iid, "arms":results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--api-pool", type=Path, default=ROOT / ".secrets/api_pool.json")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = prepare(args.source, args.output)
    print(json.dumps({k:v for k,v in manifest.items() if k != "instances"}, ensure_ascii=False, indent=2), flush=True)
    if not args.live:
        return
    os.environ.update(BRT_API_POOL_FILE=str(args.api_pool.resolve()),
                      BRT_ALLOWED_API_HOST="api.open.xiaojingai.com", BRT_DISABLE_THINKING="1",
                      BRT_LLM_STREAM="1", BRT3_LLM_MAX_ATTEMPTS="2", BRT3_LLM_REQUEST_TIMEOUT="600")
    # Keep each arm's accounting separate even when called from a full-run shell.
    os.environ.pop("BRT_COST_DIR", None)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda pair: run_case(pair[1], args.output, pair[0]), enumerate(manifest["instances"])))
    costs = {arm:write_cost_summary(args.output / "costs" / arm) for arm in ARMS}
    save(args.output / "live_results.json", {"cases":results, "costs":costs,
        "f2p":None, "limitation":"Verifier/planner comparison only; changed candidates require generation and independent official evaluation."})


if __name__ == "__main__":
    main()

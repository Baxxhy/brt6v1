"""Summarize a completed C1 ablation without selecting or filtering outcomes."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from brt6.core.utils import safe_json_dump
from brt6.llm.cost_tracking import write_cost_summary


def read(p):
    return json.loads(p.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    baseline = ROOT / "results/runs/trait_xiaojing_deepseekv4flash_full_20260918_051829"
    ablated_report = read(run / "evaluation/official_f2p/official_report.json")
    baseline_report_path = baseline / "evaluation/exhausted_fallback18/full276_merged_report.json"
    # A fresh server can finish the ablation without our historical artifacts.
    # Never replace absent baseline evidence with a hard-coded score.
    if not baseline_report_path.is_file():
        result = {"comparison_available": False,
                  "reason": "Historical main-run report is not present on this server",
                  "wo_c1_f2p": len(ablated_report["resolved_ids"]),
                  "total_instances": ablated_report.get("total_instances"),
                  "wo_c1_errors": ablated_report.get("error_ids", [])}
        safe_json_dump(result, run / "comparison.json")
        write_cost_summary(run / "design2_individual_adaptation")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    main_report = read(baseline_report_path)
    resume = read(baseline / "resume_20260918/manifest.json")
    main_success = set(main_report["resolved_ids"])
    ablated_success = set(ablated_report["resolved_ids"])
    dataset = read(ROOT / "data/issues/swt276_issues.json")
    ids = {r["instance_id"] for r in (dataset.values() if isinstance(dataset, dict) else dataset)}
    assert main_success <= ids and ablated_success <= ids
    def subset(group):
        return {"instances": len(group), "full_f2p": len(main_success & group),
                "wo_c1_f2p": len(ablated_success & group),
                "full_minus_wo_c1": len(main_success & group) - len(ablated_success & group)}
    result = {**subset(ids), "full_rate": 100 * len(main_success) / len(ids),
              "wo_c1_rate": 100 * len(ablated_success) / len(ids),
              "full_only_success": sorted(main_success - ablated_success),
              "wo_c1_only_success": sorted(ablated_success - main_success),
              "wo_c1_errors": ablated_report.get("error_ids", []),
              "legacy35": subset(set(resume["preserved_instances"])),
              "current241": subset(set(resume["pending_instances"])),
              "scope": "All 276 instances; both arms use strict-preferred ranking with exhausted fallback submission.",
              "limitations": [
                  "Historical full run retained 35 completed pre-resume instances; strata are defined by execution provenance, not outcomes.",
                  "Five rounds per branch in both arms; full method can additionally run three direct-fallback branches. This is a system ablation, not equal-total-budget isolation.",
                  "Independent stochastic model calls; one run cannot establish a guaranteed improvement."]}
    safe_json_dump(result, run / "comparison.json")
    write_cost_summary(run)
    write_cost_summary(run / "design2_individual_adaptation")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Independent post-freeze official evaluation; never feeds labels to generation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from brt6.core.utils import safe_json_dump


def read(p):
    return json.loads(p.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    frozen = read(out / "outputs_frozen.json")
    changed = frozen["changed_instances"]
    evaluation = out / "evaluation"
    if changed and not (evaluation / "official_report.json").exists():
        dataset = ROOT / "data/official/swt276_official_eval.json"
        cmd = [sys.executable, "-u", str(ROOT / "scripts/run_official_eval_after_generation.py"),
               "--dataset", "swt", "--outputs-dir", str(out / "final/generation"),
               "--dataset-file", str(dataset), "--official-dataset-name", str(dataset),
               "--evaluation-dir", str(evaluation), "--max-workers", "20", "--timeout", "7200",
               "--run-id", out.name, "--model-name", "trait-target-revision",
               "--compute-coverage", "false", "--official-python", sys.executable,
               "--swtbench-root", str(ROOT / "evaluation/vendor/swtbench")]
        for iid in changed:
            cmd.extend(["--instance-id", iid])
        with (out / "evaluation.log").open("a") as log:
            subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    manifest = read(out / "manifest.json")
    baseline = read(Path(manifest["source_run"]) / "evaluation/exhausted_fallback18/full276_merged_report.json")
    old = set(baseline["resolved_ids"])
    report = read(evaluation / "official_report.json") if changed else {"resolved_ids": [], "unresolved_ids": [], "error_ids": []}
    if changed and set(changed) != set(report["resolved_ids"] + report["unresolved_ids"] + report["error_ids"]):
        raise RuntimeError("incomplete official evaluation")
    new = (old - set(changed)) | set(report["resolved_ids"])
    recovered, regressed = sorted(new - old), sorted(old - new)
    cohort = {r["instance_id"] for r in manifest["instances"]}
    result = {"total_instances": 276, "baseline_f2p": len(old), "revised_f2p": len(new),
              "net_change": len(new) - len(old), "recovered_ids": recovered, "regressed_ids": regressed,
              "cohort_size": len(cohort), "cohort_baseline_f2p": len(old & cohort),
              "cohort_revised_f2p": len(new & cohort), "changed_ids": changed,
              "revised_target_ids": frozen["revised_instances"],
              "evaluation_error_ids": report["error_ids"],
              "resolved_ids": sorted(new),
              "interpretation": "development feasibility; not an equal-budget comparison or unseen generalization test"}
    safe_json_dump(result, out / "comparison.json")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    (out / "completed.done").write_text("generation frozen and evaluation merged\n")


if __name__ == "__main__":
    main()

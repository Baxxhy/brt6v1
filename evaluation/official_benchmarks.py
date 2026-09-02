"""Bridge BRT generation artifacts to official SWTBench/TDDBench inputs.

Both official harnesses consume a JSON list whose ``model_patch`` field is a
Git patch.  BRT keeps the selected test as ``final_test.py`` plus placement
metadata, so this module converts that artifact into a new-file test patch
without changing the generated code.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from ..core.utils import sanitize_instance_id


def load_dataset_rows(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else list(payload.values())
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"dataset is empty or invalid: {path}")
    instance_ids = [str(row.get("instance_id") or "") for row in rows]
    if any(not instance_id for instance_id in instance_ids):
        raise ValueError(f"dataset contains an empty instance_id: {path}")
    if len(instance_ids) != len(set(instance_ids)):
        raise ValueError(f"dataset contains duplicate instance IDs: {path}")
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _selected_summary(instance_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    if summary.get("candidate_repo_path") or summary.get("selected_seed_file"):
        return summary
    selected = summary.get("selected_seed_index")
    if isinstance(selected, int) and selected >= 0:
        nested = _read_json(
            instance_dir / "seed_candidates" / f"seed_{selected}" / "summary.json"
        )
        if nested:
            return nested
    attempts = summary.get("seed_attempts_summary") or []
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict) or attempt.get("seed_index") != selected:
                continue
            summary_path = Path(str(attempt.get("summary_path") or ""))
            if summary_path.is_file():
                nested = _read_json(summary_path)
                if nested:
                    return nested
    return summary


def _safe_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe generated test path: {value!r}")
    return str(path)


def resolve_generated_test_path(
    instance_id: str,
    instance_dir: str | Path,
) -> tuple[str, dict[str, Any]]:
    root = Path(instance_dir)
    summary = _selected_summary(root, _read_json(root / "summary.json"))
    direct = str(
        summary.get("candidate_repo_path")
        or summary.get("direct_test_repo_path_hint")
        or ""
    )
    if direct:
        return _safe_repo_path(direct), summary

    seed_file = str(summary.get("selected_seed_file") or "")
    host_dir = PurePosixPath(seed_file).parent if seed_file else PurePosixPath("tests")
    if instance_id.startswith("sympy__sympy-") and not (
        str(host_dir) == "sympy" or str(host_dir).startswith("sympy/")
    ):
        host_dir = PurePosixPath("sympy/tests")
    filename = f"test_brt_{sanitize_instance_id(instance_id)}.py"
    return _safe_repo_path(str(host_dir / filename)), summary


def new_file_patch(repo_path: str, source: str) -> str:
    path = _safe_repo_path(repo_path)
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    lines = normalized.splitlines()
    additions = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{additions}\n"
    )


def export_official_predictions(
    outputs_dir: str | Path,
    dataset_file: str | Path,
    output_path: str | Path,
    *,
    model_name: str = "brt6-deepseek-v3",
) -> dict[str, Any]:
    outputs = Path(outputs_dir)
    rows = load_dataset_rows(dataset_file)
    predictions: list[dict[str, Any]] = []
    generated_ids: list[str] = []
    missing_ids: list[str] = []
    placement: dict[str, str] = {}

    for row in rows:
        instance_id = str(row["instance_id"])
        instance_dir = outputs / instance_id
        final_test = instance_dir / "final_test.py"
        model_patch = ""
        repo_path = ""
        if final_test.is_file():
            source = final_test.read_text(encoding="utf-8", errors="replace")
            if source.strip():
                repo_path, _ = resolve_generated_test_path(instance_id, instance_dir)
                model_patch = new_file_patch(repo_path, source)
                generated_ids.append(instance_id)
                placement[instance_id] = repo_path
            else:
                missing_ids.append(instance_id)
        else:
            missing_ids.append(instance_id)
        predictions.append(
            {
                "instance_id": instance_id,
                "model_name_or_path": model_name,
                "model_patch": model_patch,
            }
        )

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(predictions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "dataset_file": str(Path(dataset_file).resolve()),
        "outputs_dir": str(outputs.resolve()),
        "predictions_path": str(destination.resolve()),
        "model_name_or_path": model_name,
        "total_instances": len(rows),
        "generated_instances": len(generated_ids),
        "missing_instances": len(missing_ids),
        "generated_ids": generated_ids,
        "missing_ids": missing_ids,
        "placement": placement,
    }
    destination.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def generation_completeness(export_manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a strict gate result for launching an official formal evaluation."""

    total = int(export_manifest.get("total_instances") or 0)
    generated = int(export_manifest.get("generated_instances") or 0)
    missing = int(export_manifest.get("missing_instances") or 0)
    missing_ids = [str(item) for item in export_manifest.get("missing_ids") or []]
    complete = total > 0 and generated == total and missing == 0 and not missing_ids
    reason = ""
    if not complete:
        reason = (
            "formal evaluation requires complete generation: "
            f"generated={generated}/{total}, missing={missing}"
        )
    return {
        "complete": complete,
        "total_instances": total,
        "generated_instances": generated,
        "missing_instances": missing,
        "missing_ids": missing_ids,
        "reason": reason,
    }

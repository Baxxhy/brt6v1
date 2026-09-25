"""Versioned, portable BehaviorTarget cache support.

A frozen cache is a repository-friendly experiment input.  Its manifest binds
the BehaviorTargets to the exact dataset and retrieval evidence that produced
them, while per-instance hashes prevent silent edits or partial copies.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = "brt5.behavior_target_cache.v1"
BEHAVIOR_SCHEMA_VERSION = "behavior_target.lossless.v1"
MANIFEST_FILENAME = "manifest.json"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"required file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def _dataset_instance_ids(instances_path: str | Path) -> list[str]:
    path = Path(instances_path).expanduser().resolve()
    payload = _load_json(path)
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
    elif isinstance(payload, dict):
        rows = [row for row in payload.values() if isinstance(row, dict)]
    else:
        raise ValueError(
            f"unsupported dataset structure in {path}: {type(payload).__name__}"
        )
    ids = [str(row.get("instance_id") or "").strip() for row in rows]
    if not rows or any(not instance_id for instance_id in ids):
        raise ValueError(f"dataset contains empty or invalid rows: {path}")
    if len(ids) != len(set(ids)):
        raise ValueError(f"dataset contains duplicate instance_id values: {path}")
    return ids


def _safe_instance_path(cache_dir: Path, instance_id: str) -> Path:
    if Path(instance_id).name != instance_id or instance_id in {".", ".."}:
        raise ValueError(f"unsafe instance_id in cache manifest: {instance_id!r}")
    return cache_dir / instance_id / "behavior_target.json"


def behavior_cache_source_signature(provenance: dict[str, Any] | None) -> str:
    """Return a location-independent identity used by resume checks."""

    if not provenance:
        return ""
    cache_id = str(provenance.get("cache_id") or "")
    manifest_sha256 = str(provenance.get("manifest_sha256") or "")
    if not cache_id or not manifest_sha256:
        return ""
    return f"{cache_id}:{manifest_sha256}"


def validate_behavior_target_cache(
    cache_dir: str | Path,
    instances_path: str | Path,
    *,
    dataset_mode: str | None = None,
    code_retrieval_path: str | Path | None = None,
    test_retrieval_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate a frozen cache and return portable provenance metadata."""

    root = Path(cache_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"BehaviorTarget cache directory does not exist: {root}")
    manifest_path = root / MANIFEST_FILENAME
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"cache manifest must be a JSON object: {manifest_path}")
    if manifest.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError(
            "unsupported BehaviorTarget cache schema: "
            f"{manifest.get('cache_schema_version')!r}; expected {CACHE_SCHEMA_VERSION!r}"
        )
    if dataset_mode and manifest.get("dataset_mode") != dataset_mode:
        raise ValueError(
            f"cache dataset_mode={manifest.get('dataset_mode')!r} does not match "
            f"requested dataset_mode={dataset_mode!r}"
        )

    instance_ids = _dataset_instance_ids(instances_path)
    expected_ids = set(instance_ids)
    records = manifest.get("behavior_targets")
    if not isinstance(records, dict):
        raise ValueError("cache manifest behavior_targets must be an object")
    cached_ids = set(str(key) for key in records)
    if cached_ids != expected_ids:
        missing = sorted(expected_ids - cached_ids)
        extra = sorted(cached_ids - expected_ids)
        raise ValueError(
            "BehaviorTarget cache instance set does not match dataset: "
            f"missing={missing[:10]} (total={len(missing)}), "
            f"extra={extra[:10]} (total={len(extra)})"
        )
    if int(manifest.get("instance_count") or -1) != len(instance_ids):
        raise ValueError(
            f"cache instance_count={manifest.get('instance_count')!r} does not "
            f"match dataset size={len(instance_ids)}"
        )

    instances = Path(instances_path).expanduser().resolve()
    actual_dataset_hash = sha256_file(instances)
    expected_dataset_hash = str(manifest.get("dataset_sha256") or "")
    if actual_dataset_hash != expected_dataset_hash:
        raise ValueError(
            "dataset hash does not match frozen BehaviorTarget evidence: "
            f"actual={actual_dataset_hash}, expected={expected_dataset_hash}"
        )

    retrieval_checks = (
        ("code_retrieval_sha256", code_retrieval_path),
        ("test_retrieval_sha256", test_retrieval_path),
    )
    for manifest_key, supplied_path in retrieval_checks:
        if supplied_path is None:
            continue
        actual = sha256_file(Path(supplied_path).expanduser().resolve())
        expected = str(manifest.get(manifest_key) or "")
        if not expected or actual != expected:
            raise ValueError(
                f"{manifest_key} mismatch for frozen BehaviorTarget evidence: "
                f"actual={actual}, expected={expected or '<missing>'}"
            )

    expected_behavior_schema = str(
        manifest.get("behavior_schema_version") or BEHAVIOR_SCHEMA_VERSION
    )
    for instance_id in instance_ids:
        record = records.get(instance_id)
        if not isinstance(record, dict):
            raise ValueError(f"invalid cache record for {instance_id}")
        target_path = _safe_instance_path(root, instance_id)
        expected_relative = f"{instance_id}/behavior_target.json"
        if record.get("path") != expected_relative:
            raise ValueError(
                f"unexpected cache path for {instance_id}: {record.get('path')!r}"
            )
        payload = _load_json(target_path)
        if not isinstance(payload, dict):
            raise ValueError(f"BehaviorTarget must be a JSON object: {target_path}")
        if payload.get("instance_id") != instance_id:
            raise ValueError(
                f"BehaviorTarget instance_id mismatch in {target_path}: "
                f"{payload.get('instance_id')!r}"
            )
        if payload.get("schema_version") != expected_behavior_schema:
            raise ValueError(
                f"BehaviorTarget schema mismatch in {target_path}: "
                f"{payload.get('schema_version')!r}"
            )
        actual_hash = sha256_file(target_path)
        if actual_hash != record.get("sha256"):
            raise ValueError(
                f"BehaviorTarget hash mismatch for {instance_id}: "
                f"actual={actual_hash}, expected={record.get('sha256')!r}"
            )

    provenance = {
        "mode": "frozen_cache",
        "cache_id": str(manifest.get("cache_id") or ""),
        "cache_path": str(root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "behavior_schema_version": expected_behavior_schema,
        "dataset_mode": manifest.get("dataset_mode"),
        "dataset_sha256": actual_dataset_hash,
        "code_retrieval_sha256": manifest.get("code_retrieval_sha256"),
        "test_retrieval_sha256": manifest.get("test_retrieval_sha256"),
        "instance_count": len(instance_ids),
        "source_run_id": manifest.get("source_run_id"),
    }
    if not provenance["cache_id"]:
        raise ValueError("cache manifest has an empty cache_id")
    provenance["source_signature"] = behavior_cache_source_signature(provenance)
    return provenance


def freeze_behavior_target_cache(
    source_dir: str | Path,
    output_dir: str | Path,
    instances_path: str | Path,
    *,
    cache_id: str,
    dataset_mode: str,
    code_retrieval_path: str | Path,
    test_retrieval_path: str | Path,
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy exact BehaviorTarget files and create their immutable manifest."""

    source = Path(source_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty cache directory: {output}")
    ids = _dataset_instance_ids(instances_path)
    output.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, str]] = {}
    for instance_id in ids:
        source_path = _safe_instance_path(source, instance_id)
        if not source_path.is_file():
            raise ValueError(f"source BehaviorTarget is missing: {source_path}")
        payload = _load_json(source_path)
        if not isinstance(payload, dict) or payload.get("instance_id") != instance_id:
            raise ValueError(f"invalid source BehaviorTarget: {source_path}")
        if payload.get("schema_version") != BEHAVIOR_SCHEMA_VERSION:
            raise ValueError(
                f"unexpected source BehaviorTarget schema in {source_path}: "
                f"{payload.get('schema_version')!r}"
            )
        destination = _safe_instance_path(output, instance_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        records[instance_id] = {
            "path": f"{instance_id}/behavior_target.json",
            "sha256": sha256_file(destination),
        }

    metadata = dict(source_metadata or {})
    manifest = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "cache_id": cache_id,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "dataset_mode": dataset_mode,
        "instance_count": len(ids),
        "behavior_schema_version": BEHAVIOR_SCHEMA_VERSION,
        "dataset_sha256": sha256_file(instances_path),
        "code_retrieval_sha256": sha256_file(code_retrieval_path),
        "test_retrieval_sha256": sha256_file(test_retrieval_path),
        **metadata,
        "behavior_targets": records,
    }
    manifest_path = output / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return validate_behavior_target_cache(
        output,
        instances_path,
        dataset_mode=dataset_mode,
        code_retrieval_path=code_retrieval_path,
        test_retrieval_path=test_retrieval_path,
    )

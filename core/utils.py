"""Small utilities shared by BRT6 modules."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def sanitize_instance_id(instance_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", instance_id)


def truncate_text(text: str, max_chars: int) -> str:
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[TRUNCATED]...\n" + text[-(max_chars - half) :]


def ensure_dir(path: str | os.PathLike[str]) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return str(path)


def _jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, Path):
        return str(obj)
    return obj


def safe_json_dump(obj: Any, path: str | os.PathLike[str]) -> None:
    """Atomically replace *path* with a complete JSON document.

    Long-running experiments are often interrupted while writing a checkpoint.
    Writing directly to the destination can leave a truncated but existing JSON
    file, which later resume logic may mistake for a usable artifact.  A sibling
    temporary file plus ``os.replace`` keeps the previous complete checkpoint
    visible until the new document has been written successfully.
    """

    destination = Path(path)
    ensure_dir(destination.parent)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(obj, temporary, ensure_ascii=False, indent=2, default=_jsonable)
            temporary.flush()
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def safe_json_load(path: str | os.PathLike[str]) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def now_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract one JSON object from raw model output."""

    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty response; expected a JSON object")
    candidates: list[str] = [raw]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        variants = [candidate]
        # Models occasionally place Python/regex notation such as ``\x03``
        # or ``\d`` inside a JSON string without escaping the backslash.  The
        # object is otherwise complete, so preserve that notation literally
        # instead of discarding an entire seed.  This fallback runs only after
        # the original text fails strict parsing and does not alter valid JSON
        # escape sequences.
        repaired = re.sub(
            r'\\+(?=[^"\\/bfnrtu])',
            lambda match: (
                match.group(0) + "\\"
                if len(match.group(0)) % 2
                else match.group(0)
            ),
            candidate,
        )
        if repaired != candidate:
            variants.append(repaired)
        for variant in variants:
            try:
                value = json.loads(variant)
                if isinstance(value, dict):
                    return value
                raise ValueError(
                    f"JSON parsed but is {type(value).__name__}, not object"
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            try:
                value = json.JSONDecoder(strict=False).decode(variant)
                if isinstance(value, dict):
                    return value
                raise ValueError(
                    f"JSON parsed but is {type(value).__name__}, not object"
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
    raise ValueError(f"failed to parse JSON object from response: {last_error}")


def clean_code_block(text: str) -> str:
    """Extract Python code from raw model output."""

    raw = (text or "").strip()
    if not raw:
        return ""
    blocks = re.findall(r"```(?:python|py)?\s*(.*?)```", raw, flags=re.S | re.I)
    if blocks:
        return max((b.strip() for b in blocks), key=len)
    # If there is explanatory text, start at the first likely Python statement.
    lines = raw.splitlines()
    for idx, line in enumerate(lines):
        if re.match(r"\s*(import|from|def|class|@|pytestmark\s*=|try:|with\s+)", line):
            return "\n".join(lines[idx:]).strip()
    return raw


def write_text(path: str | os.PathLike[str], text: str) -> None:
    ensure_dir(Path(path).parent)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")


def read_text(path: str | os.PathLike[str]) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

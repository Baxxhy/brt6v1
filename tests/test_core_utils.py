import json
from pathlib import Path

import pytest

from core.utils import extract_json_object, safe_json_dump


def test_extract_json_object_preserves_model_python_escapes() -> None:
    value = extract_json_object(
        r'{"reason":"payload U\x031.0 does not match \d+","decision":"reject"}'
    )

    assert value["reason"] == r"payload U\x031.0 does not match \d+"
    assert value["decision"] == "reject"


def test_extract_json_object_does_not_break_an_already_escaped_backslash() -> None:
    value = extract_json_object(
        r'{"avoid":["literal U\\x031.0"],"reason":"raw U\x031.0"}'
    )

    assert value["avoid"] == [r"literal U\x031.0"]
    assert value["reason"] == r"raw U\x031.0"


def test_extract_json_object_keeps_valid_json_escapes() -> None:
    value = extract_json_object(
        '{"reason":"first\\nsecond","quote":"say \\\"hello\\\""}'
    )

    assert value == {"reason": "first\nsecond", "quote": 'say "hello"'}


def test_safe_json_dump_replaces_complete_document(tmp_path: Path) -> None:
    destination = tmp_path / "checkpoint.json"
    destination.write_text('{"old": true}', encoding="utf-8")

    safe_json_dump({"new": [1, 2, 3]}, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "new": [1, 2, 3]
    }
    assert list(tmp_path.glob(".checkpoint.json.*.tmp")) == []


def test_safe_json_dump_preserves_previous_file_when_serialization_fails(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "checkpoint.json"
    destination.write_text('{"old": true}', encoding="utf-8")

    with pytest.raises((TypeError, ValueError)):
        safe_json_dump({"bad": object()}, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.glob(".checkpoint.json.*.tmp")) == []

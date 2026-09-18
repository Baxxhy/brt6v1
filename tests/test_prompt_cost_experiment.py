from __future__ import annotations

import json

import pytest

from brt6.llm.prompt_cost_experiment import compact_prompt, expand_prompt, merged_request, unpack_merged_response


def test_exact_reconstruction_of_repeated_code_and_unicode():
    code = 'def test_x():\n    value = "字符串 \\ literal"\n    assert value\n' * 30
    prompt = 'Issue: keep exactly.\n' + json.dumps({"current":code, "host":{"seed":code}, "original":code})
    short, stats = compact_prompt(prompt)
    assert len(short) < len(prompt)
    assert stats["shared_strings"] == 1
    assert expand_prompt(short) == prompt


def test_near_duplicates_and_small_repetitions_are_not_removed():
    a = "x" * 500
    original = json.dumps({"a":a, "b":a+"y", "c":"short", "d":"short"})
    short, stats = compact_prompt(original)
    assert short == original
    assert stats["shared_strings"] == 0


def test_does_not_compress_plain_source_or_incomplete_json():
    text = 'foo("' + 'x'*1000 + '")\n'
    original = text * 3 + '{"unfinished": "'+ 'y'*1000
    assert compact_prompt(original)[0] == original


def test_escaped_strings_and_multiple_json_sections_keep_literal_bytes():
    encoded = json.dumps("\u4e2d文\n\"quote\"" * 90, ensure_ascii=True)
    original = '{"a":' + encoded + ', "b":' + encoded + '}\nNext\n[' + encoded + ']'
    assert expand_prompt(compact_prompt(original)[0]) == original


def test_reserved_marker_is_rejected():
    with pytest.raises(ValueError):
        compact_prompt('@@BRT_SHARED_1@@')


def test_merge_removes_old_verdict_but_keeps_plan_inputs():
    user = 'Issue: unchanged\nPrevious Verifier:\n{"reason":"OLD_VERDICT_SENTINEL"}\nDelta history attempted:\n[{"change":"keep history"}]'
    system, merged = merged_request('verification rules', 'current actual log', 'planning rules', user)
    assert 'OLD_VERDICT_SENTINEL' not in merged
    assert 'keep history' in merged
    assert 'current actual log' in merged
    assert 'verification rules' in system and 'planning rules' in system


def test_missing_plan_preserves_verdict_for_original_planner_fallback():
    verdict = {"decision":"repair_oracle", "change":["ground expected value"]}
    assert unpack_merged_response(verdict) == (verdict, None)
    assert unpack_merged_response({"verifier":verdict,"next_delta":None}) == (verdict,None)
    with pytest.raises(ValueError):
        unpack_merged_response({"next_delta":{}})

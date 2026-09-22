"""Quotation formatting must not discard exact, source-scoped evidence."""
import json
from types import SimpleNamespace

import pytest

from brt6.core.schema import BehaviorTarget, CandidateTest, ExecutionResult
from brt6.validation.strict_semantic_verifier import (
    _validated_oracle_evidence,
    verify_strict_semantics,
)


def validate(quote, source='issue', issue='The returned result must preserve the input shape.'):
    return _validated_oracle_evidence(
        [{'source': source, 'quote': quote}], issue_text=issue,
        behavior=BehaviorTarget('example'), source_context='')


@pytest.mark.parametrize('quote', [
    'The returned result must preserve the input shape.',
    '"The returned result must preserve the input shape."',
    "'The returned result must preserve the input shape.'",
    '“The returned result must preserve the input shape.”',
    '‘The returned result must preserve the input shape.’',
    '"`The returned result must preserve the input shape.`"',
])
def test_exact_content_survives_quotation_delimiters(quote):
    assert validate(quote) == [{'source': 'issue', 'quote': quote}]


@pytest.mark.parametrize('quote', [
    '"The returned result must not preserve the input shape."',
    '"The returned result ... input shape."',
    '"The returned result must preserve the input shape.\'',
    '"result"',
])
def test_changed_proposition_mismatched_delimiters_and_short_quote_rejected(quote):
    assert not validate(quote)


def test_quote_does_not_cross_source_boundary():
    assert not validate('"The returned result must preserve the input shape."', source='repository_context')
    assert not validate('"The returned result must preserve the input shape."', source='fixed')


def test_internal_literal_punctuation_is_not_removed():
    assert validate('"The returned string must equal \'abc\'."', issue="The returned string must equal 'abc'.")
    assert not validate('"The returned string must equal abc."', issue="The returned string must equal 'abc'.")


@pytest.mark.parametrize('target_hit,expected', [(True, 'accept'), (False, 'repair_trigger')])
def test_program_verdict_uses_recovered_evidence_without_bypassing_other_gates(tmp_path, target_hit, expected):
    response = dict(target_hit=target_hit, uses_public_behavior=True, oracle_falsifiable=True,
                    oracle_evidence=[dict(source='issue', quote='"The returned result must preserve the input shape."')],
                    post_fix_risk=dict(level='low'))
    llm = SimpleNamespace(chat=lambda *a, **kw: json.dumps(response))
    decision, strict = verify_strict_semantics(
        'The returned result must preserve the input shape.', BehaviorTarget('example'), None,
        CandidateTest('example', code='def test_shape():\n    assert api(x).shape == x.shape\n'),
        ExecutionResult('example', returncode=1, status='ASSERTION_FAIL'), '', llm, str(tmp_path), 0)
    assert strict.oracle_grounded_in_target_evidence
    assert decision.decision == expected

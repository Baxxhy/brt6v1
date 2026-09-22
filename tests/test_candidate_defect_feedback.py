import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from brt6.core.schema import BehaviorTarget, CandidateTest, ExecutionResult
from brt6.validation.strict_semantic_verifier import verify_strict_semantics
from brt6.execution.feedback import _semantic_feedback_payload


def response():
    return {'target_hit': True, 'oracle_evidence': [{'source': 'issue', 'quote': 'Both empty results should be equal.'}],
            'uses_public_behavior': True, 'oracle_falsifiable': True, 'post_fix_risk': {'level': 'low'},
            'candidate_defect': {'kind': 'oracle', 'subject': 'test_artifact', 'code_quote': 'assert a != b',
                                 'evidence_source': 'issue', 'evidence_quote': 'Both empty results should be equal.',
                                 'explanation': 'The comparison asserts the opposite of the supplied contract.',
                                 'repair': 'Compare the two results for equality.'}}


def run(tmp_path, data, enabled):
    llm = SimpleNamespace(chat=lambda *a, **kw: json.dumps(data))
    for d in ('prompts', 'responses'): (tmp_path/d).mkdir(exist_ok=True)
    return verify_strict_semantics('Both empty results should be equal.', BehaviorTarget('x'), None,
        CandidateTest('x', code='def test_x():\n    a, b = api()\n    assert a != b\n'),
        ExecutionResult('x', returncode=1, status='ASSERTION_FAIL'), '', llm, str(tmp_path), 0,
        defect_feedback=enabled)


def test_negative_fact_routes_repair_and_survives_feedback(tmp_path):
    decision, strict = run(tmp_path, response(), True)
    assert decision.decision == 'repair_oracle'
    assert strict.change == ['Compare the two results for equality.']
    payload = _semantic_feedback_payload(decision, strict)
    assert payload['candidate_defect']['code_quote'] == 'assert a != b'
    assert payload['change'] == strict.change
    assert json.loads((tmp_path/'strict_verifier_round_0.json').read_text())['candidate_defect']


@pytest.mark.parametrize('field,value', [
    ('code_quote', 'assert imaginary'), ('evidence_quote', 'an invented specification'),
    ('subject', 'product'), ('evidence_source', 'fixed'), ('repair', '')])
def test_invalid_or_product_claim_does_not_override_acceptance(tmp_path, field, value):
    data = response(); data['candidate_defect'][field] = value
    decision, strict = run(tmp_path, data, True)
    assert decision.decision == 'accept'
    assert strict.candidate_defect == {}


def test_experiment_disabled_preserves_existing_behavior(tmp_path):
    decision, strict = run(tmp_path, response(), False)
    assert decision.decision == 'accept'
    assert strict.candidate_defect == {}
    assert 'Also return candidate_defect' not in (tmp_path/'prompts/strict_verifier_round_0.txt').read_text()


def test_product_gap_alone_does_not_reject(tmp_path):
    data = response(); data['candidate_defect'] = None
    data['semantic_gap'] = 'Product returns incorrect values for the issue trigger.'
    decision, strict = run(tmp_path, data, True)
    assert decision.decision == 'accept'


def test_desired_behavior_in_risk_text_is_not_a_prohibition(tmp_path):
    data = response()
    data['post_fix_risk'] = {
        'level': 'low', 'constraint': 'Compare the two results for equality.',
        'code_quote': 'assert a != b', 'line': 3,
    }
    decision, strict = run(tmp_path, data, True)
    payload = _semantic_feedback_payload(decision, strict)
    assert payload['change'] == ['Compare the two results for equality.']
    assert payload['avoid'] == ['Do not retain the identified test defect at: assert a != b']


@pytest.mark.parametrize('level,expected', [('low', 'accept'), ('high', 'repair_oracle')])
def test_default_route_preserves_diagnosis_without_inverting_it(tmp_path, level, expected):
    data = response()
    data['post_fix_risk'] = {
        'level': level, 'constraint': 'Compare the two results for equality.',
        'code_quote': 'assert a != b', 'line': 3,
    }
    decision, strict = run(tmp_path, data, False)
    assert decision.decision == expected
    payload = _semantic_feedback_payload(decision, strict)
    assert payload['avoid'] == []
    assert payload['located_oracle_risk']['description'] == data['post_fix_risk']['constraint']
    assert payload['located_oracle_risk']['code_quote'] == 'assert a != b'
    if expected == 'repair_oracle':
        assert any('assert a != b' in item for item in payload['change'])
    else:
        assert payload['change'] == []

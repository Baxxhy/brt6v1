import json
from types import SimpleNamespace

import pytest

from brt6.core.schema import BehaviorTarget, CandidateTest, ExecutionResult, RawIssueContext
from brt6.validation.strict_semantic_verifier import _declared_no_exception_witness, verify_strict_semantics


def context(style='standard'):
    behavior = BehaviorTarget('example',
        error_symptom={'text': 'Calling compose raises ValueError for overlapping inputs.'},
        expected_behavior={'text': 'The compose operation should accept overlapping inputs and return their composition.'},
        target_apis=[{'name': 'pkg.compose', 'source_path': 'pkg/ops.py'}])
    candidate = CandidateTest('example', candidate_repo_path='tests/test_generated.py',
                             code='def test_compose():\n    assert compose(overlap) == identity\n')
    standard = ('Traceback (most recent call last):\n'
                '  File "/testbed/tests/test_generated.py", line 2, in test_compose\n'
                '    assert compose(overlap) == identity\n'
                '  File "/testbed/pkg/ops.py", line 30, in compose\n'
                '    raise ValueError("overlap rejected")\n'
                'ValueError: overlap rejected\n')
    short = ('tests/test_generated.py:2: in test_compose\n'
             '    assert compose(overlap) == identity\n'
             'pkg/ops.py:30: in compose\n'
             '    raise ValueError("overlap rejected")\n'
             'E   ValueError: overlap rejected\n')
    return behavior, candidate, ExecutionResult('example', returncode=1, status='ISSUE_ALIGNED_FAIL',
                                              stdout=standard if style == 'standard' else short)


@pytest.mark.parametrize('style', ['standard', 'short'])
def test_declared_contract_uses_runtime_path_without_phrase_matching(style):
    behavior, candidate, execution = context(style)
    assert _declared_no_exception_witness(behavior, candidate, execution, 'NO_EXCEPTION')['target_function'] == 'compose'


@pytest.mark.parametrize('old,new', [
    ('ValueError:', 'TypeError:'),
    ('/pkg/ops.py', '/helpers/ops.py'),
    ('in compose', 'in setup'),
    ('/tests/test_generated.py', '/tests/test_other.py'),
    ('line 2,', 'line 200,'),
])
def test_other_failures_and_invalid_candidate_frames_do_not_establish_target(old, new):
    behavior, candidate, execution = context()
    execution.stdout = execution.stdout.replace(old, new)
    assert not _declared_no_exception_witness(behavior, candidate, execution, 'NO_EXCEPTION')


def test_source_echo_without_frames_and_multiple_failures_are_not_evidence():
    behavior, candidate, execution = context()
    execution.stdout += 'ValueError: another failing test\n'
    assert not _declared_no_exception_witness(behavior, candidate, execution, 'NO_EXCEPTION')
    execution.stdout = 'assert compose(x)  # pkg/ops.py\nValueError: wrong setup\n'
    assert not _declared_no_exception_witness(behavior, candidate, execution, 'NO_EXCEPTION')


def test_setup_timeout_and_other_contracts_are_not_overridden():
    behavior, candidate, execution = context()
    assert not _declared_no_exception_witness(behavior, candidate, execution, 'ASSERT_EXPRESSION')
    assert not _declared_no_exception_witness(RawIssueContext('example', issue_text='The operation should complete.'), candidate, execution, 'NO_EXCEPTION')
    execution.timeout = True
    assert not _declared_no_exception_witness(behavior, candidate, execution, 'NO_EXCEPTION')
    execution.timeout = False
    execution.status = 'SETUP_ERROR'
    assert not _declared_no_exception_witness(behavior, candidate, execution, 'NO_EXCEPTION')


@pytest.mark.parametrize('public,risk,expected', [(True, 'low', 'accept'), (False, 'low', 'repair_trigger'),
                                               (True, 'high', 'repair_oracle')])
def test_other_semantic_gates_remain_active(tmp_path, public, risk, expected):
    behavior, candidate, execution = context()
    issue = 'The compose operation should accept overlapping inputs and return their composition.'
    response = dict(target_hit=False, oracle_kind='NO_EXCEPTION', uses_public_behavior=public,
                    oracle_falsifiable=True, oracle_evidence=[dict(source='issue', quote=issue)],
                    post_fix_risk=dict(level=risk, constraint='Unsupported identity requirement',
                                       code_quote='assert compose(overlap) == identity', line=2))
    llm = SimpleNamespace(chat=lambda *a, **kw: json.dumps(response))
    decision, strict = verify_strict_semantics(issue, behavior, None, candidate, execution, '', llm, str(tmp_path), 0)
    assert decision.decision == expected
    assert strict.target_hit == public

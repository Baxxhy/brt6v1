"""Invalid assertion construction must not be accepted as a product witness."""
import json
from types import SimpleNamespace

import pytest

from brt6.core.schema import BehaviorTarget, CandidateTest, ExecutionResult, RawIssueContext
from brt6.validation.strict_semantic_verifier import _invalid_oracle_construction, verify_strict_semantics


CODE = 'import pytest\ndef test_x():\n    with pytest.warns(None):\n        api()\n'
LOG = '''tests/test_x.py:3: in test_x
    with pytest.warns(None):
/opt/env/site-packages/_pytest/recwarn.py:282: in __init__
    raise TypeError(msg)
E   TypeError: exceptions must be derived from Warning
'''


def detect(code=CODE, log=LOG, behavior=None):
    return _invalid_oracle_construction(
        CandidateTest('test', code=code, candidate_repo_path='tests/test_x.py'),
        ExecutionResult('test', returncode=1, status='UNRELATED_FAIL', stdout=log),
        behavior or BehaviorTarget('test'),
    )


def test_context_constructor_failure():
    assert detect()['api'] == 'pytest.warns'


def test_raises_and_alias_are_supported():
    code = CODE.replace('import pytest', 'from pytest import raises as expected').replace('pytest.warns', 'expected')
    assert detect(code, LOG.replace('recwarn.py', 'python_api.py'))['api'] == 'pytest.raises'


@pytest.mark.parametrize('log', [
    LOG.replace('tests/test_x.py:3:', 'tests/test_x.py:4:'),
    LOG.replace('tests/test_x.py:3:', 'tests/sibling.py:3:'),
    LOG.replace('/_pytest/recwarn.py', '/product/api.py'),
    LOG.replace('TypeError:', 'AssertionError:'),
    LOG + LOG,
    'source excerpt: with pytest.warns(None): TypeError: invalid',
])
def test_unproven_constructor_failure_is_not_rejected(log):
    assert not detect(log=log)


def test_framework_itself_can_be_the_target():
    target = BehaviorTarget('test', target_apis=[{'name': 'pytest.warns'}])
    assert not detect(behavior=target)


def test_raw_issue_fallback_keeps_its_semantic_verifier():
    assert not detect(behavior=RawIssueContext('test', issue_text='pytest warns constructor bug'))


def test_bad_constructor_routes_without_paying_for_a_judge(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('The constructor failure is already explicit in execution.')
    decision, result = verify_strict_semantics(
        'api should not warn', BehaviorTarget('test'), None,
        CandidateTest('test', code=CODE, candidate_repo_path='tests/test_x.py'),
        ExecutionResult('test', returncode=1, status='UNRELATED_FAIL', stdout=LOG),
        '', SimpleNamespace(chat=forbidden), str(tmp_path), 0,
    )
    assert decision.decision == 'repair_oracle'
    assert result.failure_origin == 'oracle_construction'
    assert json.loads((tmp_path/'strict_verifier_round_0.json').read_text())['target_hit'] is False

from unittest.mock import patch
import subprocess

import pytest

from brt6.core.schema import ExecutionResult
from brt6.execution.feedback import _execute_or_reuse_keep
from brt6.runtime.step_journal import StepJournal
from brt6.runtime.official_docker_runtime import OfficialDockerRuntime


def test_keep_reuses_result_but_keeps_journal_alignment(tmp_path):
    args = ('test command', '/repo', 'testbed', 7200, False, None, 'example')
    identity = ('source bytes', *args[:5])
    journal = StepJournal(tmp_path, {'test': 'keep'})
    result = ExecutionResult('example', returncode=0, status='PASS', duration=2199)
    with patch('brt6.execution.feedback.run_command_in_conda', return_value=result) as execute:
        with journal.activate():
            first, reused = _execute_or_reuse_keep(args, keep=False, identity=identity,
                previous_identity=None, previous_execution=None)
            second, reused = _execute_or_reuse_keep(args, keep=True, identity=identity,
                previous_identity=identity, previous_execution=first)
        assert reused and execute.call_count == 1
        assert first.to_dict() == second.to_dict()
        assert second is not first
        assert len(list(journal.directory.glob('step_*.json'))) == 2
        replay = StepJournal(tmp_path, {'test': 'keep'})
        with replay.activate():
            for _ in range(2):
                restored, _ = _execute_or_reuse_keep(args, keep=False, identity=identity,
                    previous_identity=None, previous_execution=None)
                assert restored.to_dict() == first.to_dict()
        assert execute.call_count == 1


@pytest.mark.parametrize('change', ['code', 'command', 'environment', 'not_keep', 'timeout', 'infra'])
def test_changed_or_failed_execution_is_not_reused(change):
    old = ('code', 'command', 'env')
    new = list(old)
    if change in ['code', 'command', 'environment']:
        new[['code', 'command', 'environment'].index(change)] = 'changed'
    prior = ExecutionResult('i', returncode=0, status='PASS')
    if change == 'timeout': prior.timeout = True
    if change == 'infra': prior.status = 'SETUP_ERROR'
    with patch('brt6.execution.feedback.run_command_in_conda', return_value=ExecutionResult('i', returncode=1, status='FAIL')) as run:
        _, reused = _execute_or_reuse_keep((), keep=change != 'not_keep', identity=tuple(new),
            previous_identity=old, previous_execution=prior)
    assert not reused and run.call_count == 1


def test_host_delta_uses_worktree_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv('BRT_WORKTREE_TIMEOUT', '1200')
    runtime = object.__new__(OfficialDockerRuntime)
    def slow_git(command, *, timeout):
        # A repository scan requiring more than the former 120s budget.
        if timeout < 300: raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
    with patch('brt6.runtime.official_docker_runtime._run', side_effect=slow_git) as run:
        assert runtime._host_delta(str(tmp_path)) == ([], [])
        assert run.call_count == 2
        assert all(c.kwargs['timeout'] == 1200 for c in run.call_args_list)

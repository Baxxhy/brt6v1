import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brt6.runtime.infrastructure_errors import InfrastructureUnavailableError, check_execution_infrastructure
from brt6.runtime.official_docker_runtime import OfficialDockerRuntime
from brt6.pipeline.run import _run_one


class InfrastructureCostGuardTests(unittest.TestCase):
    def test_git_reset_failure_stops_before_candidate_execution(self):
        # No live Docker or LLM calls: fail the exact pre-test reset boundary.
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            runtime = object.__new__(OfficialDockerRuntime)
            runtime.output_dir = Path(tmp)
            runtime.instance_id = 'example'
            runtime._exec_lock = threading.Lock()
            runtime._execution_index = 0
            runtime.repo_directory = '/testbed'
            runtime.base_commit = 'base'
            failed = subprocess.CompletedProcess([], 128, '', "fatal: Unable to create '/testbed/.git/index.lock': File exists")
            with patch.object(runtime, '_docker_exec', return_value=failed) as execute, patch.object(runtime, '_host_delta') as delta:
                with self.assertRaises(InfrastructureUnavailableError):
                    runtime.execute('pytest test_brt.py', tmp, 60, None)
                execute.assert_called_once()
                delta.assert_not_called()
            self.assertEqual(json.loads((Path(tmp)/'infrastructure_pause.json').read_text())['status'], 'PAUSED_INFRA')

    def test_semantic_test_failures_are_not_blocked(self):
        for message in ['AssertionError', 'ModuleNotFoundError: missing_helper', "fixture 'x' not found", 'TimeoutError: operation exceeded timeout']:
            check_execution_infrastructure(SimpleNamespace(returncode=1, stdout='', stderr=message))

    def test_cached_git_lock_result_is_blocked(self):
        with self.assertRaises(InfrastructureUnavailableError):
            check_execution_infrastructure(SimpleNamespace(returncode=128, stdout='', stderr="fatal: Unable to create '/testbed/.git/index.lock': File exists"))

    def test_worker_records_pause_instead_of_retrying_other_seeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(output_dir=tmp)
            with patch('brt6.pipeline.run._run_one_impl', side_effect=InfrastructureUnavailableError('reset failed')) as run:
                result = _run_one(args, 'example', {})
            run.assert_called_once()
            self.assertEqual(result['status'], 'PAUSED_INFRA')
            self.assertEqual(json.loads((Path(tmp)/'example/summary.json').read_text())['status'], 'PAUSED_INFRA')

    def test_old_failed_checkpoint_is_executed_again(self):
        from brt6.runtime.step_journal import StepJournal
        with tempfile.TemporaryDirectory() as tmp:
            journal = StepJournal(tmp, {'id': 'example'})
            journal.step('execution', {}, lambda: {'returncode':128, 'stderr':"fatal: Unable to create '/testbed/.git/index.lock': File exists"})
            journal = StepJournal(tmp, {'id': 'example'})
            with patch('builtins.print') as execute:
                result = journal.step('execution', {}, lambda: (execute('execution'), {'returncode':0})[1])
            execute.assert_called_once()
            self.assertEqual(result['returncode'], 0)

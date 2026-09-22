"""Failure feedback must retain captured program output without disabling capture."""
import os
import shlex
import subprocess
import sys

import pytest

from brt6.retrieval.icore_runtime import icore_test_command


@pytest.mark.parametrize('repo', ['pytest-dev/pytest', 'matplotlib/matplotlib', 'mwaskom/seaborn'])
def test_failure_log_includes_program_observation(repo, tmp_path):
    test = tmp_path / 'test_feedback_observation.py'
    marker = 'OBSERVED_PROGRAM_OUTPUT_WITHOUT_REQUIRED_LOGGER'
    test.write_text(f'def test_observation():\n    print({marker!r})\n    assert False\n')
    command = shlex.split(icore_test_command(repo, 'test', str(test), 'test_observation'))
    if command[0] == 'pytest':
        command = [sys.executable, '-m', 'pytest', *command[1:]]
    else:
        command[0] = sys.executable
    assert '--show-capture=all' in command
    assert '-s' not in command  # Keep test capture semantics, expose only failure reports.
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    current = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
    assert current.returncode == 1
    assert 'Captured stdout' in current.stdout
    assert marker in current.stdout.split('Captured stdout', 1)[1]
    legacy = [arg.replace('--show-capture=all', '--show-capture=no') for arg in command]
    previous = subprocess.run(legacy, capture_output=True, text=True, env=env, timeout=30)
    assert previous.returncode == 1
    assert 'Captured stdout' not in previous.stdout

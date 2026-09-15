from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from brt6.execution.executor import run_subprocess_tree
from brt6.runtime.official_docker_runtime import _run


PROJECT_TEST_TMP = Path(__file__).resolve().parents[1] / ".runtime" / "test-tmp"


class ProcessReapingTests(unittest.TestCase):
    def test_background_grandchild_is_stopped_and_reaped(self) -> None:
        PROJECT_TEST_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT_TEST_TMP) as cwd:
            pid_file = os.path.join(cwd, "grandchild.pid")
            completed = run_subprocess_tree(
                [
                    "bash",
                    "-lc",
                    f"sleep 60 </dev/null >/dev/null 2>&1 & echo $! > {pid_file}",
                ],
                cwd,
                5,
            )
            self.assertEqual(completed.returncode, 0)
            grandchild_pid = int(Path(pid_file).read_text(encoding="utf-8"))
            for _ in range(100):
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail(f"background descendant {grandchild_pid} was not reaped")

    def test_timeout_reaps_direct_child(self) -> None:
        created: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def recording_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            created.append(proc)
            return proc

        PROJECT_TEST_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT_TEST_TMP) as cwd, mock.patch(
            "brt6.execution.executor.subprocess.Popen",
            side_effect=recording_popen,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_subprocess_tree(["bash", "-lc", "sleep 60"], cwd, 0.05)

        self.assertEqual(len(created), 1)
        proc = created[0]
        self.assertIsNotNone(proc.returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)

    def test_official_runtime_binary_timeout_reaps_child(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            _run(
                ["bash", "-lc", "cat >/dev/null; sleep 60"],
                timeout=0.05,
                input_bytes=b"payload",
            )


if __name__ == "__main__":
    unittest.main()

"""Regression guard: the former TDD local-Conda path must stay disabled."""

from __future__ import annotations

import argparse
import os
import unittest
from unittest import mock

from brt6.pipeline import run as pipeline_run


class TddOfficialDockerContractTests(unittest.TestCase):
    def _args(self, backend: str) -> argparse.Namespace:
        return argparse.Namespace(
            dataset_mode="tdd",
            runtime_backend=backend,
            no_conda=False,
            official_harness_python="/official/tdd/python",
            swtbench_root="/bench/swt",
            tddbench_root="/bench/tdd",
            conda_env="",
        )

    def test_tdd_generation_uses_official_docker(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            contract = pipeline_run.configure_runtime_contract(
                self._args("official_docker")
            )
        self.assertTrue(contract["docker_harness_invoked"])
        self.assertFalse(contract["host_project_environment_created"])
        self.assertEqual(contract["gold_fields_allowed"], [])

    def test_tdd_local_conda_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "official_docker"):
            pipeline_run.configure_runtime_contract(self._args("local_conda"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path

from brt6.scripts.prepare_tdd_template_environments import (
    failed_environment_names,
    preflight_repair_blockers,
    select_templates_for_repair,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TddTemplateRepairTests(unittest.TestCase):
    def test_selects_only_failed_preflight_groups(self) -> None:
        templates = [
            {"env_name": "ready", "instance_id": "one"},
            {"env_name": "failed", "instance_id": "two"},
        ]
        preflight = {
            "environments": [
                {"canonical_env": "ready", "ready": True},
                {"canonical_env": "failed", "ready": False},
            ]
        }
        self.assertEqual(failed_environment_names(preflight), {"failed"})
        self.assertEqual(
            select_templates_for_repair(templates, preflight),
            [{"env_name": "failed", "instance_id": "two"}],
        )

    def test_unknown_preflight_group_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "absent from the dataset"):
            select_templates_for_repair(
                [{"env_name": "known"}],
                {"environments": [{"canonical_env": "unknown", "ready": False}]},
            )

    def test_non_template_preflight_failures_block_repair(self) -> None:
        blockers = preflight_repair_blockers(
            {
                "framework_is_icore": True,
                "conda_available": True,
                "lock_writable": True,
                "missing_patch": [],
                "missing_environment_setup_commit": [],
                "repositories": [{"repo": "owner/repo", "ready": False}],
            }
        )
        self.assertEqual(
            blockers,
            ["repository cache is incomplete: owner/repo"],
        )

    def test_launcher_never_repairs_host_tdd_templates(self) -> None:
        launcher = (
            PROJECT_ROOT / "scripts/run_p0_simple_llm_selector_full.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn("tdd_local_conda_preflight_before_repair.json", launcher)
        self.assertNotIn("prepare_tdd_template_environments.py", launcher)
        self.assertNotIn("tdd_local_conda_preflight_retry_start", launcher)
        self.assertNotIn("TDD_TEMPLATE_REPAIR_WORKERS", launcher)
        self.assertIn(
            "RUNTIME_BACKEND=${RUNTIME_BACKEND:-official_docker}", launcher
        )
        self.assertIn("host_project_environment_created=false", launcher)

    def test_machine_bootstrap_prepares_tdd_templates(self) -> None:
        bootstrap = (PROJECT_ROOT / "scripts/bootstrap_machine.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "cm-super-minimal curl dvipng gdal-bin", bootstrap
        )
        self.assertIn("libgdal-dev libgeos-dev", bootstrap)
        self.assertIn("texlive-latex-base", bootstrap)
        self.assertIn("texlive-fonts-recommended", bootstrap)
        self.assertIn("texlive-latex-extra", bootstrap)
        self.assertIn("prepare_tdd_template_environments.py", bootstrap)
        self.assertIn("preflight_tdd_local_conda.py", bootstrap)
        self.assertIn("--template-workers", bootstrap)


if __name__ == "__main__":
    unittest.main()

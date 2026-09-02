from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_formal_eval_after_generation.py"
SPEC = importlib.util.spec_from_file_location("run_formal_eval_after_generation", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
select_rows = MODULE.select_rows


class FormalEvalSubsetTests(unittest.TestCase):
    def test_select_rows_preserves_requested_order_and_exact_denominator(self) -> None:
        rows = [
            {"instance_id": "repo__issue-1"},
            {"instance_id": "repo__issue-2"},
            {"instance_id": "repo__issue-3"},
        ]

        selected = select_rows(rows, "repo__issue-3,repo__issue-1")

        self.assertEqual(
            [row["instance_id"] for row in selected],
            ["repo__issue-3", "repo__issue-1"],
        )

    def test_select_rows_rejects_unknown_instance(self) -> None:
        with self.assertRaisesRegex(ValueError, "absent from the dataset"):
            select_rows([{"instance_id": "repo__issue-1"}], "repo__issue-2")

    def test_select_rows_rejects_duplicates(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            select_rows(
                [{"instance_id": "repo__issue-1"}],
                "repo__issue-1,repo__issue-1",
            )


if __name__ == "__main__":
    unittest.main()

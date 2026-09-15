from __future__ import annotations

import unittest

from brt6.core.ablation import AblationConfig, behavior_prompt_payload
from brt6.core.behavior_evidence import behavior_prompt_view
from brt6.core.schema import BehaviorTarget


class PromptCompactionTests(unittest.TestCase):
    def test_raw_evidence_is_persisted_but_not_duplicated_in_prompts(self) -> None:
        target = BehaviorTarget(
            "demo__repo-1",
            expected_behavior={"text": "returns the public result"},
            raw={"expected_behavior": {"text": "returns the public result"}},
        )

        self.assertIn("raw", target.to_dict())
        self.assertNotIn("raw", behavior_prompt_view(target))
        self.assertNotIn("raw", behavior_prompt_payload(target, AblationConfig()))
        self.assertEqual(
            behavior_prompt_view(target)["oracle"]["expected_behavior"],
            target.expected_behavior,
        )


if __name__ == "__main__":
    unittest.main()

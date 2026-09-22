import copy
import unittest
from brt6.core.schema import BehaviorTarget
from brt6.issue.bounded_target_fallback import apply_alternative


class AlternativeTest(unittest.TestCase):
    def setUp(self):
        self.base = BehaviorTarget("example", trigger_condition={"text": "old trigger"},
                                   expected_behavior={"text": "preserve required result"})
        self.sources = {"issue": "Explicit issue trigger and expected result", "test": "a passing test"}
        self.proposal = {"abstain": False, "changed_slots": ["TRIGGER"],
                         "evidence_bindings": [{"slot": "TRIGGER", "source_ref": "issue", "quote": "issue trigger"}],
                         "overrides": {"trigger_condition": {"text": "new trigger"}}}

    def test_bounded_change_does_not_mutate_primary_or_oracle(self):
        result = apply_alternative(self.base, self.proposal, self.sources)
        self.assertEqual(self.base.trigger_condition["text"], "old trigger")
        self.assertEqual(result.expected_behavior, self.base.expected_behavior)
        self.assertEqual(result.trigger_condition["text"], "new trigger")

    def test_fabricated_citation_rejected(self):
        self.proposal["evidence_bindings"][0]["quote"] = "invented evidence"
        with self.assertRaises(ValueError):
            apply_alternative(self.base, self.proposal, self.sources)

    def test_cross_slot_change_rejected(self):
        self.proposal["overrides"]["expected_behavior"] = {"text": "opposite"}
        with self.assertRaises(ValueError):
            apply_alternative(self.base, self.proposal, self.sources)

    def test_oracle_cannot_use_only_test_evidence(self):
        p = {"abstain": False, "changed_slots": ["ORACLE"],
             "overrides": {"expected_behavior": {"text": "new expected"}},
             "evidence_bindings": [{"slot": "ORACLE", "source_ref": "test", "quote": "passing test"}]}
        with self.assertRaises(ValueError):
            apply_alternative(self.base, p, self.sources)

    def test_explicit_abstention(self):
        self.assertIsNone(apply_alternative(self.base, {"abstain": True}, self.sources))

    def test_nested_slot_overrides_are_normalized(self):
        p = copy.deepcopy(self.proposal)
        p["overrides"] = {"TRIGGER": p["overrides"]}
        result = apply_alternative(self.base, p, self.sources)
        self.assertEqual(result.trigger_condition["text"], "new trigger")

    def test_nested_cross_slot_override_is_rejected(self):
        p = copy.deepcopy(self.proposal)
        p["overrides"] = {
            "TRIGGER": {"expected_behavior": {"text": "wrong slot"}}
        }
        with self.assertRaisesRegex(ValueError, "outside declared slot"):
            apply_alternative(self.base, p, self.sources)

    def test_markdown_only_quote_difference_is_accepted(self):
        self.sources["issue"] = "The `issue trigger` must hold."
        result = apply_alternative(self.base, self.proposal, self.sources)
        self.assertEqual(result.trigger_condition["text"], "new trigger")


if __name__ == "__main__":
    unittest.main()

import copy
import os
import unittest
from unittest.mock import patch

from brt6.core.schema import BehaviorTarget
from brt6.issue.feedback_target_revision import apply_revision, eligibility


class TargetRevisionTests(unittest.TestCase):
    def setUp(self):
        self.target = BehaviorTarget("x", trigger_condition={"text": "two objects", "confidence": "medium"},
                                     uncertainties=["multiplicity unclear"],
                                     expected_behavior={"text": "no duplicate choices"})
        self.branches = {f"seed_{i}": {"status": "TRIGGER_UNRESOLVED", "feedback": "no relational join"} for i in range(3)}
        self.sources = {"code": "one object matches multiple related rows", "issue": "no duplicate choices"}
        self.proposal = {"action": "REVISE_TARGET", "uncertainty_index": 0,
                         "field": "trigger_condition", "before": copy.deepcopy(self.target.trigger_condition),
                         "after": {"text": "one object matches multiple rows", "confidence": "medium"},
                         "branch_evidence": [{"seed_id": s, "quote": "no relational join", "explanation": "missing multiplicity"} for s in self.branches],
                         "repository_evidence": {"source_ref": "code", "quote": "matches multiple related rows", "explanation": "join multiplicity"}}

    def test_revision_preserves_fixed_target_and_original(self):
        revised = apply_revision(self.target, self.proposal, self.sources, self.branches)
        self.assertEqual(self.target.trigger_condition["text"], "two objects")
        self.assertEqual(revised.expected_behavior, self.target.expected_behavior)
        self.assertEqual(revised.uncertainties, self.target.uncertainties)
        self.assertNotEqual(revised.trigger_condition, self.target.trigger_condition)

    def test_no_uncertainty_or_infrastructure_does_not_trigger(self):
        self.target.uncertainties = []
        self.assertEqual(eligibility(self.target, self.branches), "NO_RECORDED_UNCERTAINTY")
        self.target.uncertainties = ["x"]
        self.branches["seed_1"]["status"] = "SETUP_ERROR"
        with self.assertRaises(ValueError):
            apply_revision(self.target, self.proposal, self.sources, self.branches)

    def test_fabricated_evidence_and_missing_branch_rejected(self):
        for field in ("repository_evidence", "branch_evidence"):
            proposal = copy.deepcopy(self.proposal)
            if field == "repository_evidence":
                proposal[field]["quote"] = "invented quote"
            else:
                proposal[field].pop()
            with self.assertRaises(ValueError):
                apply_revision(self.target, proposal, self.sources, self.branches)

    def test_expected_behavior_cannot_be_overridden(self):
        self.proposal["field"] = "expected_behavior"
        with self.assertRaises(ValueError):
            apply_revision(self.target, self.proposal, self.sources, self.branches)

    def test_high_confidence_entry_is_locked(self):
        self.target.trigger_condition["confidence"] = "high"
        self.proposal["before"] = copy.deepcopy(self.target.trigger_condition)
        with self.assertRaises(ValueError):
            apply_revision(self.target, self.proposal, self.sources, self.branches)

    def test_keep_does_not_change_target(self):
        self.assertIsNone(apply_revision(self.target, {"action": "KEEP_TARGET"}, self.sources, self.branches))

    def test_standalone_revision_disables_only_optional_direct_fallback(self):
        from brt6.core.ablation import AblationConfig
        from brt6.execution.feedback import _should_run_direct_fallback
        with patch.dict(os.environ, {"BRT_DISABLE_DIRECT_FALLBACK": "1"}):
            self.assertFalse(_should_run_direct_fallback(AblationConfig(), []))
        with patch.dict(os.environ, {"BRT_DISABLE_DIRECT_FALLBACK": "0"}):
            self.assertTrue(_should_run_direct_fallback(AblationConfig(), []))


if __name__ == "__main__":
    unittest.main()

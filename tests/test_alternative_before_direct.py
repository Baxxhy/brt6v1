import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[1]/'scripts/run_feedback_target_revision.py'
spec=importlib.util.spec_from_file_location('target_revision_runner', path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class AlternativeBeforeDirectTests(unittest.TestCase):
    def test_alternative_precedes_even_accepted_direct_fallback(self):
        self.assertTrue(m.include_instance({'direct_fallback_attempted':True}, {'strict_accepted':True}, 'alternative_before_direct'))

    def test_primary_success_never_reprocessed(self):
        self.assertFalse(m.include_instance({'direct_fallback_attempted':False}, {'strict_accepted':True}, 'alternative_before_direct'))

    def test_old_revision_mode_unchanged(self):
        self.assertFalse(m.include_instance({'direct_fallback_attempted':True}, {'strict_accepted':True}, 'uncertainty_revision'))
        self.assertTrue(m.include_instance({}, {'strict_accepted':False}, 'uncertainty_revision'))

    def test_text_shorthand_preserves_proposed_text_and_uses_new_citation(self):
        from brt6.core.schema import BehaviorTarget
        from brt6.issue.bounded_target_fallback import apply_alternative
        target=BehaviorTarget('x',expected_behavior={'text':'old','evidence':['old evidence'],'confidence':'high'})
        proposal={'abstain':False,'changed_slots':['ORACLE'],'overrides':{'expected_behavior':'new'},
                  'evidence_bindings':[{'slot':'ORACLE','source_ref':'issue','quote':'new'}]}
        result=apply_alternative(target,proposal,{'issue':'new behavior'})
        self.assertEqual(result.expected_behavior['text'],'new')
        self.assertEqual(result.expected_behavior['evidence'],['issue: new'])
        self.assertEqual(target.expected_behavior['text'],'old')

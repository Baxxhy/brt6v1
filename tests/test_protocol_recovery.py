from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from brt6.context.protocol_recovery import recover_test_protocol
from brt6.core.schema import BehaviorTarget, RetrievedTest


class ProtocolRecoveryTests(unittest.TestCase):
    def test_recovers_module_setup_used_by_class_attributes(self) -> None:
        source = """\
from django.contrib import admin
from django.test import SimpleTestCase

site = admin.AdminSite(name='custom')
site.register(object)

class ViewTests(SimpleTestCase):
    as_view_args = {'admin_site': site}

    def test_view(self):
        self.assertIs(self.as_view_args['admin_site'], site)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tests" / "views" / "tests.py"
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding="utf-8")
            protocol = recover_test_protocol(
                "django__django-14752",
                RetrievedTest(
                    "django__django-14752",
                    name="ViewTests.test_view",
                    file="tests/views/tests.py",
                    code_content=source,
                ),
                tmp,
                BehaviorTarget("django__django-14752"),
                [],
                "django/django",
                "4.0",
            )

        self.assertIn("site = admin.AdminSite", protocol.module_context[0])
        self.assertTrue(any("site.register" in item for item in protocol.module_context))

    def test_recovers_referenced_class_helpers_recursively(self) -> None:
        source = """\
from django.test import SimpleTestCase

class DispatcherTests(SimpleTestCase):
    def _assert_receiver_count(self, signal):
        self.assertEqual(len(signal.receivers), 0)

    def assertTestIsClean(self, signal):
        self._assert_receiver_count(signal)

    def unrelated_helper(self):
        raise AssertionError('must not be copied')

    def test_send_robust_fail(self):
        signal = object()
        self.assertTestIsClean(signal)
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tests" / "dispatch" / "tests.py"
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding="utf-8")
            protocol = recover_test_protocol(
                "django__django-13768",
                RetrievedTest(
                    "django__django-13768",
                    name="DispatcherTests.test_send_robust_fail",
                    file="tests/dispatch/tests.py",
                    code_content=source,
                ),
                tmp,
                BehaviorTarget("django__django-13768"),
                [],
                "django/django",
                "3.2",
            )

        helpers = {item["name"]: item for item in protocol.local_helpers}
        self.assertEqual(
            set(helpers), {"assertTestIsClean", "_assert_receiver_count"}
        )
        self.assertIn("def assertTestIsClean", helpers["assertTestIsClean"]["code"])
        self.assertNotIn("unrelated_helper", str(protocol.local_helpers))


if __name__ == "__main__":
    unittest.main()

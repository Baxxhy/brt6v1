from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from brt6.context.protocol_recovery import audit_recovered_protocol, recover_test_protocol
from brt6.core.schema import BehaviorTarget, ProtocolRecovery, RetrievedTest
from brt6.llm.errors import LLMUnavailableError


class ProtocolRecoveryTests(unittest.TestCase):
    def test_real_prompt_reaches_model_and_preserves_extracted_protocol(self) -> None:
        protocol = ProtocolRecovery(
            "demo__repo-1",
            test_framework="pytest",
            test_file="tests/test_example.py",
            test_command="pytest tests/test_example.py::test_example",
            imports=["import pytest"],
            fixtures=["tmp_path"],
            runner_hints=["Run only the selected test."],
            protocol_risks=["Existing risk."],
        )
        original = protocol.to_dict()
        seed = RetrievedTest(
            protocol.instance_id,
            name="test_example",
            file=protocol.test_file,
            code_content="def test_example():\n    assert {'value': 1}['value'] == 1\n",
        )
        response = json.dumps({
            "test_framework": "unittest",
            "runner_hints": ["Run only the selected test.", "Additional hint."],
            "protocol_risks": ["Existing risk.", "Additional risk."],
            "fixtures": ["invented_fixture"],
            "test_command": "invented command",
        })
        client = Mock()
        client.chat.return_value = response
        root = Path(__file__).resolve().parents[1] / ".runtime" / "offline_tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            audited = audit_recovered_protocol(
                protocol, BehaviorTarget(protocol.instance_id), seed, client, tmp
            )
            client.chat.assert_called_once()
            system, prompt = client.chat.call_args.args
            self.assertIn(seed.code_content, prompt)
            self.assertIn(json.dumps(original, ensure_ascii=False), prompt)
            self.assertIn(
                '{"test_framework":"pytest|unittest|django|unknown",'
                '"runner_hints":[],"protocol_risks":[]}', prompt
            )
            self.assertEqual(
                (Path(tmp) / "prompts/protocol_recovery_prompt.txt").read_text(),
                system + "\n\n" + prompt,
            )
            self.assertEqual(
                (Path(tmp) / "responses/protocol_recovery_response.txt").read_text(),
                response,
            )
        expected = dict(original)
        expected.update(
            test_framework="unittest",
            runner_hints=["Run only the selected test.", "Additional hint."],
            protocol_risks=["Existing risk.", "Additional risk."],
        )
        self.assertEqual(audited.to_dict(), expected)

    def test_protocol_audit_propagates_api_pause(self) -> None:
        client = Mock()
        client.chat.side_effect = LLMUnavailableError("No usable API endpoint remains")
        root = Path(__file__).resolve().parents[1] / ".runtime" / "offline_tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            with self.assertRaises(LLMUnavailableError):
                audit_recovered_protocol(
                    ProtocolRecovery("demo__repo-1"),
                    BehaviorTarget("demo__repo-1"),
                    RetrievedTest("demo__repo-1", code_content="def test_example(): pass"),
                    client, tmp,
                )
            client.chat.assert_called_once()
            self.assertFalse((Path(tmp) / "responses/protocol_recovery_response.txt").exists())

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

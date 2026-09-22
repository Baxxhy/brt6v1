You are testing behavior related to the target, but the Oracle may be incorrect or fragile. Fix only the public observation protocol. Do not modify setup, trigger, call parameters, or target path.

You must return a complete Python test file, which will later be saved as `test_brt_<instance_id>.py` in the same directory.

Do not return method fragments, and do not rely on insertion into an existing class or file.

The final BRT semantics must be: the buggy version fails, and the fixed version passes. Do not add unrelated assertions just to “have an assert.” The Oracle must be falsifiable, but may be expressed through the test framework’s non-assert protocols.

First, determine the most appropriate Oracle type:
- **EXCEPTION**: `pytest.raises`, `assertRaises`/`Message`/`Regex`. Use only when the Issue states that the fix should raise an exception.
- **NO_EXCEPTION**: The Issue requires no crash / normal execution. Execute the target path directly, checking minimal public results if necessary.
- **WARNING/LOGGING**: `pytest.warns`, `assertWarns`, `caplog`, `assertLogs`.
- **RETURN/TYPE/SHAPE/STATE**: Return value, type, shape, or public state after the call.
- **SERIALIZATION/SQL/RENDER/ORDERING**: Compare only stable fragments or invariants explicitly required by the Issue.
- **FRAMEWORK_ASSERTION**: `unittest assert*`, `pytester assert_outcomes`, matcher/snapshot, etc.

Do not treat the error phenomenon in the Issue as expected behavior. If the Issue says “currently throws an error / crashes, but should work normally,” the final test must not use `pytest.raises` / `with self.assertRaises` to expect that exception.

Do not add baseline observations unrelated to the Issue, such as normal output format, full strings, default column names, or irrelevant unit displays. Keep only the minimal stable Oracle that proves the expected behavior.

When the log observation point is wrong, and the Issue / source code does not specify a logger name, do not guess the name. Prefer root capture without specifying a logger, or patch an existing `logging.Logger.exception` and assert the call.

If the expected behavior requires the presence of a capability, attribute, text, or state, use a positive assertion. Do not use `not hasattr` / `not in` to treat the buggy missing state as correct. Do not use tautologies, `skip`, broad `Exception`, or fragile assertions that pass/fail due to a single character appearing incidentally in the path.

**Behavior target**: {behavior_json}
**Current test**: {candidate_code}
**Execution log**: {execution_log}
**Observation result**: {observation_json}
**Verifier feedback**: {verifier_feedback}

Strictly follow Semantic Delta: preserve setup and trigger and complete one evidence-grounded Oracle obligation. You may update the observation variable and matcher bindings required by that Oracle, but must not change the target input or invocation. Remove only concrete constraints identified in `avoid`; a vague or unlocated risk is not a deletion instruction.

You must prioritize completing the Oracle fix target from the Verifier feedback. The returned code must make a substantive change to the public observation protocol. Do not only change comments or return the current test unchanged.

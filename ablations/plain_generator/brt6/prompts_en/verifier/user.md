Based on the execution result, determine the next step. Output fields: `decision`, `reason`, `focus`, `next_action`.

`decision` must be one of: `accept`, `repair_setup`, `repair_trigger`, `repair_oracle`, `reject`.

`accept` requires all of the following:
1. The test actually executes the target API, input, and call path described in the Issue.
2. The failure symptom matches the `error_symptom`, not just a similar keyword in the log.
3. The assertion expresses the `expected_behavior` and naturally fails on the buggy version.
4. The failure is not caused by an incorrect format name, wrong nodeid, missing dependency, invalid fixture, test exception, or unrelated extra assertion.
5. If the Issue states "currently throws an exception but should not after fix," the test must not use `pytest.raises`/`assertRaises` to treat that buggy exception as expected behavior.
6. The test has a single entry point, with no skip, tautological assertion, swallowed exception, or mock of the target API.
7. `reason` must not simultaneously include negative conclusions such as "not triggered, test setup issue, environment issue, wrong assertion direction, unrelated to Issue"; the presence of any such item prohibits `accept`.

Selection rules:
- If the path, input, parameter, or call method is incorrect, or the test PASSes: `repair_trigger`.
  - When the test PASSes, `reason` must specifically point out what input, object state, signal/mock/config, call order, or internal path condition is missing between the candidate test and the `trigger_condition`/`mutation_hints`.
  - `next_action` must specify the exact test pattern to delete or replace in the next round, not just "strengthen trigger."
- For Issues of type "missing check / missing warning / missing state update / silent acceptance," the absence of an exception in the buggy version is typically the `error_symptom`. Do not suggest blindly searching for exceptions between `__init__`/`clean`/`save`; instead, identify the relevant `check()`/`validate()`/`warning`/state API from the source code and suggest asserting evidence of the `expected_behavior`.
- Only suggest `assertRaises`/`pytest.raises` when the `expected_behavior` explicitly requires an exception.
- For Issues of type "missing log," must suggest using `assertLogs`/`caplog` to capture log evidence; do not suggest patching a `logger` or `_logger` attribute that does not exist in the buggy source code.
- If the target API is reached but the assertion direction is reversed, too strong, or checks unrelated output: `repair_oracle`.
- If the issue is with import/fixture/collection/syntax/runner: `repair_setup`.
- Only `accept` when both the failure path and the oracle align with the Issue.

Issue: {issue_text}
Behavior target: {behavior_json}
Verified test context: {host_context_json}
Relevant buggy source code: {source_context}
Test code: {candidate_code}
Execution result: {execution_json}

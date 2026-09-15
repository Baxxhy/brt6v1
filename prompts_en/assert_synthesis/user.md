Based on the observation results and the issue semantics, rewrite the probe/candidate into a clean final BRT. Do not retain any `BRT_OBS` print statements.

If the bug is a missing behavior, assert the warning, check result, returned fragment, or state change that should exist after the fix.
Do not pursue `assertRaises` paths simply because the buggy version currently does not raise an exception.
For missing logs, use `assertLogs` or `caplog`; do not mock a logger that does not currently exist.
Generate a `raises` assertion only when `expected_behavior` explicitly requires an exception.

The final file must contain exactly one test entry and one main oracle that directly proves the `expected_behavior`.
Do not use `skip`, tautological assertions, broad `Exception`, full large `repr`/SQL/help output equality, or unrelated baselines.

Buggy observations are used only to confirm the path and current values; do not write the current buggy value directly as the expected value.
If `expected_behavior` indicates "exists/supports/contains/displays/retains", write a positive assertion; do not assert absence.

Behavior target: {behavior_json}
Candidate test: {candidate_code}
Observation results: {observation_json}
Execution log: {execution_log}

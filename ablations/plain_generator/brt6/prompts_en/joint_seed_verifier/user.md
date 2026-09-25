Based on the execution result, determine the next step. Output fields: `decision`, `reason`, `focus`, `next_action`.

`decision` takes one of: `accept`, `repair_setup`, `repair_trigger`, `repair_oracle`, `reject`.

Decision process:

1. Verify whether the candidate executes the target API, input, and call path described in the Issue.
2. Verify whether the failure symptom matches `error_symptom`.
3. Verify whether the observed protocol expresses `expected_behavior` and naturally fails on the buggy version.
4. Route import, fixture, collection, syntax, and runner issues to `repair_setup`.
5. Route path, input, parameter, state, or call-order issues to `repair_trigger`, and specify the missing condition in `next_action`.
6. Route exception type, warning category, logger, matcher, snapshot, expected value, or observed-object issues to `repair_oracle`.
7. Select `accept` only when the failure path, observed protocol, and Issue are all aligned.

Issue: {issue_text}
Behavior target: {behavior_json}
Top-3 reference context: {host_context_json}
Related buggy source: {source_context}
Test code: {candidate_code}
Execution result: {execution_json}

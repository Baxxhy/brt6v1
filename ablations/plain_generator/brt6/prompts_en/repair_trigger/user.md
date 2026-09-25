The current test does not trigger the Issue-related path or fails for an unrelated reason. Keep the setup and the full Oracle contract; only adjust inputs, parameters, state, mocks, configuration, call chain, or boundary conditions. The Oracle contract includes bare `assert`, `unittest assert*`, exception/warning/log context, `NO_EXCEPTION`, state/output observation, and matcher/snapshot.

You must return a complete Python test file; it will be saved as `test_brt_<instance_id>.py` in the same directory. Do not return method fragments, and do not rely on insertion into an existing class or file.

Behavior target: {behavior_json}
HostContext: {host_context_json}
Similar test seed: {seed_test_code}
Relevant source code: {code_context}
Current test: {candidate_code}
Execution log: {execution_log}
Verifier feedback: {verifier_feedback}

Strictly follow the Semantic Delta: keep `preserve`; in this round, only execute the single Trigger change specified by `change`, and avoid `avoid`; leave other differences for subsequent rounds. If `last_transition=STAGNANT`, you must switch to a different operator; if `last_transition=REGRESSED`, base your work on the restored parent candidate.

Before fixing, verify the following:

1. Does the test actually call an API from `target_apis`?
2. Does it use the input, boundary value, configuration, operator, or call order explicitly given by the Issue?
3. Does it retain object construction and call chains from the similar test that already run successfully?
4. If the test passes, you must modify the triggering input or state; do not merely add assertions unrelated to the Issue.
5. Keep only one stable oracle directly corresponding to `expected_behavior`. If the execution log shows that a baseline/control assertion fails before the target call and prevents the Issue path from executing, you may delete that blocking control assertion. You must keep the Oracle corresponding to the Issue's target call exactly as is, without rewriting its expected value, exception/warning type, matcher, or observed object.
6. Verifier feedback is the result of the previous semantic verification round; you must execute every `reason` and `next_action`. If the feedback indicates that an input, parameter, string, operator, or call order is imprecise, you must delete the old pattern and replace it with the target pattern specified by the Issue/BehaviorTarget; do not return the current test unchanged.
7. The Verifier's `next_action` is a diagnostic suggestion, not a fact above the source code. If the suggestion conflicts with a lifecycle API, check mechanism, or call pattern that clearly exists in the relevant source code, defer to the source code and the already-passing similar test.
8. If `expected_behavior` is "new check / early check / report configuration error" and the relevant source code exposes a `check()` or similar entry point, call that entry point directly and check its stable return evidence. Do not assume without basis that the constructor or `clean()`/`save()` will raise an exception. Environment fixes may only add model bindings, field `name`, `app_label`, and other runtime metadata; do not delete `check()` or replace it with ordinary value validation. For a standalone `Field`, you may use `set_attributes_from_name()`; prefer to reuse a real model from `HostContext` and obtain the field via `Model._meta.get_field()`.
9. For missing behavior, the fix goal is to add a positive evidence assertion aligned with `expected_behavior`, so that the buggy code fails the assertion due to missing evidence; do not continue searching for an exception that the buggy code already implements.
10. For missing logs, use `assertLogs`/`caplog` to observe the log that should appear after the fix; do not patch a `logger` attribute that does not exist in the buggy source code.
11. Do not use `skip`/early `return`, tautological assertions, exception swallowing, or mock/patch of the target API to bypass the real path.
12. The literal input, parameters, operator, and call order from the Issue's MWE are path constraints; when fixing, do not simplify them to an alternative case that only covers the normal path.

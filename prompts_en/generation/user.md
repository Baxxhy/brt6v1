Based on similarity testing, perform minimal mutation to generate 1 BRT.

Raw Issue (lossless source of facts):
{issue_text}

Evidence priority: inputs, calls, symptoms, and expected behavior stated
explicitly in the raw Issue are hard constraints. The BehaviorTarget organizes
those facts and adds repository evidence, but low-confidence fields and
uncertainties are hypotheses only. They must not override, narrow, or rewrite
an explicit Issue fact.

Instance: {instance_id}
Test function name must be: test_brt_{safe_instance_id}
Placement strategy: Generate a complete new Python test file, which will be saved as test_brt_{safe_instance_id}.py in the same directory as the most similar test.
Do not output a method fragment that needs to be inserted into an existing class or file.
Do not output bare indented code.
If a class wrapper is needed, fully define the class in the new file and include necessary imports.
The complete file must be collectable by pytest/Django/Sympy test commands as an independent .py file; it must not depend on names, models, fixture helpers, global variables, or classes already imported in the similar test file, unless you explicitly import or define them in the new file.
If reusing TestCase, fixture, helper, model, decorator, pytestmark, or assertion style from the similar test, include all required imports/decorators/setup in the new file.
If HostContext.setup_context contains class-level attributes (e.g., CHECKER_CLASS, databases, app_label, configuration constants), preserve their exact names and values; do not guess alternative classes based on the test name.
Do not introduce third-party modules not supported by the similar test, the complete host file, related source code, or the Python standard library.
Must follow the actual runner in HostContext: if the runner is SymPy bin/test, do not default to importing pytest or using pytest fixtures; if the runner is Django runtests, use the existing Django TestCase and test app conventions.
Test assertions must express expected_behavior, not expect a buggy error_symptom to continue.
The complete file must contain only one collectable test function or test method; do not generate baselines, control groups, backup candidates, or multiple test entries, as the evaluation only executes this single BRT.
Do not use pytest.skip, skipIf, skipUnless, or early return when platform conditions are not met; the test must actually execute.
Do not use assert True, A or not A, x == x or x != x, or other tautological assertions.
Do not use broad try/except to swallow exceptions; do not mock/patch the target API or the internal behavior that the Issue intends to observe.
If the Issue provides MWE, literal input, parameters, operators, or call order, preserve these path anchors exactly; do not replace them with simpler inputs or APIs that do not trigger the defect just to make the test runnable.
If the defect is "missing check, missing warning, missing state update, or silently accepting incorrect configuration," do not look for an exception path that already exists in the buggy version. Instead, call the project's real check/validation/state transition API and assert the stable evidence that should appear after the fix; the buggy version will naturally fail the assertion due to missing evidence.
If the defect is "missing log," use assertLogs/caplog to observe logs; only bind a logger name if the Issue or related source code explicitly provides it. When the name is uncertain, use root capture without specifying a logger, or patch an existing public logging method like logging.Logger.exception; do not guess the logger name from the module file path, and do not patch a logger attribute that does not yet exist in the buggy source code.
Use assertRaises/pytest.raises only when expected_behavior explicitly requires an exception.
BehaviorTarget.trigger.safety_constraints are hard constraints, with higher priority than mutation_hints.
If the Issue explicitly describes input as acceptable, parseable, or successful, do not reinterpret it as invalid input, and do not place it inside assertRaises/pytest.raises. If the goal is to verify an error message, use the truly invalid input from the iCoRe seed, and only change the oracle to the stable message fragment after the fix.
Behavior target: {behavior_json}
HostContext: {host_context_json}
Related source code: {code_context}
Current test code (this round's only parent candidate): {seed_test_code}
Previous round feedback: {feedback}

If a Semantic Delta is provided, implement only one change described in its `change`. CONTEXT only changes preconditions or input, INTERACTION only changes calls/order/state transitions, OBSERVATION only changes public behavior assertions supported by the Issue. `preserve` is the default item to keep; if an upstream change invalidates a downstream one, do not fix it in this round—leave it for the next round's feedback. Do not regenerate a different route from the seed or Issue as a whole.

Output the modified complete Python file. Do not use bare assert False, no markdown, no explanation.

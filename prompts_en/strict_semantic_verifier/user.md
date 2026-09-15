Determine whether the current test failure on the buggy version truly expresses the expected behavior described in the Issue.

Issue: {issue_text}
BehaviorTarget: {behavior_json}
ProtocolRecovery: {protocol_json}
Test code: {candidate_code}
Command: {command}
Execution status: {execution_status}
stdout/stderr: {execution_log}
Relevant source code: {source_context}

Based on the full Issue, candidate test, actual buggy execution log, and relevant source code, evaluate the semantic `target_hit` and Oracle contract.

`target_hit` indicates whether the test's invocation and failure path express the Issue, not whether a particular API name appears in the code.

An Oracle does not require a bare `assert`. The following are all acceptable falsifiable Oracles: unittest `assert*`, `pytest.raises`/`assertRaises`, `pytest.warns`/`assertWarns`, `caplog`/`assertLogs`, return value/type/shape, public state changes, serialization/SQL/rendering/ordering, `pytester`/matcher/snapshot, and direct execution when the Issue explicitly requires "no longer raises an exception."

`accept` requires all of the following: buggy fails; failure is not environment/syntax/collection/timeout; failure aligns with the symptom; Oracle originates from the Issue; checks public behavior; the Oracle protocol can genuinely fail when behavior is wrong.

When the buggy execution passes, do not assume a missed Trigger. Compare the current call path with the Oracle. If the call/state path has not yet expressed the Issue, output `repair_trigger`. If the path is reasonable but the Oracle is insufficient to distinguish buggy behavior, output `repair_oracle`.

Also output the next round of Semantic Delta Diagnosis. `preserve`/`change`/`avoid` must be based on the current code and real logs. `post_fix_failure_risk` answers: assuming the fix described in the Issue has been implemented, would the current test still fail due to additional baselines, full format guesses, private state, or unrelated assertions? When `high`, `accept` is forbidden.

Output:
{{
  "decision": "accept|repair_setup|repair_trigger|repair_oracle|reject",
  "failure_class": "setup|syntax|collect|timeout|buggy_pass|target_not_hit|side_path|oracle_wrong|oracle_too_strong|issue_aligned",
  "target_hit": false,
  "oracle_grounded_in_issue": false,
  "uses_public_behavior": false,
  "oracle_kind": "ASSERT_EXPRESSION|EXCEPTION|NO_EXCEPTION|WARNING|LOGGING|RETURN_VALUE|TYPE_OR_SHAPE|STATE_CHANGE|SERIALIZATION|SQL|RENDER_OUTPUT|ORDERING|FRAMEWORK_ASSERTION|OTHER",
  "oracle_falsifiable": false,
  "reason": "",
  "next_action": "repair_setup|repair_trigger|repair_oracle|reject",
  "observed_behavior": "actual execution and failure behavior of the current candidate",
  "target_behavior": "public behavior required by the Issue after fix",
  "semantic_gap": "the most immediate and smallest unsatisfied difference",
  "preserve": ["satisfied behaviors that must be kept in the next round"],
  "change": ["exactly one behavior that may be changed in the next round"],
  "avoid": ["observed error paths or overly strong constraints"],
  "next_operator": "ARG_VALUE_REPLACE|ARG_BOUNDARY_EXPAND|OPERATOR_FLIP|CALL_CHAIN_EXTEND|STATE_MUTATION|FIXTURE_DATA_MUTATION|CONFIG_MUTATION|MOCK_BEHAVIOR_MUTATION|LIFECYCLE_TRIGGER|SERIALIZATION_TRIGGER|WARNING_LOG_TRIGGER|ORACLE_REFOCUS",
  "expected_effect": "expected change on the buggy version after the next modification",
  "failure_origin": "setup|test_body|unrelated_side_path|oracle",
  "post_fix_failure_risk": "low|medium|high|unknown"
}}

`change` must contain exactly one item, selecting only the most immediate unsatisfied difference. All other differences are deferred to subsequent execution rounds and must not be combined in the same round.

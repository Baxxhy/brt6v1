Please rewrite only the test oracle using the issue's `expected_behavior` and observable public behavior. Do not copy a buggy observation into the expected value, and do not change setup or trigger code. For SQL, check stable semantic fragments rather than the full string. Use dedicated assertions for warnings and logs. When the expected behavior is simply “must not crash,” use the smallest meaningful public invariant.

BehaviorTarget: {behavior_json}
ProtocolRecovery: {protocol_json}
Allowed oracle types: NO_EXCEPTION|EXCEPTION_TYPE|WARNING|LOGGING|EXACT_VALUE|TYPE_OR_SHAPE|STATE_CHANGE|SQL_VALIDITY|SERIALIZATION|RENDER_OUTPUT|ORDERING
Observation: {observation_json}
Current test: {candidate_code}
Execution log: {execution_log}

Put `# BRT_ORACLE_TYPE: <type>` on the first line, then return only the complete Python file.

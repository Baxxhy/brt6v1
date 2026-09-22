Extract the semantic facts needed to validate this executed candidate.

Issue: {issue_text}
Recovered reproduction target: {behavior_json}
Recovered test protocol: {protocol_json}
Candidate test: {candidate_code}
Command: {command}
Execution status: {execution_status}
stdout/stderr: {execution_log}
Repository context: {source_context}

Definitions:
- target_hit: the invocation and failing path exercise the behavior described by the recovered target.
- oracle_evidence: exact quotations that support the expected observation. Each source is issue, behavior_target, or repository_context.
- uses_public_behavior: the observation concerns behavior available through the tested interface rather than an unrelated private detail.
- oracle_falsifiable: the observation could distinguish correct from incorrect behavior.
- post_fix_risk: a candidate constraint that could still fail after the target behavior is fixed. `role=additional` only when it is distinct from the failure-driving target oracle. Use high only when constraint and location are concrete.
- failure_origin: where the observed failure actually originates.

Return exactly:
{{
  "target_hit": false,
  "oracle_evidence": [
    {{"source": "issue|behavior_target|repository_context", "quote": "exact visible quotation"}}
  ],
  "uses_public_behavior": false,
  "oracle_kind": "ASSERT_EXPRESSION|EXCEPTION|NO_EXCEPTION|WARNING|LOGGING|RETURN_VALUE|TYPE_OR_SHAPE|STATE_CHANGE|SERIALIZATION|SQL|RENDER_OUTPUT|ORDERING|FRAMEWORK_ASSERTION|OTHER",
  "oracle_falsifiable": false,
  "observed_behavior": "what actually happened",
  "target_behavior": "the evidence-supported behavior to observe",
  "semantic_gap": "one immediate semantic obligation, if any",
  "failure_origin": "setup|test_body|unrelated_side_path|oracle",
  "post_fix_risk": {{
    "level": "low|medium|high|unknown",
    "constraint": "specific extra constraint or empty",
    "code_quote": "exact candidate substring or empty",
    "line": 0,
    "role": "failure_driving|additional|unknown",
    "evidence_status": "supported|contradicted|unsupported|unknown",
    "reason": "brief evidence-based explanation"
  }},
  "reason": "brief factual analysis"
}}

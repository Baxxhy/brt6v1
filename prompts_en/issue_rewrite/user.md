Please turn the following issue and retrieved repository context into a precise bug-reproduction target. Keep claims grounded in the supplied evidence and stay concise.

Issue:
{issue_text}

Retrieved source:
{code_context}

Retrieved tests:
{test_context}

Return exactly one valid JSON object with every field below:
{{
  "issue_summary": "One-sentence summary",
  "trigger_condition": {{"text": "Triggering input, state, configuration, or call", "evidence": ["Short evidence"], "confidence": "high|medium|low"}},
  "error_symptom": {{"text": "Observed buggy behavior", "symptom_type": "exception|warning|wrong_return|wrong_type|wrong_sql|wrong_order|state_not_updated|serialization_error|performance|unknown", "evidence": ["Short evidence"], "confidence": "high|medium|low"}},
  "expected_behavior": {{"text": "Positive behavior expected after a correct fix", "evidence": ["Short evidence"], "confidence": "high|medium|low"}},
  "target_apis": [{{"name": "API name", "kind": "function|method|class|module|unknown", "source_path": "Known path or empty string", "reason": "Why it is relevant"}}],
  "suspected_bug_locations": [{{"path": "Source path", "object": "Function, class, or method", "lines": "Known range or empty string", "reason": "Why it is relevant"}}],
  "related_test_seeds": [{{"test_name": "Test name", "test_file": "Test path", "why_relevant": "Why it is a useful seed", "reusable_parts": ["Reusable imports, fixture, setup, construction, call, or assertion style"], "possible_gap": "Why it does not yet reproduce this issue"}}],
  "mutation_hints": [{{"slot": "input|argument|object_state|mock|config|call_chain|operator|boundary_value|unknown", "current_pattern": "Existing seed behavior", "target_pattern": "Issue-grounded trigger behavior", "reason": "Why this change may reach the bug", "confidence": "high|medium|low"}}],
  "observation_points": [{{"kind": "exception|warning|return_value|type|repr|str|sql|order|state|cache|config|file_output|serialization|unknown", "expression_hint": "Expression or public behavior to observe", "reason": "Why it reveals the issue"}}],
  "assertion_hints": [{{"assertion_goal": "Issue-aligned semantic property", "preferred_assertion_style": "contains_fragment|not_contains_fragment|equals|isinstance|raises|warns|before_after_relation|order_equals|unknown", "avoid": "Brittle or unrelated checks to avoid", "reason": "Why this assertion is stable"}}],
  "setup_hints": [{{"hint": "Required fixture or context", "source": "issue|retrieved_test|retrieved_source|inference", "confidence": "high|medium|low"}}],
  "uncertainties": ["Facts that execution or observation must still resolve"]
}}

Rules:
1. Use an empty value or low confidence when evidence is missing; do not invent details.
2. Distinguish explicit issue facts from repository-supported inference.
3. Keep evidence short and do not copy large source blocks.
4. `expected_behavior` must state positive fixed behavior, not repeat the bug.
5. Assertion polarity must agree with `expected_behavior`. A capability expected to exist must not become `not hasattr` or `not in`; behavior expected not to raise must not become `raises`.
6. If the issue does not specify an exact full string, use stable fragments, types, or relations rather than guessing the repaired output.
7. Do not generate test code or use fixed-side artifacts, a gold patch, or a gold test.
8. Return JSON only, parseable by `json.loads`, with no Markdown or commentary.
9. Separate the repository-internal defect trigger from an external tool used only to demonstrate it. If a third-party package or API appears in the Issue but not in the retrieved repository source or tests, do not make that package a required setup step, target API, or hard trigger. Describe the repository-native interaction or state that it induces when the supplied evidence supports one, and record the external workflow as an uncertainty or optional reproduction route.

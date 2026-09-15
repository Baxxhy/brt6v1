Please check whether this reproduction specification has a critical ambiguity.

Issue:
{issue_text}

Canonical behavior target:
{behavior_target}

Relevant source:
{code_context}

Relevant tests:
{test_context}

Return only this JSON object:
{{
  "ambiguity_status": "CLEAR|CRITICAL",
  "ambiguities": [
    {{
      "slot": "SETUP|TRIGGER|INVOCATION|ORACLE",
      "question": "The unresolved question",
      "issue_evidence": ["Exact short quote from the issue"],
      "repository_evidence": [{{"source_kind": "retrieved_test|retrieved_source", "source_ref": "File path or test name", "quote": "Exact short quote"}}]
    }}
  ],
  "alternative_hypotheses": [
    {{
      "hypothesis_id": "short_unique_id",
      "changed_slots": ["SETUP|TRIGGER|INVOCATION|ORACLE"],
      "rationale": "Why this interpretation deserves a separate branch",
      "evidence_bindings": [{{"slot": "SETUP|TRIGGER|INVOCATION|ORACLE", "source_kind": "issue|retrieved_test|retrieved_source", "source_ref": "issue, file, or test", "quote": "Exact short quote"}}],
      "overrides": {{
        "setup_hints": [],
        "related_test_seeds": [],
        "trigger_condition": {{}},
        "error_symptom": {{}},
        "mutation_hints": [],
        "target_apis": [],
        "observation_points": [],
        "expected_behavior": {{}},
        "assertion_hints": []
      }}
    }}
  ]
}}

Use `CLEAR` with empty arrays when no ambiguity would change the test. Do not create alternatives merely to add diversity. Each override may change only fields belonging to its declared ambiguous slots; omit unchanged fields.

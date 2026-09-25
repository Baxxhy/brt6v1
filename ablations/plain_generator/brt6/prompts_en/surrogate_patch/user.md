Please create a minimal surrogate source patch that helps assess whether the current BRT could be fail-to-pass.

Behavior target:
{behavior_json}

Test on the buggy version:
{final_test}

Buggy execution log:
{buggy_execution_log}

Allowed production source context:
{code_context}

Previous surrogate attempts and feedback:
{previous_attempts}

Requirements:
1. Modify only the listed production files, using at most three search/replace edits.
2. Implement `expected_behavior`; never hard-code a value only to satisfy the test.
3. Do not modify tests, `test_*.py`, `conftest.py`, configuration, or dependency files.
4. Copy each `search` block exactly from the supplied source and keep it to roughly 3–30 lines. Do not replace an entire function or class.
5. Each `replace` block must contain the complete replacement and differ meaningfully from `search`.
6. If an earlier patch applied but the test still failed, revise the root-cause hypothesis or edit location instead of repeating it.
7. If the evidence is insufficient, return an empty `patches` list.

Return only this JSON object:
{{
  "analysis": "Brief root-cause and repair rationale",
  "confidence": "high|medium|low",
  "patches": [
    {{
      "path": "Relative path to the relevant production file",
      "search": "Exact original source text",
      "replace": "Complete replacement text",
      "reason": "How this change implements expected_behavior"
    }}
  ]
}}

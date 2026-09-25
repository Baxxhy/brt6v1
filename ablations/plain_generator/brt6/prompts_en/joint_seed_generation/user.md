Generate 1 Bug Reproduction Test following the Top-3 Joint Reference Workflow.

Instance: {instance_id}
Safe test name suffix: {safe_instance_id}
Insertion strategy: {insert_strategy}

[Full Issue]
{issue_text}

[Behavior Target & Original Evidence]
{behavior_json}

[Primary Test Protocol & Host Context]
{host_context_json}

[Three Reference Tests Sorted by iCoRe Relevance]
{reference_seed_bundle}

[Relevant Buggy Source Code]
{code_context}

[Existing Execution Feedback]
{feedback}

Generation steps:
1. Use the test framework, imports, fixtures, class context, execution style, and insertion location from the rank=0 reference test.
2. Synthesize input, state, and API usage evidence from rank=0, rank=1, and rank=2 that align with the Issue's trigger conditions.
3. Construct the minimal test scenario that exercises the defect path described in the Issue.
4. Express the Issue's expected behavior as a public, stable, and falsifiable check.
5. Return a complete Python file containing a single test entry point.

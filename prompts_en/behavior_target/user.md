Based on the Issue's structured goal, similar test, and full test file excerpt, determine which imports, fixtures, classes, setup, decorators, and assertion styles the new BRT should reuse. Output only JSON.

Example: {instance_id}
Behavior goal: {behavior_json}
Similar test: {seed_test}
Full file excerpt: {full_file_excerpt}

Output fields: host_file, host_class, seed_test_name, imports, setup_context, fixtures, decorators, pytestmark, insert_strategy, insert_location_hint, risks.

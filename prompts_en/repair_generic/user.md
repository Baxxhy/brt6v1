Perform one normal iteration on the current Bug Reproduction Test. Do not assume the feedback has been correctly classified; simultaneously check the executable context, the issue trigger path, and the final assertion.

Return a complete, independently collectable Python test file that contains exactly one test entry.
Preferentially preserve any parts that already run and align with the issue; modify only those problems that the actual execution log and verifier feedback can support.

Do not install dependencies, modify production code, read real patches, add skip, swallow exceptions, insert tautological assertions, or write buggy behavior as expected behavior.

Full Issue: {issue_text}
Behavior Evidence: {behavior_json}
HostContext: {host_context_json}
ProtocolRecovery: {protocol_json}
Similar Test Seed: {seed_test_code}
Related Source Code: {code_context}
Current Test: {candidate_code}
Execution Command: {command}
Execution Status: {execution_status}
Execution Log: {execution_log}
Verifier Feedback: {verifier_feedback}

If the feedback contains a Semantic Delta Contract, preserve `preserve` and complete the single semantic obligation under `change`. You may update the imports, fixture wiring, and variable bindings required for that obligation, but must not alter an independent setup, trigger, or oracle obligation. Avoid `avoid` and ensure the result produces `expected_effect`.

Integrate all the above information to complete one fix. Output only the complete Python code.

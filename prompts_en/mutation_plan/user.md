Compare the current test against the target behavior and propose the single Delta for this round.

Planning scope:
{round_scope}

Independent branch strategy:
{adaptation_strategy}

Raw Issue (lossless source of facts):
{issue_text}

Evidence priority: explicit Issue facts take precedence over structured
inference. Low-confidence BehaviorTarget fields and uncertainties may help
locate a difference, but must not introduce a trigger, exception, or expected
value that the Issue does not require.

BehaviorTarget:
{behavior_json}

HostContext:
{host_context_json}

ProtocolRecovery:
{protocol_json}

Relevant source code:
{source_context}

Current test (first round: retrieved seed; subsequent rounds: previous candidate):
{seed_test_code}

Previous actual execution:
{execution_feedback}

Previous Verifier:
{verifier_feedback}

Delta history attempted:
{delta_history}

Requirements:
1. Use the most upstream changed dimension among CONTEXT, INTERACTION, or OBSERVATION.
2. In the initial adaptation, `change` describes one coherent reproduction scenario and may include the linked setup/input, invocation, and oracle edits required by that scenario. In feedback rounds, `change` must be one residual action and must not chain independent modifications.
3. `preserve` lists items to keep by default, not static hard constraints; if upstream changes invalidate downstream items, leave them for a later round.
4. `avoid` records paths already proven false; do not repeat previously failed actions.
5. Output KEEP when evidence is insufficient; do not copy the KEEP reason from the previous round.
6. Preserve every explicit Issue call argument, literal, operator, call order, and acceptable alternative. Do not narrow an Issue disjunction to one implementation. Bind observations to the object or state produced by the adapted target invocation, not to the parent test's precursor object.

MUTATE format only:
{{"schema_version":"semantic_delta.v2","action":"MUTATE","dimension":"CONTEXT|INTERACTION|OBSERVATION","seed_fact":"current candidate fact","target_fact":"target fact","change":"single modification for this round","preserve":["default items to keep"],"avoid":["paths already proven false"],"reason":"why this is the most upstream difference"}}

KEEP format only:
{{"schema_version":"semantic_delta.v2","action":"KEEP","dimension":"","seed_fact":"current fact","target_fact":"target fact not reliably reachable now","change":"","preserve":[],"avoid":[],"reason":"why no reliable single-step difference can be proposed"}}

Compare the current test against the target behavior and propose the single Delta for this round.

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
1. Select only the single most upstream difference among CONTEXT, INTERACTION, or OBSERVATION.
2. `change` must be a single action directly applicable to modify the current test; do not chain multiple modifications.
3. `preserve` lists items to keep by default, not static hard constraints; if upstream changes invalidate downstream items, leave them for a later round.
4. `avoid` records paths already proven false; do not repeat previously failed actions.
5. Output KEEP when evidence is insufficient; do not copy the KEEP reason from the previous round.

MUTATE format only:
{{"schema_version":"semantic_delta.v2","action":"MUTATE","dimension":"CONTEXT|INTERACTION|OBSERVATION","seed_fact":"current candidate fact","target_fact":"target fact","change":"single modification for this round","preserve":["default items to keep"],"avoid":["paths already proven false"],"reason":"why this is the most upstream difference"}}

KEEP format only:
{{"schema_version":"semantic_delta.v2","action":"KEEP","dimension":"","seed_fact":"current fact","target_fact":"target fact not reliably reachable now","change":"","preserve":[],"avoid":[],"reason":"why no reliable single-step difference can be proposed"}}

Change the current single-entry test to a probe. Keep the setup and trigger, but remove or replace the original assertions; instead, observe the public behavior. You must print `BRT_OBS_START`, a JSON object, and `BRT_OBS_END`.

BehaviorTarget: {behavior_json}
ProtocolRecovery: {protocol_json}
Current test: {candidate_code}

Output only the complete Python file.

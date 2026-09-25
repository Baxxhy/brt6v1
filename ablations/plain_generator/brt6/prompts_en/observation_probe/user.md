Convert the following candidate test into a probe test by inserting runtime observation points. Print the fixed markers exactly once after all target calls are complete, and never before observations are finished:

```
print("BRT_OBS_START")
print(json.dumps(observations, default=str, ensure_ascii=False))
print("BRT_OBS_END")
```

The probe may catch and record exceptions, but must not swallow setup, import, or collection errors.

Behavior target: {behavior_json}
Candidate test: {candidate_code}

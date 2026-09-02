# Final Experiment Reference

This directory freezes the code snapshot associated with the original BRT4 P0 136/276 SWT-Bench Lite result. It does not copy, regenerate, or modify the experimental outputs.

## Original Run

`/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545`

## Key Files

- Metrics: `/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545/evaluation/formal/metrics.json`
- Merged results: `/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545/evaluation/formal/merged_results.json`
- Generation command: `/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545/logs/generation.command.txt`
- Formal evaluation command: `/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545/logs/formal_eval.command.txt`
- Run config: `/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545/run_config.json`

## Metrics

- total_instances: 276
- F2P_SUCCESS: 136
- FIXED_FAIL: 113
- BUGGY_PASS: 17
- BUGGY_SETUP_ERROR: 5
- ERROR: 5
- F2P@1: 49.2754%

## SHA256

- metrics.json: `50820404dc901910c1628b006536b9c04f53ee7d70b6c60d442a7ccfd12cb1d2`
- merged_results.json: `23746b7a5866042dd8cbbb7d6f2f450b7e7297966262acc0ec8c108d64cb344f`
- all final_test.py aggregate: `97bcb058d92960969e5b3bae2a35d0c3a3ffc154a49c264d1dae6f8d9dacee76`
- run_config.json: `9462fa6635dbbf3053e74097e9b7c6e1a465d0b5b645ccf1f6c51a5b866960c2`
- generation command: `07a6ab1db668c45fe5cadcd9bf7cb005f3639e189818e31090eb720973c10b38`
- formal evaluation command: `be1c787ce6dce9cd9d36e40e217c097ead987d4f5c2d905fbc56c6d2f227de43`

## Note

The final code directory is a Git worktree created from `ca41aa6a0a85071a35219bf91c3c39a25bd0e127`. The original result directory remains in place under `/root/Baxxhy/BugReproduce/brt4/results/runs/` and is not modified by this freeze.

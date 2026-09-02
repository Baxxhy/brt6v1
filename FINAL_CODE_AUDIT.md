# BRT4 Final P0 136/276 Code Audit

## Conclusion

The 136/276 SWT-Bench Lite result was produced from `/root/Baxxhy/BugReproduce/brt4`, not `brt4v2`.  The generation run started on 2026-07-06 01:44:30 while the Git HEAD was `d46991443fad63e1380a6f497cc9dcb788be1b65` on branch `main`; the P0 adaptive/risk-aware changes were present as uncommitted working-tree changes and were later committed as `ca41aa6a0a85071a35219bf91c3c39a25bd0e127` at 2026-07-06 15:51:50.  This worktree freezes that committed snapshot.

## Source Identity

- Source repository: `/root/Baxxhy/BugReproduce/brt4`
- Source remote: `git@github.com:Baxxhy/brt4.git`
- Run-start branch: `main`
- Run-start HEAD: `d46991443fad63e1380a6f497cc9dcb788be1b65`
- Final P0 source commit: `ca41aa6a0a85071a35219bf91c3c39a25bd0e127`
- Final branch: `final/p0-136`
- Final tag: `p0-136-f2p-49.28`
- Source dirty at run start: yes, because `ca41aa6` did not exist until after formal evaluation; the exact P0 changes were committed afterward.

## Evidence Chain

1. `results/debug_before_p0_20260706_012355/head.txt` records `d46991443fad63e1380a6f497cc9dcb788be1b65`; `git_status.txt` records branch `main` and no tracked diff.
2. Reference `run_config.json` was created at `2026-07-06 01:44:30` and points to `/root/Baxxhy/BugReproduce/brt4/...`, GPT retrieval paths, model `deepseek-v3`, temperature `0.1`, workers `6`, seed workers `2`.
3. Reference `command.txt` and `logs/generation.command.txt` invoke `python -m brt4.run` with `/root/Baxxhy/BugReproduce/brt4` input/output paths.
4. Reference summaries contain P0 fields introduced by the later `ca41aa6` diff: `seed_mode=adaptive_top3`, `seed_attempts_summary`, `final_oracle_risk`, `final_surrogate_risk`, runner placement fields, and risk-aware ranking fields.
5. `results/final_commit_backup_20260706_154727/head_before.txt` still records `d469914...`, while `working_tree_before_commit.patch` records the adaptive seed/risk/direct-eval modifications later committed as `ca41aa6`.
6. `git reflog` records `ca41aa6` commit on `main` at `2026-07-06 15:51:50`, after `formal_eval.done` timestamp `2026-07-06 15:25:50`.
7. `brt4v2` was cloned on 2026-07-14 and branch `recovery/p0-selector-v2` adds Selector V2/posthoc/patch-coverage tooling after `ca41aa6`; it is not the original 136 generation code.

## Reference Result

- Reference run: `/root/Baxxhy/BugReproduce/brt4/results/runs/run_p0_adaptive_seed_full276_20260706_013545`
- Metrics SHA256: `50820404dc901910c1628b006536b9c04f53ee7d70b6c60d442a7ccfd12cb1d2`
- Merged results SHA256: `23746b7a5866042dd8cbbb7d6f2f450b7e7297966262acc0ec8c108d64cb344f`
- Final tests aggregate SHA256: `97bcb058d92960969e5b3bae2a35d0c3a3ffc154a49c264d1dae6f8d9dacee76`
- Recomputed status counts: F2P_SUCCESS=136, FIXED_FAIL=113, BUGGY_PASS=17, BUGGY_SETUP_ERROR=5, ERROR=5.
- `merged_results.json`: 276 rows, 276 unique instance IDs, no duplicates, no dataset misses.
- `generation`: 276 `summary.json`, 276 `final_test.py`.

## Candidate Comparison

- `d469914`: structural refactor only; does not contain P0 adaptive seed/risk fields used by the run outputs.
- `ca41aa6`: first commit containing adaptive top-3 seed retry, oracle/surrogate risk scoring, runner parity metadata, and the SWT dataset file used by the run.
- `b7908ab` and later: root cleanup and later experimental changes after the 136 result; not needed for exact 136 generation.
- `brt4v2/recovery/p0-selector-v2`: posthoc Selector V2 and patch coverage recovery branch, not the 136 generation snapshot.
- `exp/p2-nsgem-brt` and later counterfactual branches: later experiments, excluded.

## Excluded Later Code

The final source intentionally excludes Selector V2 posthoc selection, counterfactual/negative-control experiments, NS-GEM/P2 modules, patch coverage collectors, post-2026-07-06 conda environment isolation fixes, and TDD artifacts.

## Fingerprints

- Source file aggregate SHA256: `3add9ddeead5703e358c20bc70bb2fade1ceef97a70778a0bee552fb111a6973`
- Prompt aggregate SHA256: `31b1e3db8d49e6212acb32ed2bc683d4bc3c0e933bfba0453e8ea4577f713da1`
- Source manifest SHA256: `9884fbcc3c67b16b7a594a264e550bbb5c3e74d4de4ebd3d3ea461e8c25f1d08`
- Prompt manifest SHA256: `dff6916074b8691b4a957f1d72c9558639e145a6a9ac66b8bbef8acf03c470a6`

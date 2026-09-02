# BRT6 Official-Benchmark Migration Checklist

## SWT-22 missing-generation recovery (2026-08-06)

- [x] Verify the source run stopped formal evaluation at 254/276 and enumerate the exact 22 missing IDs.
- [x] Preserve the source run and its 254 generated tests as read-only recovery inputs.
- [x] Classify the 22 failures from per-attempt official image-build logs.
- [x] Confirm the checked-out/latest official SWTBench recipes still contain the failing mutable install commands and deterministic retry name/build-directory behavior.
- [x] Reconcile timed-out container creates by exact attempt name and increase the Docker SDK API timeout to 300 seconds.
- [x] Give every retry an isolated source/build context and clean it after use.
- [x] Normalize only the incompatible official setup commands for current pip/build backends, with an audit record in each runtime manifest.
- [x] Add repo-aware recovery scheduling and serialize Matplotlib image builds.
- [x] Add regression tests for unknown create outcomes, retry naming, source-context isolation, and setup normalization.
- [x] Remove exact stale `exec.eval.*` containers/images and dangling images while retaining all 32 `exec.base.*`/`exec.env.*` images.
- [x] Pass focused tests, the full 235-test BRT6 suite, Docker capacity/storage gates, and a real `django__django-13925` one-instance pilot.
- [x] Launch only the 22-instance DeepSeek V3 recovery with durable PID/command/logs and perform an initial startup check (`official_docker_swt_deepseekv3_swt22_recovery_20260807_000545`, PID `727419`).
- [ ] Refuse merge/formal evaluation unless the frozen 254 plus recovered 22 produce 276/276 non-empty tests.

## DeepSeek V3 SWT-276 restart (2026-08-04)

- [x] Confirm no existing BRT6 generation/evaluation process or active Docker container.
- [x] Pass dataset, DeepSeek API pool, BehaviorTarget cache, official harness, overlay2, and 120 GiB capacity gates.
- [x] Launch fresh detached run `official_docker_swt_deepseekv3_full_20260804_144939` with durable PID/command/log files (PID `4128551`).
- [x] Confirm the detached process remains alive and reaches `generation_start` without an immediate gate failure.
- [x] Stop active monitoring after the initial start check, preserving all run artifacts.

## Docker storage recovery (2026-08-04)

- [x] Audit Docker root, driver, stopped containers, image classes, dangling images, and physical VFS usage.
- [x] Confirm the failed run generated only 7/276 tests and invalidate its automatic formal result.
- [x] Verify a real OverlayFS lower/read and upper/write mount on the `/root` ext4 filesystem.
- [x] Export and verify shared `exec.base.*` and `exec.env.*` images before changing the daemon (`shared-base-env-and-ubuntu.tar`, SHA-256 `2b48f594...aa98148`).
- [x] Remove only stopped BRT6 `exec.eval.*` containers, `exec.eval.*` images, and dangling images; preserve base/env and `/pkp` (about 165 GiB physical root-partition space recovered from VFS copies).
- [x] Move Docker data-root to `/root/Baxxhy/BugReproduce/docker-data`, enable `overlay2`, and verify daemon identity plus a disposable container.
- [x] Restore the preserved base/env images into the migrated daemon and verify their exact tags.
- [x] Delete each instance image when official generation finishes while retaining environment images.
- [x] Make cleanup classification accept both `exec.*` and `sweb.*` image names.
- [x] Enforce the Docker data-root free-space and non-VFS generation gates.
- [x] Refuse formal evaluation unless generation is complete for the full dataset.
- [x] Pass focused unit tests, Python compilation, shell syntax, the 231-test suite, and a real official-Docker lifecycle smoke test.

## Official Docker generation migration

- [x] Audit Issue2Test, AEGIS, and Echo container/feedback designs.
- [x] Trace every BRT6 seed, candidate, probe, dependency-recovery, and environment-setup execution path.
- [x] Add a one-official-container-per-instance runtime for SWTBench and TDDBench.
- [x] Build runtime specs from non-gold metadata only.
- [x] Mirror host candidate deltas into the official buggy checkout before each command.
- [x] Route all generation executions through the active official container.
- [x] Bypass host project Conda creation, cloning, installation, repair, locking, and cleanup.
- [x] Record an auditable runtime manifest and container cleanup result per instance.
- [x] Add unit tests for dispatch, path mapping, base reset, no-gold input, and no-host-Conda behavior.
- [x] Pass Python compilation, focused tests, shell syntax checks, and the full 226-test suite.
- [x] Pass a real one-instance SWT official-Docker generation smoke test (`astropy__astropy-12907`, `1 passed`, container cleaned).
- [x] Mark the old local-Conda full run superseded and stop it without deleting artifacts.
- [x] Launch a clean detached DeepSeek V3 SWT-276 Docker run and perform only the initial start check (`official_docker_swt_deepseekv3_full_20260802_233706`, PID `3929995`).

## Completed copy/evaluator work

- [x] Keep `/root/Baxxhy/BugReproduce/brt5` unchanged.
- [x] Create `/root/Baxxhy/BugReproduce/brt6` with Git history and current tracked code changes.
- [x] Leave historical result/output/artifact directories empty.
- [x] Add official SWTBench and TDDBench prediction exporters.
- [x] Route formal evaluation through both official Docker harnesses.
- [x] Update active package references from `brt5` to `brt6`.
- [x] Add a detached DeepSeek V3 SWT-276 full-run launcher.
- [x] Run focused tests, compilation, shell syntax, Docker, API, and harness preflight checks.
- [x] Launch the full SWT run and record PID/log/command without ongoing monitoring.

# Inherited BRT5 Checklist (historical)

## Identity

- branch: `codex/reproducible-clean`
- pilot run: `p0_simple_llm_selector_canary5_20260717`
- full run: `p0_simple_llm_selector_full276_20260717`
- stage: five isolated ablations accepted; release submission authorized

## Code freeze

- [x] HEAD confirmed as `p0-136-f2p-49.28` / `a167ad6`
- [x] previous broad refactor preserved in stash
- [x] dynamic reachability removed from active flow
- [x] independent oracle-risk selection removed
- [x] LLM-only semantic selector implemented
- [x] simple ranking implemented
- [x] surrogate calls remain absent
- [x] environment and formal-evaluation fixes preserved
- [x] focused tests pass under literal `icore` (7 method + 50 environment tests)

## Five-instance pilot

- [x] fresh IssueRewrite 5/5
- [x] fresh generation 5/5
- [x] zero surrogate calls verified
- [x] project commands use recorded `setup_*` environments
- [x] formal F2P evaluates denominator 5
- [x] no environment/setup/collection failures
- [x] required metric keys complete and finite
- [x] at least one `F2P_SUCCESS` (3/5 = 60.0%)

## Full 276

- [x] dataset with official gold patches prepared for evaluation only
- [x] full detached chain launched under literal `icore`
- [x] generation configured for all 276 rows
- [x] evaluation configured for all 276 rows including missing generations
- [x] project evaluation resolves each instance's own environment
- [x] command, PID, and durable logs recorded

## Environment recovery

- [x] original full run completed: 273 generated, 3 missing, 131/276 F2P
- [x] missing instances fixed to the exact three `MISSING_GENERATION` rows
- [x] failed hashed environments confirmed present and recoverable
- [x] three isolated recovery generations launched under literal `icore` (`brt5_env3_recovery_f2p`)
- [x] all three recovered `final_test.py` files verified in the eight-instance recovery run
- [x] zero surrogate calls verified for all three recovered summaries
- [x] eight-row generation view is complete (8/8); unchanged 268 rows remain in the frozen source result
- [x] eight formal rows are merged into the frozen 276-row result with denominator 276
- [x] recovery result recorded: 132/276 F2P, delta +1 versus 131/276

## Environment isolation code repair

- [x] requirements files are worktree-local and unique per environment
- [x] legacy `$HOME/requirements.txt` scripts are rewritten before execution
- [x] dependency templates are never used for project setup or tests
- [x] generation uses a fresh instance runtime clone and records its template
- [x] formal evaluation uses a separate fresh instance runtime clone
- [x] runtime clones are removed after their owning instance
- [x] Astropy 1.3 dependency contract matches its build/runtime requirements
- [x] Pylint 2.15 formal evaluation preserves the contract Astroid version
- [x] environment manifest fingerprints and `pip check` results are recorded
- [x] regression tests for the above are written
- [x] static syntax and 67 focused unit tests executed under literal iCoRe
- [x] three missing-instance generations executed under literal iCoRe
- [x] all eight instances formally evaluated in isolated project environments (6 success, 2 fixed fail, 0 environment errors)
- [x] eight rows merged into the 276-row result with denominator 276 (132/276 = 47.8261%)
- [x] no `brt5i_*` runtime environments or generation/evaluation processes remain

## B-machine environment-integrity repair

- [x] B-machine failure mechanisms mapped to concrete environment code paths
- [x] duplicate dependency metadata is rejected
- [x] imported dependency versions and ABI-sensitive imports are checked
- [x] repair purges orphan metadata and force-reinstalls coherent dependency groups
- [x] disposable clones scrub stale benchmark-project namespace and editable residue
- [x] legacy Matplotlib setuptools protocol is compatible with warnings-as-errors startup
- [x] all 127 unit tests pass under literal `icore`; all 105 Python files compile
- [x] real Xarray, Scikit-learn, and current Matplotlib template integrity probes pass
- [x] contaminated legacy Matplotlib template is rejected; current hashed template passes after targeted repair
- [x] diff reviewed and release commit prepared for the B-machine rerun

## Five isolated ablations

- [x] ablation definitions and mutual-exclusion contract frozen
- [x] normalized AblationConfig and CLI controls implemented
- [x] result metadata and resume signature implemented
- [x] w/o Mutation removes all downstream mutation guidance and artifacts
- [x] Generic Iteration uses only the general three-repair loop
- [x] environment/trigger/assertion repair routes can each be disabled alone
- [x] every ablation disables Patch Coverage while full mode preserves it
- [x] focused and full regression tests pass under literal iCoRe (141/141)
- [x] five independent three-instance IssueRewrite-generation-F2P canaries complete
- [x] no full 276-row ablation launched; release commit/push separately authorized

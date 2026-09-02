# BRT6 Official-Benchmark Migration Plan

## SWT-22 missing-generation recovery contract (2026-08-06)

- Source run: `official_docker_swt_deepseekv3_full_20260804_144939`; preserve its 254 non-empty generated tests and all historical logs read-only.
- Recovery scope: regenerate only the 22 instances named by the source run's `generation_gate.json`, with the same frozen SWT-276 issue dataset, BehaviorTarget cache, DeepSeek V3 model, prompts, budgets, selection method, and official SWTBench execution/evaluation semantics.
- Failure classes: three Docker create-response timeouts followed by deterministic-name 409 conflicts; two concurrent build-context races; seven deterministic official install recipes incompatible with the current unpinned pip/build backend; and ten Matplotlib builds affected by concurrent network/I/O pressure (Ubuntu archive disconnects or Git status timeout).
- Repair boundary: make BRT6's official-harness wrapper reconcile unknown Docker-create outcomes by exact name, use a longer Docker API timeout, isolate each attempt's source/build context, and apply narrowly scoped compatibility normalization to the official generated setup script. Do not alter tests, issue inputs, benchmark commands, patches, scoring, or generation logic.
- Concurrency boundary: recovery uses repository-aware bounded scheduling, including serialized Matplotlib builds, so large apt/TeX downloads and same-repository source preparation do not repeat the six-way contention that caused the original failures.
- Storage boundary: remove only stale `exec.eval.*` containers/images and dangling images before recovery; retain shared `exec.base.*` and `exec.env.*`. Continue deleting each recovered instance image after its instance ends.
- Validation gate: pass focused lifecycle/setup-normalization tests, the BRT6 regression suite, one previously failing Docker-lifecycle pilot, and capacity/storage preflight before launching the remaining recovery.
- Merge/evaluation gate: create an isolated symlink-only 276-row generation view containing the frozen 254 source artifacts plus recovered artifacts. Formal SWTBench evaluation is forbidden until all 276 links resolve to non-empty `final_test.py`; no partial denominator or synthetic zero score may be published.
- Run control: launch recovery with durable command, PID, logs, manifests, and completion gate; verify initial startup only, then leave it detached without continuous monitoring as previously requested.

## DeepSeek V3 SWT-276 relaunch contract (2026-08-04 14:49 CST)

- Run ID: `official_docker_swt_deepseekv3_full_20260804_144939`; this is a fresh run and does not reuse the incomplete 7/276 generation.
- Command: `bash scripts/run_official_swt_full.sh`, detached with an explicit `RUN_TIMESTAMP=20260804_144939` and durable PID, command, launcher log, and per-run pipeline log.
- Dataset/model: frozen SWT-276 issue-only dataset, provider `deepseek`, model `deepseek-v3`, full method, frozen 276-row BehaviorTarget cache.
- Runtime: six generation workers, official per-instance SWTBench Docker containers, `overlay2`, only base/env images cached, instance images deleted on close.
- Gates at launch: 276 dataset rows, 13 DeepSeek API entries, zero active containers, zero `exec.eval.*` images, Docker data-root `/root/Baxxhy/BugReproduce/docker-data`, about 586 GiB free versus the 120 GiB minimum.
- Evaluation: formal SWTBench evaluation starts only after generation returns zero and all 276 non-empty `final_test.py` artifacts exist.
- Monitoring contract: verify the detached process survives startup and reaches generation, then stop active monitoring as previously requested; all later evidence remains in the run directory.

## Docker storage recovery contract (2026-08-04)

- Failure being repaired: `official_docker_swt_deepseekv3_full_20260802_233706` stopped after 7/276 generated tests with `Errno 28`; its subsequent 0/276 formal result is invalid because generation was incomplete.
- Cleanup boundary: remove stopped BRT6 `exec.eval.*` containers, all `exec.eval.*` instance images, and dangling images. Preserve shared `exec.base.*` and `exec.env.*` images. Preserve the unrelated stopped `/pkp` container and its metadata.
- Cache contract: every official-generation instance removes its exact instance image after its container is removed; only base and environment images remain cached.
- Compatibility contract: image cleanup recognizes both the official harness's historical `sweb.{base,env,eval}` names and the active `exec.{base,env,eval}` names.
- Capacity gate: official Docker generation requires at least 120 GiB free at Docker's data root by default and rejects the `vfs` storage driver by default. Both thresholds are explicit environment-policy overrides for controlled diagnostics only.
- Evaluation gate: formal evaluation is refused unless generation returned success and every dataset row has a non-empty `final_test.py`. An incomplete run records a durable skip/refusal reason and cannot publish denominator-bearing formal metrics.
- Migration target: `/root/Baxxhy/BugReproduce/docker-data` on the `/root` ext4 filesystem, using `overlay2` after a real overlay mount probe. Export base/env images before the switch, verify daemon root/driver and a disposable container afterward, and restore the old daemon configuration if validation fails.
- Recovery boundary: retain the old `/var/lib/docker` store until the new daemon and restored shared images are verified. Do not delete unrelated Docker state as part of BRT6 cleanup.

### Recovery evidence

- Cleanup left zero `exec.eval.*` images, zero dangling images, and zero BRT6 stopped containers; the four env tags and one base tag were preserved exactly.
- Root-partition free space increased from about 11 GiB to about 176 GiB after removing the VFS-expanded instance/dangling layers.
- Docker now reports data-root `/root/Baxxhy/BugReproduce/docker-data`, driver `overlay2`, backing filesystem `extfs`, `Supports d_type=true`, and `Native Overlay Diff=true`.
- The restored Docker store occupies about 6.5 GiB. The verified 6.4 GiB migration tar and its SHA-256 sidecar remain in `/root/Baxxhy/BugReproduce/docker-migration-20260804`.
- The old `/var/lib/docker` store occupies about 75 GiB and remains untouched as rollback storage for the unrelated `/pkp` container; the active daemon no longer uses it.
- A real `astropy__astropy-12907` official-generation smoke passed and left zero containers/instance images; its manifest records both cleanup return codes as zero while all shared base/env tags remain.
- The incomplete historical 7/276 generation was presented to the new formal entrypoint: it returned 3 with `refused_incomplete_generation`, reported 269 missing instances, and did not invoke Docker.
- Python compilation, launcher shell syntax, cleanup-prefix assertions, focused tests, and the final 231-test BRT6 suite pass.

## Active run contract (2026-08-02)

- Objective: preserve the copied BRT5 generation method and run DeepSeek V3 on all 276 SWT-Bench Lite/SWT instances.
- Source baseline: `/root/Baxxhy/BugReproduce/brt5`, including its current tracked working-tree changes.
- Target workspace: `/root/Baxxhy/BugReproduce/brt6`; historical `results/`, `output/`, `outputs/`, `tmp/`, and `artifacts/` are intentionally not copied.
- Dataset contract: generation uses the frozen 276-row issue-only input; official SWT evaluation loads `princeton-nlp/SWE-bench_Lite` and applies the official SWT filter. TDDBench support uses the official 449-row `TDD_Bench.json`.
- Environment contract: every dynamic generation action (seed qualification, candidate execution, observation probes, and feedback reruns) executes in the buggy/base-commit instance image built by the checked-out official SWTBench or TDDBench Docker harness. BRT6 must not create, clone, repair, install into, or select a project Conda environment on the host.
- Evaluation contract: formal scoring is delegated unchanged to the checked-out official Docker harnesses in sibling `swt-bench` and `TDD-Bench-Verified` repositories.
- Isolation contract: one official container is reused within one instance and removed when that instance finishes. The host worktree is source/candidate staging only; before each execution its candidate-file delta is mirrored into `/testbed` after a buggy-base reset.
- Leakage contract: generation runtime construction receives only `instance_id`, `repo`, `version`, `base_commit`, and `environment_setup_commit`; gold code/test patches are neither loaded nor passed to generation.
- Model contract: provider `deepseek`, model id `deepseek-v3`, temperature `0.1`.
- Change boundary: runtime dispatch, official Docker lifecycle, package/path identity, official prediction export, run manifests, and documentation only; generation, mutation, feedback, verifier, ranking, prompts, and budgets remain unchanged.
- Smoke gate: runtime unit tests (including no-gold and no-host-Conda assertions), Python compilation, shell syntax checks, Docker availability, official image/container execution for one SWT instance, API-pool presence, and launcher preflight.
- Full command: `bash scripts/run_official_swt_full.sh`; launch detached with durable PID, command, and log files, then stop monitoring as requested.
- Expected outputs: `results/runs/<run>/generation`, `evaluation/formal_f2p/official_predictions.json`, official Docker logs/reports, `metrics.json`, `completion.json`, and `logs/full_pipeline.log`.
- Stop conditions: missing official harness checkout, Docker unavailable, empty DeepSeek pool, invalid 276-row cache, or launcher exiting during the initial process-start check.

### Supersession rule

- The local-Conda run `official_swt_deepseekv3_full_20260802_191926` is retained while the Docker path is being validated, but it is non-comparable under the revised contract.
- Stop that run only after the real single-instance Docker smoke test passes. Preserve its artifacts and label it superseded; do not merge or reuse its generation outputs.
- Launch a fresh SWT-276 run with `runtime_backend=official_docker`, verify only that the detached process enters generation successfully, then stop monitoring as requested.

## Revision log

| Date | Change | Reason |
|---|---|---|
| 2026-08-02 | Created BRT6 without historical result directories | User explicitly excluded experiment outputs from the copy |
| 2026-08-02 | Replaced custom formal evaluation with official SWTBench/TDDBench Docker harness adapters | Match the requested official environments and metrics while leaving generation logic unchanged |
| 2026-08-02 | Launched detached DeepSeek V3 SWT-276 full run `official_swt_deepseekv3_full_20260802_191926` | Initial launch check reached `generation_start`; ongoing monitoring intentionally stopped per user request |
| 2026-08-02 | Supersede host project-Conda generation with one official buggy-instance Docker container per task | Align generation feedback with Issue2Test/AEGIS/Echo-style container isolation while preserving BRT6's controller and preventing gold-patch access |
| 2026-08-02 | Passed real Docker smoke on `astropy__astropy-12907` | Official SWTBench instance image ran a staged test in `/testbed`, returned `1 passed`, and cleaned its container |
| 2026-08-02 | Stopped and marked the old local-Conda run superseded; retained all artifacts | Prevent non-comparable generation outputs from entering the Docker-contract run |
| 2026-08-02 | Launched detached run `official_docker_swt_deepseekv3_full_20260802_233706` (PID `3929995`) | Full 276-instance DeepSeek V3 SWT run entered `generation_start` with six official-Docker workers; no further monitoring requested |
| 2026-08-04 | Marked the detached run incomplete and its automatic 0/276 evaluation invalid | Generation stopped at 7/276 when Docker `vfs` exhausted the system partition |
| 2026-08-04 | Added Docker cleanup, overlay2 migration, capacity, and generation-completeness contracts | Prevent per-instance layer accumulation and forbid formal scoring of partial generation |
| 2026-08-04 | Migrated the active daemon to `/root/Baxxhy/BugReproduce/docker-data` on overlay2 and restored shared images | Eliminate VFS layer multiplication on the system partition while retaining environment caches |
| 2026-08-04 | Passed a real no-LLM instance lifecycle smoke and a 7/276 formal-refusal smoke | Verify both automatic instance-image eviction and the incomplete-generation gate end to end |
| 2026-08-04 | Approved fresh relaunch `official_docker_swt_deepseekv3_full_20260804_144939` after all storage/API/data gates passed | Resume the requested DeepSeek V3 SWT-276 experiment without reusing invalid partial output |
| 2026-08-04 | Launched the fresh run as detached PID `4128551` and observed `generation_start` | Initial start gate passed; active monitoring stopped per the user's standing request |
| 2026-08-06 | Classified all 22 missing generations and added exact-name retry reconciliation, isolated build contexts, packaging-tool compatibility pins, and apt transfer retries | Official recipes remain semantically unchanged but current pip 26, Docker API timeouts, shared contexts, and network contention no longer turn recoverable image builds into permanent missing artifacts |
| 2026-08-07 | Passed the previously failing `django__django-13925` real Docker pilot, 235 BRT6 tests, official recipe tests, and the 254+22 recovery preflight | Authorize the isolated 22-instance DeepSeek V3 recovery; formal evaluation remains gated on 276/276 artifacts |
| 2026-08-07 | Launched detached SWT-22 recovery `official_docker_swt_deepseekv3_swt22_recovery_20260807_000545` as PID `727419` | Only the 22 missing tests are regenerated; Matplotlib is serialized and the official full evaluation is chained behind the 276/276 artifact gate |

# Inherited BRT5 Experiment History

## 1. Method contract

- Branch: `codex/reproducible-clean`.
- Baseline source: frozen best tag `p0-136-f2p-49.28` at `a167ad6`.
- Immediate measured comparator: 116/276 F2P from `run_setup_trigger_oracle_full276_20260717_035400`.
- Primary objective: recover F2P while keeping the method exactly as agreed with the user.
- Method: lossless `setup/trigger/oracle` BehaviorTarget; iCoRe seed order; real buggy execution; execution-log feedback and repair; full-Issue LLM semantic validation; simple final ranking.
- Explicit exclusions: no surrogate patch generation, execution, validation, ranking, or early stop; no runtime target tracing/coverage; no receiver/MRO or API-boundary inference; no independent oracle-risk score in selection.

## 2. Evaluation contract

- Dataset: `data/issues/swt276_issues.json`, exactly 276 rows.
- Pilot: the fixed five previous failures in `results/runs/p0_lossless_nosurrogate_recovery_20260717/canary5.json`.
- Primary metric: formal gold-patch F2P@1 only.
- Missing generated tests remain in the denominator and count as failures.
- Framework commands must use `/root/conda/ENTER/envs/icore/bin/python`.
- Generated tests and formal buggy/fixed runs must use each project's configured `setup_*` iCoRe environment recorded in generation metadata.
- Generation never receives or reads the gold patch.

## 3. Minimal code change

1. Keep the P0 generation, protocol recovery, mutation, execution-feedback, and repair flow.
2. Keep the lossless three-part BehaviorTarget and original iCoRe ordering.
3. Remove dynamic reachability from executor, verifier, checkpoints, prompts, and ranking.
4. Remove independent oracle-risk scoring from checkpoints and final selection.
5. Keep the LLM verifier over full Issue, candidate, command, real stdout/stderr, BehaviorTarget, ProtocolRecovery, and source context.
6. Rank candidates lexicographically by semantic acceptance, issue alignment, grounded oracle, public behavior, original iCoRe seed rank, and fewer repair rounds.
7. Preserve current environment-manager and isolated formal-evaluator fixes.

## 4. Pilot gate

The five-instance pilot must satisfy all of the following before launching 276:

- IssueRewrite produces 5/5 lossless BehaviorTargets.
- Generation produces 5/5 `final_test.py` and 5/5 summaries.
- Every generation summary records zero surrogate calls and `buggy_only` validation.
- No generation or formal result is an environment/setup/collection error.
- Formal evaluation reports `total_instances=5`; missing generation, if any, is counted in that total.
- Required finite metrics exist: `total_instances`, `f2p_success`, `f2p_fail`, `f2p_at_1_percent`, and `by_status`.
- At least one of the five previous failures becomes `F2P_SUCCESS`; otherwise stop and diagnose rather than spend the 276 budget.

## 5. Full run

- Run ID: `p0_simple_llm_selector_full276_20260717`.
- IssueRewrite and generation use bounded concurrency under the literal `icore` interpreter.
- Formal evaluation starts automatically after generation and evaluates all 276 dataset rows, not only completed generations.
- Formal evaluation computes F2P only (`compute_patch_coverage=false`).
- The full chain is detached after the pilot gate; no continuing monitor is required.
- Durable commands, logs, manifests, environment policy, generation completeness, and formal metrics live under the run directory.

## 6. Stop and retry rules

- Do not change method code during the pilot.
- Retry only for a concrete implementation, environment, or evaluation failure.
- Do not launch 276 on incomplete pilot metrics, denominator drift, system-Python leakage, or zero pilot F2P.
- Once 276 is launched, do not tune code from partial results.

## 7. Revision log

| Date | Change | Reason |
|---|---|---|
| 2026-07-17 | Replaced dynamic-evidence selector design with buggy execution + LLM verifier + simple ranking | Match the user's intended method and keep the P0 recovery controlled |
| 2026-07-18 | Add an environment-only recovery run for the three missing generations, followed by a full 276-row F2P reevaluation | The main run completed at 131/276 with exactly three `MISSING_GENERATION` cases caused by recoverable Conda lifecycle failures |
| 2026-07-18 | Replace mutable shared project environments with immutable dependency templates plus per-instance runtime clones; isolate generated requirement files and unify dependency pins | The three-instance recovery exposed a cross-process `$HOME/requirements.txt` race and deterministic Astropy/Pylint contract drift |
| 2026-07-18 | Restore dependency pins after Conda clone and use the iCoRe project setup command in formal evaluation | Conda clone reintroduced setuptools 80.9 into Astropy runtimes, while the generic formal setup omitted Pylint runtime dependencies |
| 2026-07-18 | Complete the eight-instance canary and denominator-stable result merge | Final canary is 6/8 with zero environment errors; merged result is 132/276 (47.8261%), +1 success over the frozen 131/276 result |
| 2026-07-19 | Add dependency-template integrity validation before the B-machine rerun | The B-machine ablation exposed duplicate metadata, binary/import drift, stale project namespace files, and an incompatible legacy Matplotlib setuptools pin that version-only checks could not detect |

## 8. Missing-generation recovery contract

- Recovery run ID: `p0_simple_llm_selector_env3_recovery_20260718`.
- Scope is limited to `astropy__astropy-6938`, `pylint-dev__pylint-5859`, and `pylint-dev__pylint-7080`.
- Reuse the 276-run IssueRewrite cache, retrieval inputs, generation parameters, framework interpreter, and method code.
- Exercise the existing deterministic recovery branch: remove each incompatible failed hashed environment and recreate it from the original iCoRe environment spec.
- Keep the original generation and formal evaluation directories read-only.
- Write each recovery instance into an isolated raw output directory and preserve its command log.
- Acceptance gate: all three recovery instance directories contain top-level `final_test.py` and `summary.json`, and all summaries report zero surrogate calls.
- On acceptance, create a symlink-only merged generation view containing the original 273 tests plus the three recovered tests, then run the same formal evaluator on all 276 rows with PatchCov disabled.
- If any target still lacks `final_test.py`, stop before formal reevaluation and diagnose that concrete environment recreation failure; do not broaden the retry.

## 9. Environment-isolation implementation contract

- This pass first changed code only; the user subsequently authorized an eight-instance canary run on 2026-07-18.
- Dependency-template environments are validated against the iCoRe dependency contract and are never used for project installation or test execution.
- Generation and formal evaluation each clone the validated template into a deterministic instance/workspace-specific runtime environment; project installs and dependency recovery are confined to that clone.
- Runtime clones are removed after their owning generation/evaluation instance completes, while metadata retains the immutable template identity needed by formal evaluation.
- Requirements heredocs use a worktree-local, environment-specific path; no environment script may read or write `$HOME/requirements.txt` or `/root/requirements.txt`.
- Astropy 1.3 declares `numpy==1.23.5` and `cython<3` in the dependency contract itself. Pylint 2.15 formal setup must not replace the contract's Astroid pin.
- Each generation/evaluation record captures a package-manifest fingerprint and `pip check` result for later reproducibility auditing.

## 10. Eight-instance minimal-environment canary

- Scope: `astropy__astropy-12907`, `astropy__astropy-14182`, `astropy__astropy-14995`, `astropy__astropy-6938`, `pylint-dev__pylint-7114`, `pylint-dev__pylint-7993`, `pylint-dev__pylint-5859`, and `pylint-dev__pylint-7080`.
- Reuse the five existing generated tests; regenerate only the three previously missing tests under the literal iCoRe interpreter.
- Evaluate all eight in clean per-instance runtime clones using their configured project environments.
- Override these eight rows in the completed 276-row formal result; missing generation remains a failure and the denominator remains exactly 276.
- Final run: `p0_minimal_envfix_canary8_20260718_104530`.
- Final canary: 6/8 F2P, 2 `FIXED_FAIL`, zero environment errors.
- Final merged result: 132/276 F2P (47.8261%), with status counts 132 `F2P_SUCCESS`, 125 `FIXED_FAIL`, and 19 `BUGGY_PASS`.

## 11. B-machine environment-integrity repair

- Scope: environment preparation and validation only; generation, selection, F2P, and coverage definitions remain unchanged.
- Detect multiple metadata installations for every dependency named by the iCoRe contract instead of collapsing them into one arbitrary version.
- Import critical binary/build dependencies inside the target Conda environment and compare the imported version and module path with the declared requirement.
- Treat duplicate metadata, import failure, and imported-version drift as repairable dependency-contract failures; uninstall every conflicting copy, purge orphan metadata, and force-reinstall the declared requirement without using the pip cache.
- Repair NumPy/Pandas and NumPy/Cython as coherent pairs when either side fails integrity validation.
- Remove stale metadata, egg-links, editable finders, and namespace `.pth` files for the benchmark project from each disposable clone before project setup.
- Validate Xarray through NumPy/Pandas/Xarray imports, Scikit-learn through NumPy/Cython/sklearn imports, and Matplotlib through pyparsing/matplotlib/pyplot imports.
- Pin legacy Matplotlib 3.1-3.4 to a setuptools release that does not emit the modern `pkg_resources` API deprecation during Matplotlib's warnings-as-errors test startup.
- Acceptance gate: focused unit tests pass under `icore`; real Xarray, Scikit-learn, and Matplotlib template integrity probes complete without metadata, binary-import, or build-interface errors; no 276-row run is launched in this repair pass.

## 12. Five isolated feedback and mutation ablations

- Add mutually exclusive `on|off` controls for mutation, specialized feedback, environment feedback, trigger feedback, and assertion feedback alongside BehaviorTarget.
- Exactly zero or one component may be disabled.  Full mode keeps Patch Coverage; every ablation runs formal F2P only.
- Each result records one normalized ablation signature, method variant, effective feedback routes, mutation-plan call count, and repair-route counts. Resume is allowed only when the complete signature matches.
- `w/o Mutation` shares the full BehaviorTarget cache but hides mutation hints from every downstream prompt and performs no mutation-planner call or mutation artifact write.
- `Generic Iteration` uses one general repair prompt for at most three feedback repairs and does not invoke category-specific setup, trigger, or oracle helpers during those repairs.
- The three feedback ablations disable only their named repair route; environment creation, strict verification, top-3 seed exploration, and final ranking stay fixed.
- Validation uses the same three SWT instances for all five slices, runs IssueRewrite through formal F2P, fixes the denominator at three, and never computes Patch Coverage.
- The implementation pass did not launch a full run. After canary acceptance,
  the user separately authorized review, commit, and remote push.

## 13. Ablation acceptance evidence

- Canary root: `results/runs/ablation_canary3_batch_20260719_204557`.
- All five variants generated 3/3 tests, evaluated a fixed denominator of 3,
  recorded exactly one disabled component, and had zero environment failures.
- F2P results were: `w/o Mutation` 2/3, `Generic Iteration` 2/3,
  `w/o Environment Feedback` 0/3, `w/o Trigger Feedback` 1/3, and
  `w/o Assertion Feedback` 1/3.
- The disabled repair route had zero calls in each slice; `w/o Mutation` made
  zero mutation-plan calls and emitted no mutation prompt or planner artifact.
- F2P-only evaluation no longer writes SWT or TDD coverage placeholders.
- Shell syntax, Python compilation, and all 141 unit tests pass under the
  literal `icore` interpreter. No full 276-row ablation was launched.

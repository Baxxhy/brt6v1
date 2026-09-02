# Fresh-machine reproduction

The repository can prepare a new Debian/Ubuntu Linux machine with Conda already
installed, but API credentials are never stored in Git. Initial setup downloads
framework dependencies, the benchmark repositories, and per-project Conda
environments. Expect substantial network traffic, disk use, and setup time.

## 1. Publish a clean repository

The original Git history contained a tracked multi-key pool. Deleting the file
in a later commit does not delete it from history. Revoke/rotate those keys and
publish a fresh history:

```bash
python scripts/export_clean_repo.py /tmp/brt6-public
cd /tmp/brt6-public
git status
git commit -m "reproducible BRT experiment"
git remote add origin <YOUR_NEW_REPOSITORY_URL>
git push -u origin main
```

Do not push the current historical repository until the old keys have been
revoked or its history has been independently purged and verified.

## 2. Bootstrap a new Linux machine

```bash
mkdir -p BugReproduce
cd BugReproduce
git clone <YOUR_NEW_REPOSITORY_URL> brt6
cd brt6
bash scripts/bootstrap_fresh_swt_server.sh
```

The command above is the canonical SWT setup when Conda is already installed.
It reuses that Conda installation, creates an isolated `brt5_icore` controller
environment under the checkout's sibling `../.brt5-conda`, prepares all 12
benchmark repositories, prewarms all 52 SWT dependency-template environments
with four concurrent workers by default, validates the frozen inputs, and runs
local regression checks.
It does not start the 276-instance experiment.

All managed paths are derived from the checkout. For example, if the repository
is `.../BugReproduce/brt6`, benchmark repositories are placed in
`.../BugReproduce/swe_repos` and Conda environments/packages in
`.../BugReproduce/.brt5-conda`; no machine-specific project path is embedded.
On a cluster where operating-system prerequisites are already installed and
`apt-get` is unavailable, use:

```bash
bash scripts/bootstrap_fresh_swt_server.sh --skip-system-packages --prewarm-workers 8
```

If the project requirements were already installed in an activated Python
3.10+ Conda environment, reuse that controller without reinstalling it:

```bash
bash scripts/bootstrap_fresh_swt_server.sh \
  --skip-system-packages \
  --controller-python "$(command -v python)" \
  --prewarm-workers 8
```

The supplied controller is validated with `pip check` and imports of the
framework dependencies. Benchmark dependency templates remain isolated under
`../.brt5-conda`; the active controller environment is not modified.

The bootstrap does the following:

1. installs Linux build prerequisites with `apt-get`;
2. validates and reuses the existing Conda installation (no second Conda);
3. creates the isolated `brt5_icore` framework environment (the historical name
   is retained for cache/environment compatibility);
4. installs the project-only framework dependencies from `requirements.txt`;
5. prompts for one or more DeepSeek-compatible keys and stores them at
   `.secrets/api_pool.json` with mode `0600`;
6. clones all repositories required by the selected dataset;
7. verifies every `base_commit` and `environment_setup_commit`;
8. prewarms all 52 SWT dependency templates and validates the frozen cache;
9. writes `.bootstrap/use_fresh_swt_server.sh` for every later run.

For both SWT and TDD inputs:

```bash
bash scripts/bootstrap_machine.sh --dataset all
```

`--dataset all` also clones
`https://github.com/IBM/TDD-Bench-Verified.git` beside the BRT repository.

## 3. Configure keys without committing them

Interactive multi-key configuration:

```bash
source .bootstrap/use_fresh_swt_server.sh
"$PYTHON_BIN" scripts/configure_api_keys.py --provider deepseek
"$PYTHON_BIN" scripts/configure_api_keys.py --provider gpt
```

Each command replaces only that provider's entries and preserves the other
provider. The GPT defaults are `https://aigc.x-see.cn/v1` and
`gpt-5.4-mini`. Key input is hidden.

Alternatively copy `config/api_pool.example.json` to
`.secrets/api_pool.json`, replace the placeholder locally, and run:

```bash
chmod 600 .secrets/api_pool.json
```

Environment variables are also supported:

```bash
export DEEPSEEK_API_KEYS='key1,key2,key3'
export DEEPSEEK_BASE_URL='https://api.deepseek.com'
export DEEPSEEK_MODEL='deepseek-v3'

export GPT_API_KEY='your-gpt-key'
export GPT_BASE_URL='https://aigc.x-see.cn/v1'
export GPT_MODEL='gpt-5.4-mini'
```

For a remote machine or cluster, pass these values through its secret manager,
or transfer `.secrets/api_pool.json` separately over an authenticated channel.
Do not put that file in the repository, even when the repository is private.

Select the configured provider explicitly when launching an experiment:

```bash
# Existing behavior and DeepSeek key rotation.
bash scripts/run_p0_simple_llm_selector_full.sh --dataset swt --model deepseek

# GPT endpoint/key pool and gpt-5.4-mini.
bash scripts/run_p0_simple_llm_selector_full.sh --dataset swt --model gpt
```

## 4. Run the full and ablation experiments

The repository contains the frozen 276-instance BehaviorTarget version from
the full-method run `p0_simple_llm_selector_full276_20260717`, whose formal
F2P result was `131/276 = 47.4638%`:

```bash
BEHAVIOR_CACHE=data/behavior_targets/swt/full_method_f2p_47_46_20260717

# Optional independent integrity check before a run.
source .bootstrap/use_fresh_swt_server.sh
"$PYTHON_BIN" scripts/validate_behavior_target_cache.py \
  --cache-dir "$BEHAVIOR_CACHE" \
  --instances-path data/issues/swt276_issues.json \
  --dataset-mode swt \
  --code-retrieval-path retrieval_results/code/code_retrieval_results_gpt.json \
  --test-retrieval-path retrieval_results/test/icore/gpt/related_tests.json
```

Pass `--behavior-target-cache "$BEHAVIOR_CACHE"` to reuse this exact version.
The launcher validates all 276 targets and their dataset/retrieval hashes, then
skips IssueRewrite. If the option is omitted, the launcher performs
IssueRewrite and generates a new BehaviorTarget version for that run.

```bash
# Full B*=<Environment, Trigger, Assertion>
bash scripts/run_swt_experiment.sh \
  --behavior-target on \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Behavior Target
bash scripts/run_swt_experiment.sh \
  --behavior-target off

# w/o Mutation
bash scripts/run_swt_experiment.sh \
  --mutation off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# Generic Iteration (w/o specialized feedback)
bash scripts/run_swt_experiment.sh \
  --specialized-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Environment Feedback
bash scripts/run_swt_experiment.sh \
  --environment-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Trigger Feedback
bash scripts/run_swt_experiment.sh \
  --trigger-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"

# w/o Assertion Feedback
bash scripts/run_swt_experiment.sh \
  --assertion-feedback off \
  --behavior-target-cache "$BEHAVIOR_CACHE"
```

At most one component may be `off` in a command. Every ablation runs formal
F2P only and keeps the complete dataset denominator; the all-on full method
also runs the benchmark-specific coverage metric.

`w/o Behavior Target` is the only experiment that must not receive
`--behavior-target-cache`, because consuming the cache would invalidate that
ablation. The launcher rejects that combination.

The launcher uses the fixed fresh-server runtime contract. Generation runs in
the `brt5_icore` framework environment; every benchmark project runs in its own
iCoRe-derived Conda environment. Missing generated tests remain in the formal
evaluation denominator. Formal output includes F2P and Change Coverage (Delta C).

To install only the BRT6 framework environment without preparing any SWT/TDD
repository or per-instance environment:

```bash
conda create -n icore -y python=3.12 pip
conda run -n icore python -m pip install -r requirements.txt
```

This installs the framework and its transitive Python dependencies only. The
benchmark-project environments are prepared separately by iCoRe when an
experiment actually needs them.

## Limits of “one command”

The machine still needs Debian/Ubuntu Linux, network access, enough disk space, and either
root/sudo for automatic OS-package installation or equivalent build tools
installed beforehand. Secrets cannot be reconstructed from a Git clone; they
must be supplied locally or through a CI secret store.

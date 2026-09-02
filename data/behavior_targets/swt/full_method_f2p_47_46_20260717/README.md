# SWT-276 full-method BehaviorTarget cache (F2P 47.46%)

This directory contains the exact 276 `behavior_target.json` files used by the
full-method run:

```text
p0_simple_llm_selector_full276_20260717
F2P: 131/276 = 47.4638%
```

The source IssueRewrite completed successfully for all 276 instances. The
cache uses `behavior_target.lossless.v1`; `manifest.json` binds every target to
the SWT-276 dataset and the exact code/test retrieval inputs using SHA-256.

Use this cache from the repository root:

```bash
BEHAVIOR_CACHE=data/behavior_targets/swt/full_method_f2p_47_46_20260717

bash scripts/run_p0_simple_llm_selector_full.sh \
  --dataset swt \
  --behavior-target-cache "$BEHAVIOR_CACHE"
```

Use the same `BEHAVIOR_CACHE` value for every BehaviorTarget-enabled ablation.
Do not pass it to `--behavior-target off`. Omitting the cache option intentionally
runs IssueRewrite again and creates a different BehaviorTarget version.

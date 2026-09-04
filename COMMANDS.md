# BRT6 Commands

下面所有 SWT 命令都固定使用第一次实验的 SWT-Bench：

```bash
export SWTBENCH_ROOT=/root/Baxxhy/BugReproduce/brt6/evaluation/vendor/swtbench
```

不要改成 `/root/Baxxhy/BugReproduce/swt-bench`。

## 1. 只生成

修改 `RUN_NAME` 和并发数后执行。该命令只完成 IssueRewrite 和测试生成，不会启动正式评测：

```bash
cd /root/Baxxhy/BugReproduce
export PYTHONPATH=/root/Baxxhy/BugReproduce
export DOCKER_HOST=unix:///run/mutate-docker.sock
export SWTBENCH_ROOT=/root/Baxxhy/BugReproduce/brt6/evaluation/vendor/swtbench
export OFFICIAL_HARNESS_PYTHON=/root/miniconda3/envs/swtbench/bin/python
RUN_NAME=semantic_delta_swt_generate_$(date +%Y%m%d_%H%M%S)
RUN_DIR=/root/Baxxhy/BugReproduce/brt6/results/runs/$RUN_NAME
export TMPDIR=$RUN_DIR/tmp
export XDG_CACHE_HOME=$RUN_DIR/cache
mkdir -p "$RUN_DIR/issue_rewrite" "$RUN_DIR/generation" "$TMPDIR" "$XDG_CACHE_HOME"
/root/miniconda3/envs/icore/bin/python -m brt6.pipeline.run_issue_rewrite \
  --instances_path /root/Baxxhy/BugReproduce/brt6/data/issues/swt276_issues.json \
  --code_retrieval_path /root/Baxxhy/BugReproduce/brt6/retrieval_results/code/code_retrieval_results_gpt.json \
  --test_retrieval_path /root/Baxxhy/BugReproduce/brt6/retrieval_results/test/icore/gpt/related_tests.json \
  --output_dir "$RUN_DIR/issue_rewrite" \
  --llm-provider deepseek \
  --model DeepSeek-V4-Flash \
  --max_workers 20 \
  --temperature 0.1 \
  --max_tokens 4096 && \
BRT4_BEHAVIOR_CACHE_DIR="$RUN_DIR/issue_rewrite" \
/root/miniconda3/envs/icore/bin/python -m brt6.pipeline.run \
  --instances_path /root/Baxxhy/BugReproduce/brt6/data/issues/swt276_issues.json \
  --code_retrieval_path /root/Baxxhy/BugReproduce/brt6/retrieval_results/code/code_retrieval_results_gpt.json \
  --test_retrieval_path /root/Baxxhy/BugReproduce/brt6/retrieval_results/test/icore/gpt/related_tests.json \
  --repo_root_base /root/Baxxhy/BugReproduce/swe_repos \
  --output_dir "$RUN_DIR/generation" \
  --llm-provider deepseek \
  --model DeepSeek-V4-Flash \
  --max_workers 20 \
  --max_semantic_rounds 5 \
  --validation_mode buggy_only \
  --timeout 1800 \
  --temperature 0.1 \
  --max_tokens 4096 \
  --dataset_mode swt \
  --runtime_backend official_docker \
  --official_harness_python /root/miniconda3/envs/swtbench/bin/python \
  --swtbench_root "$SWTBENCH_ROOT"
```

## 2. 只评测

将 `RUN_DIR` 改成已经完成生成的实验目录：

```bash
cd /root/Baxxhy/BugReproduce/brt6
export SWTBENCH_ROOT=/root/Baxxhy/BugReproduce/brt6/evaluation/vendor/swtbench
RUN_DIR=/root/Baxxhy/BugReproduce/brt6/results/runs/替换成实验名称
/root/miniconda3/envs/icore/bin/python scripts/run_official_eval_after_generation.py \
  --dataset swt \
  --outputs-dir "$RUN_DIR/generation" \
  --dataset-file /root/Baxxhy/BugReproduce/brt6/data/official/swt276_official_eval.json \
  --evaluation-dir "$RUN_DIR/evaluation/formal_f2p" \
  --max-workers 20 \
  --timeout 1800 \
  --run-id "$(basename "$RUN_DIR")" \
  --model-name brt6-DeepSeek-V4-Flash \
  --compute-coverage false \
  --official-python /root/miniconda3/envs/swtbench/bin/python \
  --swtbench-root "$SWTBENCH_ROOT" \
  --official-dataset-name /root/Baxxhy/BugReproduce/brt6/data/official/swt276_official_eval.json
```

## 3. 从头生成并评测

这是以后推荐使用的完整命令：

```bash
cd /root/Baxxhy/BugReproduce/brt6
SWTBENCH_ROOT=/root/Baxxhy/BugReproduce/brt6/evaluation/vendor/swtbench \
bash scripts/launch_semantic_delta_full.sh \
  --dataset swt \
  --model deepseek \
  --model-id DeepSeek-V4-Flash \
  --max-rounds 5 \
  --issue-workers 20 \
  --generation-workers 20 \
  --evaluation-workers 20 \
  --generation-timeout 3600 \
  --worktree-timeout 1200 \
  --evaluation-timeout 3600 \
  --gold-dataset /root/Baxxhy/BugReproduce/brt6/data/official/swt276_official_eval.json \
  --f2p-only \
  --allow-dirty \
  --allow-external-model-data
```

该命令会自动创建带时间戳的实验目录和 tmux 会话，依次执行 IssueRewrite、生成、完整性检查和官方 F2P 评测。
启动器固定使用隔离的 `brt6-runs` tmux socket，并禁用 Bash 启动文件。进入控制台使用启动结果打印的命令，格式为 `tmux -L brt6-runs attach -t 会话名`。

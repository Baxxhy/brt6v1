# BRT6 运行说明

项目根目录是 `/root/Baxxhy/BugReproduce/brt6`。脚本从项目根目录解析数据、结果和
日志路径；直接使用 `python -m brt6...` 时，请从它的父目录
`/root/Baxxhy/BugReproduce` 执行，或显式设置 `PYTHONPATH`。

## 只做离线检查（不会调用 LLM 或 Docker）

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash -n scripts/*.sh
cd /root/Baxxhy/BugReproduce
python -m compileall -q brt6
python -m unittest discover -s brt6/tests -p 'test_*.py'
python -m brt6.run --help
python -m brt6.run_issue_rewrite --help
```

## 安装控制器依赖

```bash
cd /root/Baxxhy/BugReproduce/brt6
conda create -n icore -y python=3.12 pip
conda run -n icore python -m pip install -r requirements.txt
```

完整新机器准备（会安装/下载依赖和 benchmark 资源，但不会自动启动 276 条实验）：

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash scripts/bootstrap_fresh_swt_server.sh
```

## 常用运行入口

下面命令会调用模型或 benchmark 运行时，执行前请先配置 API key 和对应资源。

只跑 IssueRewrite：

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash scripts/run_issue_rewrite.sh
```

5 条冒烟生成：

```bash
cd /root/Baxxhy/BugReproduce/brt6
LIMIT=5 bash scripts/run_smoke.sh
```

完整生成：

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash scripts/run_generate.sh
```

已有生成结果的正式评测：

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash scripts/run_evaluate.sh results/runs/<run_name>
bash scripts/run_formal_eval.sh results/runs/<run_name>
```

官方 SWT Docker 评测（直接复用本机缓存镜像）：

```bash
cd /root/Baxxhy/BugReproduce/brt6
RUN_DIR=/root/Baxxhy/BugReproduce/brt6/results/runs/<run_name>
mkdir -p "$RUN_DIR/evaluation/official_docker"
jq '.[0:10]' data/issues/swt276_issues.json \
  > "$RUN_DIR/evaluation/official_docker/instances.json"
python scripts/run_official_eval_after_generation.py \
  --dataset swt \
  --outputs-dir "$RUN_DIR/generation" \
  --dataset-file "$RUN_DIR/evaluation/official_docker/instances.json" \
  --evaluation-dir "$RUN_DIR/evaluation/official_docker" \
  --max-workers 14 \
  --timeout 1800 \
  --run-id <run_name> \
  --official-python /root/miniconda3/envs/swtbench/bin/python \
  --swtbench-root /root/Baxxhy/BugReproduce/brt6/evaluation/vendor/swtbench
```

`--dataset-file` 必须只包含本次已生成的实例；完整性门禁会拒绝缺少
`final_test.py` 的条目。

完整流水线（生成、评测、导出）：

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash scripts/run_full_pipeline.sh
```

官方 SWT 入口会先检查 276 条缓存、官方 harness、Conda 和 Docker：

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash scripts/run_official_swt_full.sh --preflight-only
bash scripts/run_official_swt_full.sh
```

## API key 配置

当前私有仓库使用 `.secrets/api_pool.json` 中的 14-key 池：

```bash
cd /root/Baxxhy/BugReproduce/brt6
python scripts/configure_api_keys.py --provider deepseek
```

也可以设置 `DEEPSEEK_API_KEYS`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`，或使用
`GPT_API_KEY` 等 GPT 配置。若仓库将来改为公开，必须先移除并轮换已跟踪的 key。

## Docker 与临时目录

BRT6 沿用 iCoRe native 的本机 Docker：默认连接
`unix:///run/mutate-docker.sock`，Docker data-root 应为 `/root/lby-docker`。
生成和评测脚本会把 `TMPDIR`、`TEMP`、`TMP` 及 `XDG_CACHE_HOME` 放到对应的
`results/runs/<run_name>/` 下；如需覆盖 Docker socket，可在启动前设置
`DOCKER_HOST`。

SWT 官方 harness 的固定提交已复制到
`evaluation/vendor/swtbench/`，默认运行不再读取项目外的 `swt-bench` 工作区。

SWT 生成和官方评测默认直接使用已存在的 `exec.eval.x86_64.*` 官方镜像，不执行
build、pull 或网络源码 fetch。每个实例使用官方 `ExecSpec` 生成的原始容器名；
容器在评测结束后保留并供后续六状态或重跑复用。cached-image 评测跳过项目
`pip install`，不会升级 pip、setuptools 或项目依赖；仓库复位使用 `git clean -fd`
保留官方镜像中被 Git 忽略的编译产物。

启动前检查 daemon 和已有容器：

```bash
DOCKER_HOST=unix:///run/mutate-docker.sock docker info
DOCKER_HOST=unix:///run/mutate-docker.sock docker ps -a \
  --format '{{.Names}}\t{{.Status}}\t{{.Image}}' | grep '^exec\.eval\.'
```

只有确认某个实例容器已损坏时，才按上一步显示的完整官方名称精确删除；官方镜像
不删除，下一次评测会从已有镜像重建同名容器：

```bash
DOCKER_HOST=unix:///run/mutate-docker.sock docker rm -f <完整官方容器名>
```

仅诊断缺失镜像时才显式设置 `BRT_ALLOW_OFFICIAL_IMAGE_REBUILD=1`；正常批量运行
不要设置。

## 主要参数

- `RUN_NAME`、`RUN_DIR`：结果目录名称或完整路径。
- `INSTANCES_PATH`：issue 数据集；默认 `data/issues/swt276_issues.json`。
- `CODE_RETRIEVAL_PATH`、`TEST_RETRIEVAL_PATH`：iCoRe 检索结果。
- `REPO_ROOT_BASE`：benchmark 仓库缓存，默认项目父目录下的 `swe_repos`。
- `MODEL`、`TEMPERATURE`、`WORKERS`、`TIMEOUT`：模型和并发控制。
- `LIMIT`：仅用于限制生成实例数，适合受控冒烟。
- `BEHAVIOR_TARGET_CACHE`：复用已冻结的 BehaviorTarget 缓存。

每个 run 目录包含 `run_config.json`、`command.txt`、`logs/`、`generation/`、
`evaluation/`、`exports/` 和 `tmp/`。生成脚本会写 `generation.done`，完整流水线会
额外写 `formal_eval.done`、`export.done` 和 `done`。

## 运行边界

BRT6 的 benchmark 项目依赖由官方运行时隔离；控制器依赖只安装
`requirements.txt`。`brt5i_*`、`BRT3_*`、`BRT4_*` 等名称是现有缓存、环境和脚本
兼容标识，本轮没有重命名。

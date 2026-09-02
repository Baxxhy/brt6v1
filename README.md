# BRT6

BRT6 从 issue、iCoRe 已生成的源码/测试检索结果和 buggy 仓库出发，生成单个完整 Bug Reproduction Test。种子测试、候选测试、observation probe 和迭代反馈全部运行在 SWTBench 或 TDDBench 官方 buggy-instance Docker 中；最终评测也使用对应官方 Docker harness。

BRT6 不在宿主机为 benchmark 项目创建、克隆、修复或安装 Conda 环境。宿主机上的 `icore` 只运行 BRT 控制器和 LLM 客户端，官方 harness 的独立 Python 只负责构建/启动官方 image；项目依赖始终封装在 benchmark Docker image 内。生成容器只接收实例标识、仓库、版本和 base/setup commit，不接收 gold code/test patch。

Docker 只缓存共享 `exec.base.*` / `exec.env.*` 环境层；每个实例结束后会删除其容器和精确的 `exec.eval.*` image。生成默认要求 Docker data-root 至少有 120 GiB 可用空间并拒绝 `vfs`（诊断时可用 `BRT_DOCKER_MIN_FREE_GB`、`BRT_REJECT_DOCKER_VFS` 显式覆盖）。只有生成返回成功且数据集每行都有非空 `final_test.py` 时，正式评测才会启动；否则 run 会写入 `generation_gate.json` 并记录评测被跳过。

## 新机器复现

完整的安全导出、自动安装、密钥配置和全量实验说明见
[README_REPRODUCE.md](README_REPRODUCE.md)。真实 API key 不保存在 Git 中。

```bash
bash scripts/bootstrap_official_benchmarks.sh
bash scripts/run_official_swt_full.sh
```

## 快速运行

完整命令、恢复方式、tmux 和日志说明见 [README_RUN.md](README_RUN.md)。

## 项目结构

目录、代码文件和主调用链见 [README_STRUCTURE.md](README_STRUCTURE.md)。

## 最近结果

历史生成/评测结果通过 `scripts/clean_old_results.sh` 管理：普通旧结果只保留最近 3 个，带 `best`、`bestsofar`、`complete` 标记的结果会进入 `results/archive/legacy_best/`。

## 新实验输出

所有新实验统一写入 `results/runs/`；正式评测产物位于每个 run 的 `evaluation/formal_f2p/`，包括官方 predictions、Docker 日志与官方报告。

## 核心导出

每次 run 的完整逐实例结果位于 `exports/all_outputs.json`；另有 JSONL、测试代码简表和总指标摘要。

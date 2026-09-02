# BRT6 当前实现与迁移运行指南

本文档描述本迁移包中 **当前工作树实际代码** 的实现，不以旧版
`README_STRUCTURE.md` 和 `README_RUN.md` 中的当前 BRT6 命名为准；历史文档中的
BRT3/BRT4/BRT5 名称仅用于说明旧版本或兼容标识。

## 1. 系统目标

BRT6 根据 benchmark Issue、iCoRe 已生成的源码/测试检索结果和 buggy
仓库，为每个实例生成一个完整的新测试文件 `final_test.py`。生成阶段不接触
gold code patch、gold test patch、FAIL_TO_PASS 或 PASS_TO_PASS；候选测试只在
官方 benchmark 的 buggy-instance Docker 中执行。全量生成完成后，再调用官方
SWTBench 或 TDDBench Docker harness 计算 F2P 和相应覆盖率指标。

## 2. 迁移包包含与排除的内容

ZIP 包含：

- BRT6 Python 源码和兼容入口；
- `pipeline/`、`generation/`、`execution/`、`validation/`、`runtime/` 等实现；
- 所有 prompt 模板、启动/恢复/诊断脚本和单元测试；
- `requirements.txt` 和环境 bootstrap 脚本；
- SWT/TDD Issue 输入、检索输入和版本化 BehaviorTarget 冻结缓存；
- 项目文档及本迁移指南。

ZIP 明确排除：

- `results/`、`artifacts/`、`output/`、`outputs/`、`tmp/`；
- 日志、Python bytecode、pytest/mypy 缓存；
- `.git/` 历史和本机 bootstrap 状态；
- `.env`、`.secrets/`、API key pool 及任何真实密钥；
- 本机 Conda 环境、Docker image、Docker container；
- 外部的 `swt-bench`、`TDD-Bench-Verified` 和 `swe_repos` checkout。

冻结 BehaviorTarget 位于 `data/behavior_targets/`。它们是正式方法的版本化输入，
不是本机运行产生的 `results/`；保留它们是为了让默认 full-method 命令不必重新执行
IssueRewrite，并能保持输入身份可验证。

## 3. 当前主调用链

正式启动脚本：

```text
scripts/run_official_swt_full.sh
  -> scripts/run_p0_simple_llm_selector_full.sh
     -> brt6.pipeline.run_issue_rewrite  # 没有指定冻结缓存时
     -> brt6.pipeline.run                # 生成
     -> scripts/run_official_eval_after_generation.py
```

核心模块职责：

- `pipeline/run.py`：参数、preflight、多实例线程池、实例级官方 Docker 生命周期；
- `io/io_utils.py`：加载 Issue、检索源码和检索测试，构建 `InstanceContext`；
- `issue/issue_rewriter.py`：把原始 Issue 转成 Setup/Trigger/Oracle 三部分
  `BehaviorTarget`；
- `context/host_context.py`：恢复 seed test 的类、import、setup 和相邻测试上下文；
- `context/protocol_recovery.py`：通过 AST 恢复 fixture、decorator、pytest mark、
  helper、model、conftest 和原生测试命令；
- `mutation/seed_mutator.py`：生成结构化 Trigger Mutation Plan；
- `validation/mutation_plan_validator.py`：验证 plan 中的文件、符号、anchor、
  safety constraint 和 Oracle 隔离；
- `generation/generator.py`：生成完整测试，执行静态语义检查和 mutation adherence
  检查，并按 setup/trigger/oracle/generic 路由修复；
- `runtime/official_docker_runtime.py`：调用官方 harness 构建实例 image、启动容器、
  同步候选增量、执行命令和清理实例 image；
- `execution/executor.py`：把执行分类为 PASS、ISSUE_ALIGNED_FAIL、
  ASSERTION_FAIL、SETUP_ERROR、COLLECT_ERROR、TIMEOUT 等；
- `validation/strict_semantic_verifier.py`：结合 Issue、BehaviorTarget、源码、测试和
  执行结果决定 accept、repair_setup、repair_trigger、repair_oracle 或 reject；
- `execution/feedback.py`：Top-3 seed、自适应切换、反馈循环、checkpoint 排序和
  `final_test.py` 选择；
- `evaluation/official_benchmarks.py`：把 `final_test.py` 转成官方 `model_patch`；
- `scripts/run_official_eval_after_generation.py`：运行官方 SWTBench/TDDBench harness。

## 4. 单实例生成流程

1. 从 Issue 数据和检索 JSON 构建 `InstanceContext`。
2. 校验并读取冻结 BehaviorTarget；未指定缓存时通过 LLM 重新生成。
3. 仅把 instance ID、repo、version、base/setup commit 交给官方 Docker runtime；
   gold patch 字段被显式禁止。
4. 宿主创建 base commit worktree，作为完整源码读取和候选文件 staging 区；项目依赖
   不安装到该 worktree。
5. 根据 BehaviorTarget 对检索测试排序，最多尝试 Top-3 seed。
6. 为 seed 构建 HostContext，并用 AST 恢复 ProtocolRecovery；LLM 只审计风险，
   不替换 AST 事实。
7. 可选地生成并验证 Trigger Mutation Plan。只有 `VALID` plan 会传给生成器；
   若生成测试没有遵循 plan，则保存不合规候选并降级为无 plan 直接生成。
8. 生成完整的新测试文件，并做执行前语义检查。
9. 每次执行前，容器内仓库 reset/clean 到 base commit；宿主 worktree 中的候选增量
   通过 `docker cp` 同步到 `/testbed`，再在官方容器环境中执行。
10. 环境错误走 Environment Feedback；没有触发目标走 Trigger Feedback；Oracle
    不正确走 Assertion Feedback。默认最多 2 个环境轮和 3 个 BRT 轮。
11. 每轮保存 checkpoint。排序首先要求可执行、buggy fail、Oracle 可证伪且没有
    语义/plan/Oracle-preservation 违规，然后考虑 verifier accept、target hit、
    issue-grounded Oracle 和修复轮次。
12. 选择最佳 seed/round，写出 `generation/<instance_id>/final_test.py`。

生成阶段固定为 `validation_mode=buggy_only`，不生成 surrogate patch，也不做伪造的
双版本验证。真正的 fixed-side 结果由后面的官方 harness 使用 gold patch 得出。

## 5. Generation Gate 与正式评测

只有同时满足以下条件，正式评测才会启动：

- 生成进程返回 0；
- 数据集中每个实例都有非空 `final_test.py`。

否则脚本写入 `generation_gate.json` 并跳过正式评测。评测阶段把每个测试转换成
“新增测试文件”的 Git patch，调用官方 SWTBench 或 TDDBench Docker harness。
主要产物为 `official_predictions.json`、`official_run_manifest.json`、
`official_report.json` 和 `metrics.json`。

## 6. 新服务器目录布局

建议保持以下同级布局，因为脚本按 `brt6` 的父目录寻找外部仓库：

```text
BugReproduce/
  brt6/                  # 本 ZIP 解压得到
  swt-bench/             # 官方仓库，外部 clone
  TDD-Bench-Verified/    # 官方仓库，外部 clone
  swe_repos/             # bootstrap 根据数据集准备的 buggy 源码缓存
```

解压示例：

```bash
mkdir -p /path/to/BugReproduce
cd /path/to/BugReproduce
unzip /path/to/brt6_portable_source_*.zip
cd brt6
```

## 7. 外部仓库和环境准备

目标机需要 Linux、网络、Git、Conda/Miniforge、Docker daemon，以及足够磁盘空间。
官方生成 preflight 默认要求 Docker data-root 至少 120 GiB 可用，并拒绝 `vfs`
storage driver。

先准备官方仓库：

```bash
cd /path/to/BugReproduce
git clone https://github.com/logic-star-ai/swt-bench.git swt-bench
git clone https://github.com/IBM/TDD-Bench-Verified.git TDD-Bench-Verified
cd brt6
```

然后准备控制器、官方 harness 环境和 `swe_repos`：

```bash
bash scripts/bootstrap_machine.sh --dataset all
```

bootstrap 会创建或更新：

- `icore`：Python 3.12 BRT 控制器环境；
- `brt6_swtbench`：官方 SWTBench harness 环境；
- `brt6_tddbench`：官方 TDDBench harness 环境；
- 父目录的 `swe_repos/`。

控制器固定依赖见 `requirements.txt`。benchmark 项目依赖位于官方 Docker image，
不应安装进宿主 `icore` 环境。

## 8. API 密钥配置

真实密钥不在 ZIP 中。交互式配置：

```bash
/root/conda/ENTER/envs/icore/bin/python scripts/configure_api_keys.py --provider deepseek
/root/conda/ENTER/envs/icore/bin/python scripts/configure_api_keys.py --provider gpt
```

如果目标机的 Conda 不在 `/root/conda/ENTER`，用下面的方式取得 Python：

```bash
CONDA_BASE=$(conda info --base)
"$CONDA_BASE/envs/icore/bin/python" scripts/configure_api_keys.py --provider deepseek
```

也可以使用 `DEEPSEEK_API_KEYS`、`GPT_API_KEYS`、`DEEPSEEK_BASE_URL`、
`GPT_BASE_URL` 等环境变量。不要把 `.secrets/api_pool.json` 提交或再次打包。

## 9. 启动命令

检查 SWT full-method 环境和冻结输入：

```bash
bash scripts/run_official_swt_full.sh --preflight-only
```

启动 SWT full-method：

```bash
bash scripts/run_official_swt_full.sh
```

通用 SWT/TDD 和消融入口：

```bash
bash scripts/run_p0_simple_llm_selector_full.sh --dataset swt --model deepseek
bash scripts/run_p0_simple_llm_selector_full.sh --dataset tdd --model deepseek
```

运行结果统一写入 `results/runs/`；该目录没有包含在迁移 ZIP 中。

## 10. 迁移后的快速自检

```bash
CONDA_BASE=$(conda info --base)
PYTHON_BIN="$CONDA_BASE/envs/icore/bin/python"
PACKAGE_ROOT=$(cd .. && pwd)

PYTHONPATH="$PACKAGE_ROOT" "$PYTHON_BIN" -m compileall -q .
PYTHONPATH="$PACKAGE_ROOT" "$PYTHON_BIN" -c \
  'import brt6; from brt6.pipeline.run import build_parser; print("brt6_import=ok")'

# pytest 不属于精简的 icore requirements；可临时安装，或使用已带 pytest 的
# brt6_tddbench Python 运行当前官方 Docker 路径的回归测试。
TDD_PYTHON="$CONDA_BASE/envs/brt6_tddbench/bin/python"
PYTHONPATH="$PACKAGE_ROOT" "$TDD_PYTHON" -m pytest -q \
  tests/test_official_benchmarks.py \
  tests/test_official_generation_runtime.py \
  tests/test_swtbench_runtime_compat.py
```

正式运行前还应执行 `run_official_swt_full.sh --preflight-only`。Python 编译成功只说明
源码完整，并不能替代 Docker、官方仓库、API pool、数据哈希和磁盘空间检查。

## 11. 当前快照注意事项

本迁移包是当前工作树快照，而不是只导出 Git HEAD。打包时工作树包含未提交的正式
Docker runtime、评测和反馈逻辑修改；迁移包保留这些实际文件。为保证结果可追溯，
运行产生的 `run_config.json` 会记录 Git commit、dirty 状态、数据集 SHA256、生成
runtime contract SHA256 和 evaluator contract SHA256。

旧结果、持久化环境标识和部分历史文档中仍存在 BRT3/BRT4/BRT5 命名；当前正式实现应以
`scripts/run_p0_simple_llm_selector_full.sh`、`pipeline/run.py`、
`runtime/official_docker_runtime.py` 和本指南为准。

测试目录现在统一导入 `brt6.*`。从工作区父目录执行
`python -m unittest discover -s brt6/tests -p 'test_*.py'`，即可验证当前包；
不需要依赖旁边 checkout 中的其他 BRT 版本。

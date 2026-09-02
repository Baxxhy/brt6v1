# BRT6 Docker 与 F2P 可靠性加固实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 BRT6 复用官方缓存容器、完全离线评测、兼容旧 pytest，并阻止已知类别的无效生成测试。

**架构：** Docker 策略集中在 BRT6 runtime shim，不修改外部 SWT-Bench；pytest fallback 作为官方 parser 的保守补充；生成语义门禁作为候选 hard-eligibility 的确定性前置条件。所有标识使用可读字段，不新增哈希或 SHA256。

**技术栈：** Python 3、Docker SDK、unittest、AST、官方 SWT-Bench runtime shim。

---

## 文件结构

- 修改 `runtime/swt_cached_compat.py`：cached-image 离线 eval 命令策略。
- 修改 `evaluation/swtbench_runtime_compat.py`：容器复用配置、健康检查和 pytest fallback 注册。
- 修改 `scripts/run_official_eval_after_generation.py`：为官方评测传入复用和离线环境。
- 修改 `validation/semantic_guard.py`：限定 API、默认配置和无依据类型前置断言门禁。
- 修改 `execution/feedback.py`：确定性风险进入 hard-eligibility。
- 修改 `tests/test_swtbench_runtime_compat.py`：Docker 和 parser 回归。
- 修改 `tests/test_feedback_ablations.py`：语义门禁和排序回归。
- 修改 `README.md`：运行及容器复用说明。

### 任务 1：cached-image 离线评测

- [ ] 编写失败测试：给含 `pip install -e .[test]` 的 eval 命令，期望输出删除项目重装、保留测试与状态复位命令，并设置离线变量。
- [ ] 运行 `python -m unittest tests.test_swtbench_runtime_compat -v`，确认因缺少离线转换 API 失败。
- [ ] 在 `runtime/swt_cached_compat.py` 实现 eval 命令转换，并在 shim 安装时包装 `ExecSpec.eval_script_list`。
- [ ] 重新运行测试并确认通过。

### 任务 2：可读名称的实例容器复用

- [ ] 编写失败测试：相同实例六状态得到同一可读容器名；名称只含固定前缀和实例 ID；损坏容器只重建该容器；不同实例名称不同。
- [ ] 运行目标测试，确认当前 shim 未设置复用契约而失败。
- [ ] 在 `evaluation/swtbench_runtime_compat.py` 配置 `SWT_REUSE_CONTAINERS=1`、实例作用域和 `/root` 锁目录，增加复用前健康检查与精确恢复。
- [ ] 在 `scripts/run_official_eval_after_generation.py` 写入同样环境契约和 manifest。
- [ ] 运行目标测试并确认通过。

### 任务 3：旧 pytest 单测试 fallback

- [ ] 编写失败测试：`collected 1 item` + `file.py F`/`1 failed` 解析为 FAILED；对应 `file.py .`/`1 passed` 解析为 PASSED；多测试混合结果返回空。
- [ ] 运行目标测试，确认当前官方 parser 返回空。
- [ ] 在 `evaluation/swtbench_runtime_compat.py` 实现保守 fallback，并替换 parser map 中 Astropy/pytest-v2 入口。
- [ ] 运行目标测试并确认通过。

### 任务 4：生成测试确定性语义门禁

- [ ] 编写失败测试：Issue 指定 `models.FilePathField` 而候选导入 forms 字段时拒绝；默认配置场景显式覆盖 `FILE_UPLOAD_PERMISSIONS=None` 时拒绝；Issue 未承诺 chararray 类型却先断言该类型时标记高风险。
- [ ] 运行目标测试，确认三个无效候选当前仍 hard-eligible。
- [ ] 在 `validation/semantic_guard.py` 实现 AST/Issue 证据检查，返回可执行修复指令。
- [ ] 在 `execution/feedback.py` 确保该风险优先于 LLM accept 和 seed 排序。
- [ ] 运行目标测试并确认通过。

### 任务 5：完整验证和文档

- [ ] 运行 `python -m unittest discover -s tests -v`，确认零失败。
- [ ] 运行 compile 检查，确认所有修改模块可导入。
- [ ] 使用现有缓存镜像执行一个 Astropy 新版状态 canary，确认日志中没有 `pip install`、没有网络访问。
- [ ] 对现有 10 个实例重新执行官方评测，确认容器复用、状态隔离和 7746 fallback。
- [ ] 更新 `README.md`，写明常驻容器检查、复用和精确清理命令。

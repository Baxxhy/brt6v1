# 官方生成容器常驻复用实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 SWT-Bench 生成与评测按实例共享官方原名常驻容器，并准确记录生成并发。

**架构：** 新增一个带文件锁和原子写入的容器登记模块，生成 helper 和官方评测兼容层都通过它解析、验证和登记容器。生成 runtime 在正常关闭时保留容器和镜像；实例锁继续避免同一容器并发写入。

**技术栈：** Python 3、Docker SDK、`fcntl`、JSON、现有 unittest/pytest。

---

### 任务 1：实现官方容器登记与生成复用

**文件：**
- 创建：`runtime/official_container_registry.py`
- 修改：`scripts/official_generation_container.py`
- 修改：`runtime/official_docker_runtime.py`

- [ ] 实现项目内 JSON 登记表的加锁读取、原子写入、官方名称校验、唯一候选收养和严格兼容性检查。
- [ ] 让 SWT cached/rebuild 生成路径优先复用登记容器，首次创建使用官方 `ExecSpec` 原名并登记。
- [ ] 将生成正常关闭改成保留容器与镜像，并在运行清单记录复用和持久化状态。
- [ ] 运行 `python -m py_compile runtime/official_container_registry.py scripts/official_generation_container.py runtime/official_docker_runtime.py`，预期退出码 0。
- [ ] 提交任务 1。

### 任务 2：让官方评测共享登记容器

**文件：**
- 修改：`evaluation/swtbench_runtime_compat.py`

- [ ] 在已有实例锁内，通过登记模块解析已登记官方名称。
- [ ] 复用时严格检查官方镜像和容器状态；首次由评测创建成功后登记官方原名。
- [ ] 删除自动替换不兼容容器的行为，冲突时明确失败。
- [ ] 运行 `python -m py_compile evaluation/swtbench_runtime_compat.py`，预期退出码 0。
- [ ] 提交任务 2。

### 任务 3：修复并发元数据并验证

**文件：**
- 修改：`pipeline/run.py`
- 修改：`tests/test_official_generation_runtime.py`
- 修改：`tests/test_swtbench_runtime_compat.py`

- [ ] 在 `run_config.json` 写入实际 `max_workers`，在 `summary.json` 分开记录配置值与默认值。
- [ ] 补充登记、复用、持久关闭和元数据回归测试。
- [ ] 运行相关测试，预期全部通过且失败数为 0。
- [ ] 运行完整测试与 `git diff --check`；不启动生成或评测。
- [ ] 提交任务 3。

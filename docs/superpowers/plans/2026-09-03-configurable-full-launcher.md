# 可配置全量实验启动器实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 在当前会话内执行；用户明确禁止子代理。

**目标：** 新增可调参数的统一后台启动命令，并用它启动 SWT 276 完整生成与官方 F2P 评测。

**架构：** 启动器只解析参数并映射到现有正式流水线，不复制算法逻辑。现有流水线增加一个保持默认值为 5 的 `MAX_SEMANTIC_ROUNDS` 环境入口。

**技术栈：** Bash、tmux、现有 BRT6 正式生成与官方 Docker 评测脚本。

---

### 任务 1：开放最大轮数配置

**文件：**
- 修改：`scripts/run_p0_simple_llm_selector_full.sh`

- [ ] 定义 `MAX_SEMANTIC_ROUNDS=${MAX_SEMANTIC_ROUNDS:-5}`。
- [ ] 校验其为 1 到 5 的整数。
- [ ] 将生成命令的硬编码 `--max_semantic_rounds 5` 替换为该变量。

### 任务 2：实现统一后台启动器

**文件：**
- 创建：`scripts/launch_semantic_delta_full.sh`

- [ ] 解析数据集、模型、模型 ID、最大轮数、三个并发数、覆盖率、运行名和会话名。
- [ ] 要求显式提供外部模型数据授权，并让脏工作区继续保持显式授权。
- [ ] 校验路径、正整数、轮数范围、运行目录和 tmux 会话。
- [ ] 使用 shell 安全转义构造 tmux 命令，打印运行目录与日志位置。

### 任务 3：静态验证并启动

**文件：**
- 检查：`scripts/launch_semantic_delta_full.sh`
- 检查：`scripts/run_p0_simple_llm_selector_full.sh`

- [ ] 运行 `bash -n` 检查两个脚本语法。
- [ ] 运行启动器 `--help` 检查接口文本。
- [ ] 使用 DeepSeek-V4-Flash、5 轮、6/6/6 并发、F2P-only、SWT 276 启动后台任务。
- [ ] 只检查 tmux 会话、运行目录和首个流水线阶段标记，确认启动正常。

# Delta 闭环固化实现计划

> **面向 AI 代理的工作者：** 在当前会话内直接实施；不运行算法、pytest、Docker 或官方评测。只做静态检查。

**目标：** 让单 Delta 变异的实施证据、PASS 后路由和静态规则边界与执行反馈算法一致。

**架构：** Delta 实施分析只比较父候选与新候选的可观察 AST 片段，并保存为遥测，不参与拒绝或重试。PASS 交由 Strict Verifier 根据 Issue、当前测试和源码判断 Trigger/Observation 残余差异。旧静态语义审计不再覆盖 Verifier 或决定 checkpoint 硬资格。

**技术栈：** Python dataclass、AST、现有 Strict Verifier、JSON 轨迹。

### 任务 1：记录 Delta 实施证据

**文件：**
- 修改：`validation/delta_guard.py`
- 修改：`core/schema.py`
- 修改：`generation/generator.py`
- 修改：`execution/feedback.py`

- [ ] 比较父/子候选的 setup、非 Oracle 调用、Oracle AST 片段。
- [ ] 记录请求维度是否观察到、额外改变维度和分析限制。
- [ ] 将记录写入 Candidate、Checkpoint 和 FinalResult；不改变候选生成、执行或选择结果。

### 任务 2：让 PASS 进入语义路由

**文件：**
- 修改：`validation/strict_semantic_verifier.py`

- [ ] 保留 setup/syntax/collect/timeout 的确定性结果。
- [ ] 移除 PASS 的固定 `repair_trigger` 映射，使其进入现有 LLM Strict Verifier，并由其输出 Trigger 或 Observation 残余差异。

### 任务 3：静态语义审计降级为软遥测

**文件：**
- 修改：`execution/feedback.py`
- 修改：`validation/strict_semantic_verifier.py`

- [ ] 从 checkpoint 的 hard eligibility 中移除 `audit_candidate()` 结论。
- [ ] 不再让 `audit_candidate()` 覆盖 Strict Verifier 结论。
- [ ] 保留 Oracle 合法性和风险字段作为选择器的软证据。

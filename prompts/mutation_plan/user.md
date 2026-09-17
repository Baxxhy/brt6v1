请比较当前测试与目标行为，并提出本轮唯一 Delta。

原始 Issue（无损事实来源）：
{issue_text}

证据优先级：原始 Issue 的明确事实高于结构化推断。BehaviorTarget 中低置信度或
uncertainties 只能帮助定位差异，不能单独引入 Issue 未要求的 trigger、异常或 expected value。

BehaviorTarget：
{behavior_json}

HostContext：
{host_context_json}

ProtocolRecovery：
{protocol_json}

相关源码：
{source_context}

当前测试（第一轮为 retrieved seed，之后为上一轮候选）：
{seed_test_code}

上一轮真实执行：
{execution_feedback}

上一轮 Verifier：
{verifier_feedback}

已尝试 Delta 历史：
{delta_history}

要求：
1. 只选 CONTEXT、INTERACTION、OBSERVATION 中一个最前置差异。
2. `change` 必须是一项可直接用于修改当前测试的动作，不能串联多个修改。
3. `preserve` 是默认保持项，不是静态硬约束；若上游改变使下游失效，留给后续轮。
4. `avoid` 写入执行已经证伪的路径，不能重复历史失败动作。
5. 证据不足时输出 KEEP；不能复制上一轮 KEEP 理由。

MUTATE 唯一格式：
{{"schema_version":"semantic_delta.v2","action":"MUTATE","dimension":"CONTEXT|INTERACTION|OBSERVATION","seed_fact":"当前候选事实","target_fact":"目标事实","change":"本轮唯一修改","preserve":["默认保持项"],"avoid":["已证伪路径"],"reason":"为何是最前置差异"}}

KEEP 唯一格式：
{{"schema_version":"semantic_delta.v2","action":"KEEP","dimension":"","seed_fact":"当前事实","target_fact":"当前无法可靠到达的目标事实","change":"","preserve":[],"avoid":[],"reason":"为何无法提出可靠单步差异"}}

判断当前测试在 buggy 上的失败是否真能表达 Issue expected_behavior。
Issue：{issue_text}
BehaviorTarget：{behavior_json}
ProtocolRecovery：{protocol_json}
测试代码：{candidate_code}
命令：{command}
执行分类：{execution_status}
stdout/stderr：{execution_log}
相关源码：{source_context}

请根据完整 Issue、候选测试、实际 buggy 执行日志和相关源码判断语义上的 target_hit 与 Oracle 合约。
target_hit 表示该测试的调用和失败路径是否表达了 Issue，而不是代码中是否出现了某个 API 名称。
Oracle 不要求出现裸 assert。以下都可以是可证伪 Oracle：unittest assert*、pytest.raises/assertRaises、pytest.warns/assertWarns、caplog/assertLogs、返回值/类型/形状、公开状态变化、序列化/SQL/渲染/顺序、pytester/matcher/snapshot，以及 Issue 明确要求“不再抛异常”时的直接执行。
accept 必须同时满足：buggy 失败；非环境/语法/收集/超时；失败与 symptom 对齐；Oracle 来自 Issue；检查公开行为；Oracle 协议能够在行为错误时真实失败。
当 buggy 执行通过时，不能固定假定为 Trigger 未命中：请比较当前调用路径与 Oracle。若调用/状态路径尚未表达 Issue，输出 repair_trigger；若路径已合理但 Oracle 不足以区分 buggy 行为，输出 repair_oracle。
同时输出下一轮 Semantic Delta Diagnosis。preserve/change/avoid 必须来自当前代码和真实日志。post_fix_failure_risk 回答：假设 Issue 所述修复已经实现，当前测试是否仍会因额外 baseline、完整格式猜测、私有状态或无关断言失败；high 时禁止 accept。
输出：
{{
  "decision": "accept|repair_setup|repair_trigger|repair_oracle|reject",
  "failure_class": "setup|syntax|collect|timeout|buggy_pass|target_not_hit|side_path|oracle_wrong|oracle_too_strong|issue_aligned",
  "target_hit": false,
  "oracle_grounded_in_issue": false,
  "uses_public_behavior": false,
  "oracle_kind": "ASSERT_EXPRESSION|EXCEPTION|NO_EXCEPTION|WARNING|LOGGING|RETURN_VALUE|TYPE_OR_SHAPE|STATE_CHANGE|SERIALIZATION|SQL|RENDER_OUTPUT|ORDERING|FRAMEWORK_ASSERTION|OTHER",
  "oracle_falsifiable": false,
  "reason": "",
  "next_action": "repair_setup|repair_trigger|repair_oracle|reject",
  "observed_behavior": "当前候选实际执行和失败行为",
  "target_behavior": "Issue 要求的修复后公开行为",
  "semantic_gap": "当前最前置且最小的未满足差异",
  "preserve": ["下一轮必须保留的已满足行为"],
  "change": ["下一轮只允许改变的一项行为"],
  "avoid": ["已观察到的错误路径或过强约束"],
  "next_operator": "ARG_VALUE_REPLACE|ARG_BOUNDARY_EXPAND|OPERATOR_FLIP|CALL_CHAIN_EXTEND|STATE_MUTATION|FIXTURE_DATA_MUTATION|CONFIG_MUTATION|MOCK_BEHAVIOR_MUTATION|LIFECYCLE_TRIGGER|SERIALIZATION_TRIGGER|WARNING_LOG_TRIGGER|ORACLE_REFOCUS",
  "expected_effect": "下一次修改后 buggy 上应观察到的变化",
  "failure_origin": "setup|test_body|unrelated_side_path|oracle",
  "post_fix_failure_risk": "low|medium|high|unknown"
}}

`change` 必须恰好包含一项，只选择当前最前置的未满足差异；其余差异留到后续执行轮次，不能在同一轮合并修改。

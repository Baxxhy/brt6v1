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
  "next_action": "repair_setup|repair_trigger|repair_oracle|reject"
}}

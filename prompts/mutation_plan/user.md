根据完整 Issue 证据、BehaviorTarget Trigger、单一 seed、相关源码和测试协议，生成一个仅修改 Trigger 的最小计划。

BehaviorTarget：{behavior_json}
HostContext：{host_context_json}
ProtocolRecovery：{protocol_json}
相关生产代码：
{source_context}

当前相似测试 seed：
{seed_test_code}

上一轮真实执行反馈：{execution_feedback}
Verifier 反馈：{verifier_feedback}

允许的 op 只有：
- ARG_VALUE_REPLACE
- ARG_BOUNDARY_EXPAND
- OPERATOR_FLIP
- CALL_CHAIN_EXTEND
- STATE_MUTATION
- FIXTURE_DATA_MUTATION
- CONFIG_MUTATION
- MOCK_BEHAVIOR_MUTATION
- LIFECYCLE_TRIGGER
- SERIALIZATION_TRIGGER
- WARNING_LOG_TRIGGER

选择规则：
1. 初始轮只规划 Issue 明确要求、且 seed 尚未包含的最小 Trigger 差异。
2. buggy PASS 或 target_not_hit 时，使用执行日志和 Verifier 指出的具体缺口；不要重新规划 Oracle。
3. 若 seed 已经包含正确 Trigger，只需 Oracle 变化，则返回 ABSTAIN，由后续 Oracle 模块处理。
4. 每个 step 的 before、after 和 seed_anchor 必须具体；seed_anchor/before 必须逐 AST 对应 seed 中真实存在的代码，包括常量和关键字参数，不能写“修改输入”“调用目标 API”等泛化描述。
5. 如果无法同时给出真实文件、符号和 seed anchor，返回 ABSTAIN。

有安全计划时只输出：
{{
  "status": "PROPOSED",
  "trigger_goal": "",
  "steps": [
    {{
      "op": "ARG_VALUE_REPLACE",
      "target_file": "真实/相对/路径.py",
      "target_symbol": "真实类或函数符号",
      "seed_anchor": "seed 中真实存在的代码表达式",
      "before": "seed 中的原值或原调用",
      "after": "Issue 要求的新值或新调用",
      "rationale": "为什么该修改会进入目标路径",
      "risk": "low|medium"
    }}
  ],
  "preserve_from_seed": [],
  "why_target_will_be_reached": ""
}}
没有安全计划时只输出：
{{
  "status": "ABSTAIN",
  "trigger_goal": "",
  "steps": [],
  "preserve_from_seed": [],
  "why_target_will_be_reached": "证据不足的具体原因"
}}

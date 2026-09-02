根据执行结果判断下一步。输出字段 decision, reason, focus, next_action。
decision 取 accept, repair_setup, repair_trigger, repair_oracle, reject。

判定流程：
1. 核对候选是否执行 Issue 描述的 target API、输入和调用路径。
2. 核对失败现象是否与 error_symptom 一致。
3. 核对观察协议是否表达 expected_behavior，并在 buggy 版本上自然失败。
4. 将 import、fixture、collection、syntax 和 runner 问题路由到 repair_setup。
5. 将路径、输入、参数、状态或调用顺序问题路由到 repair_trigger，并在 next_action 中指出具体缺失条件。
6. 将异常类型、warning 类别、logger、matcher、snapshot、期望值或观察对象问题路由到 repair_oracle。
7. 仅在失败路径、观察协议与 Issue 同时对齐时选择 accept。

Issue：{issue_text}
行为目标：{behavior_json}
Top-3 参考上下文：{host_context_json}
相关 buggy 源码：{source_context}
测试代码：{candidate_code}
执行结果：{execution_json}

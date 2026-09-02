请对当前 Bug Reproduction Test 进行一次普通迭代。不要假设反馈已经被正确分类；需要同时检查可执行上下文、Issue 触发路径和最终断言。

必须返回一个可独立收集的完整 Python 测试文件，并且只能包含一个测试入口。
优先保留已经可运行且与 Issue 对齐的部分，只修改真实执行日志和 verifier 反馈能够支持的问题。
不得安装依赖、修改生产代码、读取真实补丁、增加 skip、吞异常、添加恒真断言或把 buggy 行为写成期望行为。

完整 Issue：{issue_text}
行为证据：{behavior_json}
HostContext：{host_context_json}
ProtocolRecovery：{protocol_json}
相似测试种子：{seed_test_code}
相关源码：{code_context}
当前测试：{candidate_code}
执行命令：{command}
执行分类：{execution_status}
执行日志：{execution_log}
Verifier 反馈：{verifier_feedback}

综合以上所有信息完成一次修复，只输出完整 Python 代码。

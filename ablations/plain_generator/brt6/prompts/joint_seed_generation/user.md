请按照 Top-3 联合参考流程生成 1 个 Bug Reproduction Test。

实例：{instance_id}
安全测试名后缀：{safe_instance_id}
放置策略：{insert_strategy}

【完整 Issue】
{issue_text}

【行为目标与原始证据】
{behavior_json}

【主测试协议与宿主上下文】
{host_context_json}

【按 iCoRe 相关性排序的三份参考测试】
{reference_seed_bundle}

【相关 buggy 源码】
{code_context}

【已有执行反馈】
{feedback}

生成步骤：
1. 使用 rank=0 参考测试的测试框架、imports、fixtures、类上下文、运行方式和放置位置。
2. 综合 rank=0、rank=1、rank=2 中与 Issue 触发条件一致的输入、状态和 API 使用证据。
3. 构造执行 Issue 所述缺陷路径的最小测试场景。
4. 将 Issue 的 expected behavior 表达为公开、稳定且可证伪的检查。
5. 返回一个包含单一测试入口的完整 Python 文件。

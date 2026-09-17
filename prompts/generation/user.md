请基于相似测试做最小变异，生成 1 个 BRT。

原始 Issue（无损事实来源）：
{issue_text}

证据优先级：原始 Issue 中明确写出的输入、调用、现象和预期是硬约束；
BehaviorTarget 用于整理这些事实并补充仓库证据，但其中低置信度或 uncertainties
只能作为待验证假设，不能覆盖、缩窄或改写原始 Issue 的明确事实。

实例：{instance_id}
测试函数名必须是：test_brt_{safe_instance_id}
放置策略：必须生成一个完整的新 Python 测试文件，后续会保存到最相似测试同级目录下的 test_brt_{safe_instance_id}.py。
不要输出需要插入到已有 class 或已有文件中的 method 片段。
不要输出裸缩进代码。
如果需要 class wrapper，请在新文件中完整定义 class，并包含必要 imports。
完整文件必须能作为独立 .py 文件被 pytest/Django/Sympy 测试命令收集；不能依赖原相似测试文件里已经 import 的名字、模型、fixture helper、全局变量或 class，除非你在新文件中显式 import 或定义。
如果复用相似测试中的 TestCase、fixture、helper、model、decorator、pytestmark 或断言风格，必须把运行所需的 import/decorator/setup 一并写入新文件。
如果 HostContext.setup_context 包含类级属性（例如 CHECKER_CLASS、databases、app_label、配置常量），必须保留其精确名称和值，不得根据测试名称猜测替代类。
不得引入相似测试、完整宿主文件、相关源码或 Python 标准库中没有依据的第三方模块。
必须服从 HostContext 中的实际 runner：如果 runner 是 SymPy bin/test，不要默认 import pytest 或使用 pytest fixture；如果 runner 是 Django runtests，使用现有 Django TestCase 与测试应用约定。
测试的断言必须表达 expected_behavior，而不是期待 buggy error_symptom 继续发生。
完整文件只能包含一个可收集的 test 函数或 test method；不要生成 baseline、对照组、
备用候选或多个测试入口，因为评测只执行这个唯一 BRT。
不得使用 pytest.skip、skipIf、skipUnless 或平台不满足时提前 return；测试必须真实执行。
不得使用 assert True、A or not A、x == x or x != x 等恒真断言。
不得用 broad try/except 吞掉异常，不得 mock/patch 掉 target API 或 Issue 要观察的内部行为。
Issue 给出 MWE、字面输入、参数、operator、调用顺序时必须原样保留这些路径锚点；
不能为了让测试可运行而换成更简单但不触发缺陷的输入或 API。
如果缺陷是“缺少检查、缺少 warning、缺少状态更新或静默接受错误配置”，不要寻找一个
buggy 版本已经存在的异常路径。应调用项目真实的检查/验证/状态转换 API，并断言修复后
应出现的稳定证据；buggy 版本因证据缺失而自然 assertion fail。
如果缺陷是“缺少日志”，使用 assertLogs/caplog 观察日志；只有 Issue 或相关源码明确给出
logger 名称时才绑定该名称。名称不确定时使用不指定 logger 的根捕获，或 patch
logging.Logger.exception 这类已存在的公共日志方法；不得根据模块文件路径猜测 logger 名称，
也不要 patch buggy 源码中尚不存在的 logger 属性。
只有 expected_behavior 明确要求抛异常时才使用 assertRaises/pytest.raises。
BehaviorTarget.trigger.safety_constraints 是硬约束，优先级高于 mutation_hints。
Issue 明确描述为可接受、可解析或成功的输入，不得重新解释为 invalid input，也不得放入
assertRaises/pytest.raises。若目标是验证错误消息，必须沿用 iCoRe seed 中真正无效的输入，
只把 oracle 改为修复后的稳定消息片段。
行为目标：{behavior_json}
HostContext：{host_context_json}
相关源码：{code_context}
当前测试代码（这是本轮唯一父候选）：{seed_test_code}
上一轮反馈：{feedback}

若附带 Semantic Delta，只实施其中 `change` 描述的一项改变。CONTEXT 只改变前置状态或输入，INTERACTION 只改变调用/顺序/状态迁移，OBSERVATION 只改变 Issue 支持的公共行为断言。`preserve` 是默认保持项；若上游改变令下游失效，本轮不要顺手修复，交给下一轮执行反馈。不得重新从 seed 或 Issue 整体生成另一条路线。

输出修改后的完整 Python 文件。不要裸 assert False，不要 markdown，不要解释。

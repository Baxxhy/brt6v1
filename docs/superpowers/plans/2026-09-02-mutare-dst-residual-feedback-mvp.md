# Mutare-DST 最小改动、最大复用实现计划

> 本文以当前 BRT6 可运行代码为基线，保留现有种子执行、Top-3 种子流水线、候选执行、严格验证、定向修复和预算控制，只在现有反馈对象、检查点排序和结果轨迹上加入轻量的“父子语义残差”。

## 1. 这次“最小实现”的准确含义

“最小实现”不等于删掉当前有效步骤，也不等于做一个功能残缺的演示版。这里采用的定义是：

~~~text
尽量少改生产文件
+ 不增加新的模型调用
+ 不增加新的容器执行
+ 不改变现有 CLI、数据结构和预算
+ 复用当前已经算出的执行与验证结果
= 让现有流水线同时具备“差异反馈、定向修复、轨迹记录、差异排序”四项能力
~~~

因此，本计划明确保留以下行为：

- 保留每个 Top-3 种子的现有预执行。
- 保留第一份候选以及每轮修复候选的执行。
- 保留当前 StrictVerifier 和 VerifierDecision。
- 保留 setup、trigger、oracle、generic 修复路由。
- 保留当前反馈轮数和 BRT 轮数限制。
- 保留当前“某个种子接受后只结束该种子、外层仍探索 Top-3”的行为。
- 保留现有 mutation plan 以及 trigger 失败时至多一次的重规划。
- 保留最终正式评估流程。

第一版不增加：

- coverage.py、调用图或动态插桩；
- 新的 LLM agent；
- 新的 prompt 文件；
- 新的 CLI 参数；
- 新的 schema 或 dataclass；
- beam search、frontier 或回滚；
- 额外的测试执行或 LLM 调用。

最终只修改一个生产文件和一个现有测试文件：

~~~text
execution/feedback.py
tests/test_feedback_ablations.py
~~~

## 2. 核心算法主张

### 2.1 推荐表述

Mutare-DST 将缺陷复现测试生成建模为一个**基于执行证据的残差缩减过程**：

> 从已有测试提供的可执行宿主协议出发，生成候选测试；每次执行后，将候选行为与 Issue 所描述的目标缺陷行为比较，识别当前仍未满足的最前置语义条件；随后只针对该残差生成下一次变换，并用父候选到子候选的残差变化评价这次变换是否有效。

这个表述比“对两个文本做语义相似度”更准确。这里的“差异”不是一个 embedding 距离，也不是让 LLM 自由描述两个测试哪里不同，而是由当前已经存在的执行结果和严格验证结果归约成一个可操作状态：

~~~text
宿主是否可执行？
    ↓ 是
目标路径是否被触发？
    ↓ 是
Oracle 是否与 Issue 一致且可证伪？
    ↓ 是
候选是否被严格接受？
~~~

### 2.2 搜索对象、状态和动作

搜索对象是候选测试：

~~~text
C0 → C1 → C2 → ...
~~~

每个候选 Ci 已经会产生：

~~~text
ExecutionResult
+ VerifierDecision
+ StrictVerifierResult
~~~

MVP 不再调用一个模型来“计算语义差异”，而是从这三份现成证据确定性地构造 ResidualState(Ci)。

动作也不新增。继续使用当前代码已有的三类动作：

~~~text
SETUP 缺口   → repair_setup
TRIGGER 缺口 → repair_trigger，可复用一次 mutation re-plan
ORACLE 缺口  → repair_oracle
~~~

所以 Mutare-DST MVP 不是替换原流水线，而是在原流水线上增加一个明确的状态解释层：

~~~text
当前执行反馈
    ↓
ResidualState：还差什么
    ↓
ResidualTransition：与父候选相比有没有缩小
    ↓
现有 repair_candidate：下一步只改什么
~~~

## 3. 为什么继续运行种子

### 3.1 当前代码中的真实作用

context/host_context.py 中的 build_host_context() 已经做两类工作：

1. 静态恢复 seed 的 import、fixture、decorator、class setup、相邻测试和 runner 命令。
2. 当 skip_execution=False 时，用 run_command_in_conda() 执行 seed，并写入 HostContext.seed_execution_status 和 HostContext.seed_execution。

execution/feedback.py 又用这个状态决定该 seed 是否具备可用测试协议，必要时切换备用 seed。

### 3.2 本方案的决定

继续运行，而且保持调用位置、次数、超时和 fallback 行为不变。

理由不是“运行 seed 能证明它触发了目标缺陷”。它通常不能证明这一点。保留它的原因是：

- 它确认恢复出来的测试 runner、依赖、fixture 和宿主文件在 buggy 仓库中能工作。
- 它是现有 seed 资格检查和 fallback 的一部分，删除会改变当前可运行行为。
- 它已经付出了执行成本，MVP 可以零额外成本地把结果作为搜索起点的协议画像。
- 保留后更容易做公平前后对比，因为容器执行预算没有变化。

### 3.3 能从 seed 执行推断什么

seed 通过时，只能推断：

~~~json
{
  "host_ready": true,
  "trigger_satisfied": null,
  "oracle_satisfied": null,
  "next_gap": "TRIGGER",
  "semantic_status": "UNKNOWN_NOT_VERIFIED"
}
~~~

不能把 seed 的 PASS 解释为“已经接近 Issue”，也不能把它直接算成候选相对 seed 的语义进步。因为当前 seed 执行没有经过与生成候选相同的严格语义验证。

因此，seed 状态只完成两个任务：

- 提供搜索起点：宿主协议已就绪或尚未就绪。
- 记录 seed → C0 的初始化关系。

第一份生成候选 C0 执行以后，才开始正式计算 C0 → C1 → C2 的语义残差变化。

标准运行下的 seed 状态映射固定为：

| seed execution status | host_ready | stage / depth | 处理 |
|---|---:|---|---|
| PASS、ISSUE_ALIGNED_FAIL、ASSERTION_FAIL | true | TRIGGER / 1 | 宿主协议可执行；目标触发与 Oracle 仍为未知 |
| SETUP_ERROR、SYNTAX_ERROR、COLLECT_ERROR、TIMEOUT、ERROR | false | SETUP / 0 | 保留当前 fallback 行为；若没有备用 seed，则后续仍走当前修复逻辑 |

generate_only 是当前已有的显式例外：它本来就设置 skip_execution=True。该模式不执行候选反馈搜索，因此不构造可用于比较或排序的 seed residual，不能与标准运行轨迹混合统计。这里所说的“保持 seed 运行”指标准非 generate_only 流程。

## 4. 当前流水线与改造后的流水线

### 4.1 当前实际流程

当前 execution/feedback.py 的主要执行链是：

~~~text
Issue
  ↓
BehaviorTarget
  ↓
iCoRe Top-3 seed
  ↓
逐 seed 建立 HostContext，并执行 seed
  ↓
恢复 ProtocolRecovery
  ↓
生成或规划变换，得到 C0
  ↓
执行 Ci
  ↓
StrictVerifier / VerifierDecision
  ↓
保存 checkpoint
  ↓
accept：结束当前 seed
setup：repair_setup
trigger：repair_trigger，必要时重做一次 mutation plan
oracle：repair_oracle
reject：结束当前 seed
  ↓
在预算内继续执行子候选
  ↓
从当前 seed 的 checkpoints 选最好候选
  ↓
从 Top-3 seed 结果中选最好结果
~~~

这本来就有迭代反馈。MVP 不删除迭代，反而把现在“只看当前轮绝对标签”的反馈升级成“当前残差 + 父子变化”。

### 4.2 改造后的流程

~~~text
执行 seed
  ↓
构造 SeedResidual：只判断宿主协议是否 ready
  ↓
生成并执行 C0
  ↓
现有严格验证
  ↓
构造 ResidualState(C0)
  ↓
记录 seed → C0 为 INITIALIZED，不参与改进/回退判断
  ↓
将 current_state + next_gap + instruction 喂给现有修复 prompt
  ↓
生成并执行 C1
  ↓
构造 ResidualState(C1)
  ↓
比较 C0 → C1：
IMPROVED / STAGNANT / REGRESSED / COMPLETE
  ↓
把这次变化和当前残差喂给下一轮
  ↓
仍按当前预算继续，不新增提前停止和额外执行
  ↓
残差深度参与同一 seed 的 checkpoint 排序和 Top-3 seed 排序
~~~

这里的“差异驱动”有三个实际落点，而不是名词：

1. 下一轮模型明确知道现在只差 setup、trigger 还是 oracle。
2. 下一轮模型明确知道上一轮修改让状态进步、停滞还是回退。
3. 最终选择不仅看单轮分数，还偏好真正关闭了更多残差的候选。

## 5. 最小残差表示

### 5.1 不新增 dataclass

第一版直接在 execution/feedback.py 中用普通 dict[str, Any]。这样：

- 不修改 core/schema.py；
- 不触发 JSON 兼容迁移；
- 旧结果仍能被当前 reader 读取；
- 可以直接嵌入已有 verifier 字段和反馈 JSON。

建议结构如下：

~~~json
{
  "source": "candidate",
  "round_id": 1,
  "stage": "ORACLE",
  "depth": 2,
  "host_ready": true,
  "trigger_satisfied": true,
  "oracle_satisfied": false,
  "accepted": false,
  "next_gap": "ORACLE",
  "evidence": {
    "execution_status": "ASSERTION_FAIL",
    "verifier_decision": "repair_oracle",
    "failure_class": "oracle_wrong",
    "target_hit": true,
    "oracle_grounded_in_issue": false,
    "uses_public_behavior": true
  }
}
~~~

### 5.2 四个正常阶段

| stage | depth | 含义 | 下一动作 |
|---|---:|---|---|
| SETUP | 0 | 候选尚未稳定执行 | 复用 repair_setup |
| TRIGGER | 1 | 宿主可运行，但目标行为未被确认触发 | 复用 repair_trigger |
| ORACLE | 2 | 已确认到达目标行为，但判定条件不正确或不充分 | 复用 repair_oracle |
| COMPLETE | 3 | 严格验证接受 | 结束当前 seed |

另有：

| stage | depth | 含义 |
|---|---:|---|
| TERMINAL | -1 | 当前 verifier 明确拒绝，且现有路由也不会继续修复 |

不单独增加 REACH 阶段。当前实现只有 target_hit，没有可靠的动态调用证据。此时把“路径到达”和“缺陷触发”硬拆成两层会造成伪精确。后续若引入低成本 tracing，再升级为五阶段状态。

### 5.3 状态确定规则

候选状态必须复用 _repair_focus()，避免“残差状态”和实际路由互相矛盾：

~~~python
if decision.decision == "accept":
    stage = "COMPLETE"
elif focus == "setup":
    stage = "SETUP"
elif focus == "trigger":
    stage = "TRIGGER"
elif focus == "oracle":
    stage = "ORACLE"
else:
    stage = "TERMINAL"
~~~

细节以当前执行和严格验证字段作为证据：

~~~text
SETUP:
  execution.status ∈ {SETUP_ERROR, SYNTAX_ERROR, COLLECT_ERROR}
  或 strict failure_class 属于 setup/syntax/collect

TRIGGER:
  strict failure_class ∈ {buggy_pass, target_not_hit}
  或现有 decision 明确为 repair_trigger

ORACLE:
  strict failure_class ∈ {oracle_wrong, oracle_too_strong}
  或 side_path 被当前 _repair_focus() 识别为 oracle 问题

COMPLETE:
  decision.decision == accept
~~~

不自行发明新分类器，也不再调用 LLM。

### 5.4 父子残差变化

比较连续两个候选的 depth：

~~~python
if child.stage == "COMPLETE":
    relation = "COMPLETE"
elif child.depth > parent.depth:
    relation = "IMPROVED"
elif child.depth < parent.depth:
    relation = "REGRESSED"
else:
    relation = "STAGNANT"
~~~

记录内容：

~~~json
{
  "parent_round": 0,
  "child_round": 1,
  "parent_stage": "TRIGGER",
  "child_stage": "ORACLE",
  "depth_delta": 1,
  "relation": "IMPROVED",
  "closed_gap": "TRIGGER",
  "remaining_gap": "ORACLE",
  "initialization": false
}
~~~

seed → C0 使用：

~~~json
{
  "parent_round": "seed",
  "child_round": 0,
  "relation": "INITIALIZED",
  "initialization": true
}
~~~

它只用于追踪，不用来声称语义改进。

## 6. 如何把残差真正喂给模型

### 6.1 复用现有传递链

当前代码已经存在完整链路：

~~~text
_semantic_feedback_payload()
  ↓
semantic_feedback
  ↓
repair_candidate(..., verifier_feedback=semantic_feedback)
  ↓
JSON 序列化
  ↓
repair_setup / repair_trigger / repair_oracle / repair_generic
  的 {verifier_feedback}
~~~

trigger 需要重做 mutation plan 时，_build_plan_or_invalid() 也已经收到相同的 verifier_feedback。

因此只需扩展 _semantic_feedback_payload() 的返回值，不需要修改：

~~~text
generation/generator.py
prompts/repair_setup/user.md
prompts/repair_trigger/user.md
prompts/repair_oracle/user.md
prompts/repair_generic/user.md
mutation planner 的 prompt
~~~

### 6.2 扩展后的反馈输入

保留所有当前 verifier 字段，在其下新增一个命名空间：

~~~json
{
  "decision": "repair_oracle",
  "reason": "target reached but assertion mismatches the issue",
  "next_action": "repair the oracle",
  "failure_class": "oracle_wrong",
  "target_hit": true,
  "oracle_grounded_in_issue": false,
  "uses_public_behavior": true,
  "oracle_kind": "VALUE",
  "oracle_falsifiable": true,
  "residual_feedback": {
    "current_state": {
      "stage": "ORACLE",
      "depth": 2,
      "next_gap": "ORACLE"
    },
    "last_transition": {
      "relation": "IMPROVED",
      "parent_stage": "TRIGGER",
      "child_stage": "ORACLE",
      "depth_delta": 1
    },
    "instruction": "The last mutation reached the target behavior. Preserve its setup and trigger path. Change only the oracle so it checks the public, falsifiable behavior stated by the issue."
  }
}
~~~

模型看到的 prompt 仍是现有 prompt，近似为：

~~~text
Issue:
<原始 issue>

Behavior Target:
<当前结构化目标行为>

Host / Protocol / Related Source:
<当前上下文>

Current candidate:
<Ci 的代码>

Execution:
<Ci 的状态和日志>

Verifier feedback:
<上述 JSON，其中包含 current_state、last_transition 和 instruction>

请输出修复后的完整测试文件。
~~~

### 6.3 确定性 instruction

为了让嵌套 JSON 不只是日志，代码生成一句短而具体的指令：

~~~text
SETUP:
Fix only collection, imports, fixtures, command compatibility, or test setup.
Preserve the intended trigger and oracle whenever possible.

TRIGGER:
The candidate is executable but has not confirmed the issue trigger.
Preserve the working harness and oracle contract; change inputs, call sequence,
state, or branch conditions to reach the target behavior.

ORACLE:
The target behavior is reached. Preserve setup and trigger path.
Change only the public, falsifiable oracle to match the issue.

REGRESSED:
The last mutation regressed from <parent> to <child>.
Restore the properties that had already closed <closed gaps>, then address <next gap>.

STAGNANT:
The last mutation did not close <next gap>.
Use a materially different edit for that gap while preserving already satisfied stages.
~~~

这些文本由 Python 固定映射生成，不增加一次 LLM 调用。

## 7. 迭代控制：第一版保持当前行为

这是本次重新规划与旧版本最重要的差别之一。

MVP **不**因为 STAGNANT 或 REGRESSED 提前停止，也**不**自动回滚。理由：

- 用户要求尽量保持当前可运行流程。
- 当前每个 seed 的预算已经很小，新增提前停止可能直接降低 F2P。
- 回滚需要保存分支状态并定义重新展开策略，会把线性循环扩大成 frontier search。
- 在没有轨迹数据前，不能假定一次回退就代表后续无法成功。

第一版中的 transition 只用于：

1. 指导下一轮模型如何修复；
2. 写入轨迹，支持事后分析；
3. 在硬约束之后帮助选择候选；
4. 为第二阶段是否引入早停或回滚提供数据。

所以现有循环条件保持不变：

~~~text
accept                 → 当前 seed 停止
reject                 → 当前 seed 按现逻辑停止
对应 feedback 被关闭   → 按现逻辑停止
对应预算耗尽            → 按现逻辑停止
否则                   → 生成、执行下一候选
~~~

这仍然是迭代反馈，只是先增强反馈质量，不冒险改变搜索预算。

## 8. 候选与种子选择如何最小改造

### 8.1 同一 seed 内的 checkpoint 排序

当前 _save_checkpoint() 已经生成 rank_key。不改 CandidateCheckpoint，只把残差深度插入硬资格之后：

~~~python
rank_key = [
    int(hard_eligible),
    residual_depth,
    int(accepted),
    int(issue_aligned),
    int(target_hit),
    int(grounded),
    int(public),
    int(executable_fail),
    int(not plan_violated),
    int(oracle_contract_preserved),
    int(oracle_risk_level != "high"),
    -attempt_id,
]
~~~

排序原则是：

1. 不能用残差深度救活不可执行、违反 mutation plan 或没有可证伪 oracle 的候选。
2. 在硬资格相同的候选中，优先选择关闭更多语义缺口的候选。
3. 继续使用现有其余维度做细粒度 tie-break。

在 checkpoint.verifier 这个本来就是字典的字段中附加：

~~~json
{
  "residual_state": {},
  "residual_transition": {}
}
~~~

无需 schema 变更。

### 8.2 Top-3 seed 间排序

当前 _seed_result_score() 继续保留最重要的安全规则：

~~~text
ERROR / SETUP_ERROR / ENV_UNRESOLVED → -1000
~~~

只有通过这层检查后，才从 checkpoint 的 rank_key 读取 hard eligibility，并从 verifier.residual_state.depth 读取深度。例如：

~~~python
score = (
    hard_eligible * 100_000
    + residual_depth * 1_000
    + existing_score
)
~~~

这里的数量级只用于编码词典序，不具有统计意义。它保证“可正式评估”优先于残差阶段，残差阶段又优先于现有几十到几百分的细节分数。旧 checkpoint 若没有 rank_key 或 residual 字段，分别按 hard_eligible=0 和 depth=0 处理。正式实验中仍报告原始成功指标，不把这个内部选择分数当作效果指标。

### 8.3 不减少 Top-3 探索

即使第一个 seed 已接受，外层仍按当前逻辑运行后续 seed。这样：

- 不改变现有运行次数；
- 不把“更快停止”和“生成质量提升”混为一个处理；
- 仍可比较不同 seed 的最终残差轨迹。

## 9. 新增的运行产物

每个 seed 目录增加一个小文件：

~~~text
seed_candidates/seed_<k>/residual_trace.json
~~~

建议内容：

~~~json
{
  "schema_version": "residual_trace_v1",
  "instance_id": "django__django-10924",
  "seed": {
    "file": "tests/...",
    "name": "test_...",
    "execution_status": "PASS",
    "residual_state": {
      "stage": "TRIGGER",
      "depth": 1,
      "semantic_status": "UNKNOWN_NOT_VERIFIED"
    }
  },
  "rounds": [
    {
      "round_id": 0,
      "state": {
        "stage": "TRIGGER",
        "depth": 1,
        "next_gap": "TRIGGER"
      },
      "transition": {
        "relation": "INITIALIZED",
        "initialization": true
      }
    },
    {
      "round_id": 1,
      "state": {
        "stage": "ORACLE",
        "depth": 2,
        "next_gap": "ORACLE"
      },
      "transition": {
        "relation": "IMPROVED",
        "depth_delta": 1
      }
    }
  ],
  "final": {
    "selected_round": 1,
    "stage": "ORACLE",
    "depth": 2
  },
  "counts": {
    "improved": 1,
    "stagnant": 0,
    "regressed": 0,
    "complete": 0
  }
}
~~~

写法采用每轮覆盖整个 JSON，而不是增量 append，防止进程中断产生半行 JSON。

外层选择 seed 后，把选中 seed 的 residual_trace.json 复制到实例根目录，并把以下摘要加入已有 seed_attempts_summary：

~~~json
{
  "final_residual_stage": "ORACLE",
  "final_residual_depth": 2,
  "residual_transition_counts": {
    "improved": 1,
    "stagnant": 0,
    "regressed": 0,
    "complete": 0
  }
}
~~~

同样不修改 FinalResult schema。

## 10. 精确代码改动

### Task 1：在 execution/feedback.py 增加四个私有 helper

增加：

~~~python
def _seed_residual_state(host: HostContext) -> dict[str, Any]:
    ...

def _candidate_residual_state(
    round_id: int,
    execution: ExecutionResult,
    decision: VerifierDecision,
    strict_result: Any | None,
) -> dict[str, Any]:
    ...

def _residual_transition(
    parent: dict[str, Any],
    child: dict[str, Any],
    *,
    initialization: bool = False,
) -> dict[str, Any]:
    ...

def _residual_instruction(
    state: dict[str, Any],
    transition: dict[str, Any],
) -> str:
    ...
~~~

约束：

- _candidate_residual_state() 必须调用或遵循现有 _repair_focus() 的结果。
- 所有布尔值缺证据时使用 None，不要把未知写成 False。
- helper 必须是确定性的，不调用 LLM、不访问网络、不执行测试。

### Task 2：把 helper 接入现有候选循环

在建立 HostContext 后：

~~~python
seed_residual = _seed_residual_state(host)
previous_candidate_residual = None
residual_rounds = []
~~~

每轮严格验证以后、保存 checkpoint 以前：

~~~python
current_residual = _candidate_residual_state(
    brt_attempt, execution, decision, strict_result
)
parent_residual = (
    previous_candidate_residual
    if previous_candidate_residual is not None
    else seed_residual
)
transition = _residual_transition(
    parent_residual,
    current_residual,
    initialization=previous_candidate_residual is None,
)
semantic_feedback = _semantic_feedback_payload(
    decision,
    strict_result,
    current_residual=current_residual,
    transition=transition,
)
~~~

然后：

- 将状态和 transition 传给 _save_checkpoint()；
- 更新并保存 residual_trace.json；
- 设定 previous_candidate_residual = current_residual；
- 继续执行现有 accept、预算、focus 和 repair 分支。

注意更新 previous_candidate_residual 的位置要在下一轮 repair 前完成，但不能改变当前候选、当前 best_* 或预算变量的语义。

### Task 3：扩展 checkpoint、seed 排序和选中产物

在 _save_checkpoint() 增加两个可选参数：

~~~python
residual_state: dict[str, Any] | None = None
residual_transition: dict[str, Any] | None = None
~~~

然后：

- 在 verifier 字典中保存二者；
- 在 rank_key 的 hard_eligible 后加入 depth；
- 不修改 _checkpoint_score() 的对外含义。

在 _seed_result_score()：

- 先保留当前不可运行状态返回 -1000；
- 再按 hard eligibility、checkpoint residual depth、existing score 的顺序编码；
- checkpoint 中没有新字段时按 hard_eligible=0、depth=0 处理，兼容旧产物。

在 Top-3 聚合处：

- 读取每个 seed 的 residual_trace.json；
- 写入现有 attempt 字典；
- 用已有 _copy_if_exists() 复制选中轨迹。

### Task 4：保持实验入口不变

不修改 core/ablation.py、method version、signature 或 CLI。残差反馈直接跟随现有 specialized_feedback：

- specialized feedback 开启时，计算残差、写入 prompt、参与排序并保存轨迹；
- generic iteration 消融时，不把残差写入反馈或排序。

运行新算法时使用新的输出目录区分实验结果即可。这个论文原型不增加哈希、SHA256、结果指纹或兼容迁移层。

## 11. 明确不改的文件

以下文件在 MVP 中不修改：

~~~text
context/host_context.py
generation/generator.py
mutation/*
validation/*
core/schema.py
pipeline/run.py
prompts/*
evaluation/*
~~~

原因分别是：

- host_context.py 已经正确执行 seed；无需再运行一次，也无需删除。
- generator.py 已经把完整 verifier_feedback 放入所有修复 prompt。
- mutation planner 已经接收同一反馈对象。
- validator 已经产出 MVP 所需的 failure_class、target_hit 和 oracle 字段。
- CandidateCheckpoint.verifier 本来就是字典，可以向后兼容地容纳残差。
- resume 已经比较 signature，只需改变 signature 的生成端。
- prompt 已有 verifier_feedback 插槽。
- 正式评估不应被一个内部搜索状态改写。

如果实现时发现必须修改上述文件，应先停下来验证是不是误解了现有接口；不应为了代码美观扩大范围。

## 12. 单元测试范围

只扩展：

~~~text
tests/test_feedback_ablations.py
~~~

新增五类小测试，不创建新的大规模测试框架。

### 12.1 seed 画像

输入：

~~~text
HostContext.seed_execution_status = PASS
~~~

预期：

~~~text
stage = TRIGGER
depth = 1
host_ready = true
trigger_satisfied = null
semantic_status = UNKNOWN_NOT_VERIFIED
~~~

这条测试防止将 seed PASS 错当成语义成功。

### 12.2 父子状态变化

构造三个纯内存状态：

~~~text
C0 = TRIGGER
C1 = ORACLE
C2 = COMPLETE
~~~

预期：

~~~text
C0 → C1 = IMPROVED
C1 → C2 = COMPLETE
ORACLE → SETUP = REGRESSED
~~~

### 12.3 反馈确实到达现有 repair 调用

复用现有 _run_forced_decisions 或对应 mock 流程，检查传入 repair_candidate() 的 verifier_feedback 包含：

~~~text
residual_feedback.current_state
residual_feedback.last_transition
residual_feedback.instruction
~~~

不做真实 LLM 调用。

### 12.4 排序安全性

验证：

- 硬资格相同时，ORACLE 候选优于 TRIGGER 候选；
- ENV_UNRESOLVED 即使带 depth=3 仍返回 -1000；
- 旧 checkpoint 没有 residual 字段时仍能评分。

### 12.5 运行签名

不新增运行签名测试。现有 generic iteration 集成测试继续验证它只调用 generic repair，不接收残差反馈。

## 13. 最小验证命令

实现后只运行与改动直接相关的验证：

~~~bash
python -m py_compile execution/feedback.py
cd ..
python -m unittest brt6.tests.test_feedback_ablations -v
~~~

不在本阶段运行全量 benchmark，不为验证 helper 启动 Docker，也不重复执行真实 LLM。

完成单元级验证后，再用三个现有实例做小规模 canary：

~~~text
django__django-10924
django__django-10914
django__django-11001
~~~

canary 保持：

~~~text
同一模型
同一温度
同一 Top-3 seed
同一 seed 预执行
同一反馈预算
同一执行超时
同一正式评估
~~~

只比较改动前后：

- F2P / ISSUE_ALIGNED_FAIL；
- accepted candidate 数；
- setup、trigger、oracle 路由次数；
- 每个成功实例的候选执行数和 LLM 调用数；
- IMPROVED、STAGNANT、REGRESSED 轨迹分布；
- 最终选中候选的 residual depth。

三个实例只用于冒烟和轨迹检查，不能用于宣称指标提升。

## 14. 对指标提升的合理预期

### 14.1 可能提升的部分

这个 MVP 最可能改善：

- trigger 修复后被 oracle 修复覆盖掉的问题；
- oracle 已经正确却又被下一轮随意改动的问题；
- verifier 输出很多字段但模型不知道最优先修什么的问题；
- 多个候选分数接近时，选择了残差更大的候选的问题；
- 论文分析中无法解释“某次变换到底有没有推进”的问题。

它可能提高相同预算下的成功率，也可能先表现为：

- 相同成功率下更少无效修改；
- 更高的 transition improvement rate；
- 更稳定的候选选择；
- 更低的成功实例平均轮数。

### 14.2 不能保证的部分

这一改动不能保证 F2P 一定提升，原因是：

- target_hit 仍部分依赖现有严格验证器，而不是真实动态 tracing；
- 四阶段状态会压缩细粒度失败原因；
- prompt 没有单独重写，只通过已有 JSON 插槽加入定向信息；
- 线性搜索仍可能被一个局部最优候选限制。

因此论文中的因果主张必须由后续实验支持，不能把“增加了 residual 字段”直接写成“提升了成功率”。

### 14.3 最值得看的中间指标

第一轮实验优先验证：

~~~text
Residual Progress Rate
= IMPROVED 或 COMPLETE 的父子变换数
  / 所有非初始化父子变换数
~~~

以及：

~~~text
Preservation Error Rate
= 已关闭阶段在下一轮重新打开的次数
  / 所有修复变换数
~~~

如果这两项没有改善，就不应立即扩展成复杂算法；应先检查 stage 映射和 instruction 是否真的约束了模型。

## 15. 后续升级条件

只有满足以下任一观察，才进入第二阶段。

### 情况 A：大量候选停在 TRIGGER

说明 target_hit 过粗。此时增加轻量调用追踪，把 TRIGGER 拆成：

~~~text
REACH：是否到达目标 API/函数
TRIGGER：是否满足目标条件并产生症状
~~~

### 情况 B：频繁 REGRESSED

说明仅靠 instruction 不能保存已满足约束。此时再引入：

- parent checkpoint 回滚；
- 单次替代 mutation；
- 两分支小 frontier。

### 情况 C：残差排序与真实成功不一致

说明 depth 太粗。此时再把同阶段内部细分为 evidence-backed 子分数，而不是立刻加入更多 LLM judge。

### 情况 D：轨迹显示收益稳定，但 F2P 不提升

说明瓶颈可能在 seed 检索或 BehaviorTarget 提取，而不是反馈。此时再考虑对 seed 与 Issue 的相关性建模。

这些升级均不属于本次最小实现。

## 16. 实施顺序与检查点

实施时严格按以下顺序：

1. 在 tests/test_feedback_ablations.py 写纯 helper 和 signature 测试。
2. 在 execution/feedback.py 实现残差状态与 transition helper。
3. 运行相关单测，确认状态映射。
4. 接入现有候选循环和 semantic_feedback。
5. 接入 checkpoint 和 seed 排序。
6. 写入 residual_trace.json 并复用现有复制函数。
7. 运行最小编译和单元测试。
8. 人工检查一个 mock 产物，确认 seed PASS 没有被写成语义成功。
9. 在用户确认后运行三个 canary；不直接扩大到全量数据集。

每个步骤都必须保证：

~~~text
没有新增 LLM 调用
没有新增候选执行
没有新增 seed 执行
没有改变现有预算
没有改变现有 CLI
旧 JSON 缺少 residual 字段时仍可读取
~~~

## 17. 完成定义

满足以下全部条件才算 MVP 实现完成：

- 当前 seed 仍按原逻辑执行，不出现默认 SKIPPED。
- 每轮候选仍按当前逻辑执行和验证。
- 每轮都有确定性的 residual state。
- 从第二个候选开始都有父子 transition。
- 当前 residual 和 transition 会进入现有修复 prompt。
- trigger 重规划能收到同一 residual feedback。
- checkpoint 和 Top-3 seed 选择能读取 residual depth。
- 不可运行状态不会被 residual depth 抬高。
- 每个 seed 产生 residual_trace.json。
- 选中 seed 的轨迹被复制到实例根目录。
- generic iteration 消融不启用 residual feedback。
- 相关单元测试通过。
- 生产改动只落在 execution/feedback.py。
- 没有引入哈希、SHA256、版本指纹或新的工程兼容层。

## 18. 一句话实施结论

第一版不删 seed 执行、不另造 agent、不重写 prompt、不增加运行预算；它把现有每轮的执行与严格验证结果压缩为“还差哪一步”，再比较父候选与子候选“这一步是否被关闭”，并通过当前已经存在的反馈入口、checkpoint 和 Top-3 选择逻辑发挥作用。这是对当前 BRT6 代码改动最小、同时能形成完整 Mutare-DST 机制闭环的实现路径。

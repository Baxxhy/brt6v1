# BRT6 差异驱动语义变异最终修改计划

> **面向实现者：** 直接按任务顺序实现，每项完成后运行对应验证。本文是唯一正式修改计划，替代此前的 residual-only 和 AST materialization 方案。

**目标：** 保留 BRT6 当前可运行框架和 LLM 的代码生成能力，用最小代码改动把核心算法升级为“执行反馈驱动的语义差异变异搜索”，同时修复 131/276 实验暴露的错误接受、Buggy PASS、Oracle 过约束和候选选择问题，最后从 IssueRewrite 开始完整重跑 SWT 276。

**架构：** LLM 负责理解 seed 与目标缺陷行为的语义差异、选择变异操作并生成完整候选；现有控制器负责保存 `preserve/change/avoid` 差异契约、执行候选、比较父子状态、限制同类无效修改并选择最佳候选。程序不做 AST 代码变换，不增加新 Agent、模型调用、Docker 执行阶段或 coverage。

**技术栈：** 现有 Python 流水线、DeepSeek-V4-Flash、14-key API pool、iCoRe Top-3、SWTBench 官方 Docker harness。

---

## 1. 输入依据

本文结合：

- `docs/BRT6_CURRENT_IMPLEMENTATION_FLOW_DETAILED.md`：当前真实主流程、Prompt、候选生成、反馈、排序和官方评测边界。
- `docs/superpowers/plans/2026-09-02-mutare-dst-residual-feedback-mvp.md`：昨晚已经实现的 residual state、父子 transition、反馈注入和轨迹记录。
- `results/runs/brt6_swt276_deepseekv4flash_full_20260902_164451/`：本轮真实生成与官方评测产物。

旧正式结果：

```text
Official F2P@1:               131 / 276 = 47.46%
Generation accepted:         193
Accepted 且正式 F2P:         117 / 193 = 60.6%
Buggy 和 Gold-fixed 都失败:   92
Buggy PASS 最终候选:          20，正式 F2P 为 0
Unmatched/parse:              25
No parsed test:               5
Patch not applied:            2
```

算法使用情况：

```text
Mutation Plan calls:          990
VALID plan calls:             91
Plan abstentions:             889
最终 VALID plan candidate:      2 / 275
最终无 plan candidate:         249 / 275
最终 FALLBACK_DIRECT:           24 / 275
```

因此有两个不能回避的事实：

1. 47.46% 是真实官方 F2P，但不能归因于昨晚 residual，也不能证明当前 Mutation Planner 是主要贡献。
2. 当前最大损失不是代码无法运行，而是生成阶段把大量 Gold-fixed 上仍失败的错误 Oracle 当成复现成功。

## 2. 最终算法定位

### 2.1 核心问题

相似测试提供了可执行骨架，但它与目标缺陷行为之间仍存在语义差异。一次自由重写容易同时破坏 setup、trigger 和 oracle；而当前 residual 只把 verifier 的标签重新命名，缺少可持续的父子修改契约。

本算法解决：

```text
如何在保留 seed 已经正确的测试协议和行为约束时，
只修改当前仍不满足的语义维度，
并根据真实执行反馈持续更新下一次修改？
```

### 2.2 核心表示：Semantic Delta Contract

每个候选执行后维护一个轻量 JSON，不增加 dataclass：

```json
{
  "stage": "SETUP|TRIGGER|ORACLE|SEARCH_ACCEPTED|TERMINAL",
  "observed_behavior": "当前候选实际做了什么以及为什么失败",
  "target_behavior": "Issue 要求的目标行为",
  "gap": "当前最关键且最前置的未满足差异",
  "preserve": ["下一轮必须保留的已满足行为"],
  "change": ["下一轮只允许改变的输入、状态、调用链或 Oracle"],
  "avoid": ["已经观察到的错误路径或过强约束"],
  "operator": "下一轮语义变异操作",
  "expected_effect": "修改后在 buggy 上应观察到的变化",
  "evidence": {
    "execution_status": "",
    "failure_origin": "",
    "verifier_decision": "",
    "oracle_risk": ""
  }
}
```

它不是 embedding 距离，也不是 AST diff。它是供下一轮 LLM 消费的“保留—修改—避免”约束。

### 2.3 语义变异操作

继续复用现有操作集合：

```text
ARG_VALUE_REPLACE
ARG_BOUNDARY_EXPAND
OPERATOR_FLIP
CALL_CHAIN_EXTEND
STATE_MUTATION
FIXTURE_DATA_MUTATION
CONFIG_MUTATION
MOCK_BEHAVIOR_MUTATION
LIFECYCLE_TRIGGER
SERIALIZATION_TRIGGER
WARNING_LOG_TRIGGER
ORACLE_REFOCUS
```

“变异”的定义是：LLM 根据 Semantic Delta Contract 修改测试的输入、状态、调用序列、fixture/config/mock 行为或目标 Oracle，并生成完整测试文件。AST 只保留现有静态解析、Oracle fingerprint 和安全审计用途，不负责改写代码。

### 2.4 搜索过程

```text
Issue + BehaviorTarget + iCoRe Top-3 seed
  -> 恢复 HostContext / ProtocolRecovery
  -> 为单个 seed 生成初始 Semantic Delta
  -> LLM 按 preserve/change/avoid 生成完整候选
  -> 官方 buggy Docker 执行
  -> StrictVerifier 输出执行后的 Delta Diagnosis
  -> 控制器形成父子 transition
  -> IMPROVED: 保留子候选，继续下一个 gap
  -> STAGNANT: 保留父候选，要求换 operator
  -> REGRESSED: 回到父候选，禁止破坏 preserve
  -> SEARCH_ACCEPTED: 当前 seed 停止
  -> Top-3 语义共识与风险排序
  -> final_test.py
  -> 官方 Gold-fixed F2P 评测
```

### 2.5 真正的创新点

主创新不是“使用 LLM”、不是“分成三个阶段”，而是三者的耦合：

1. **差异状态化：** 将候选与目标行为的差异压缩为可执行修改的 Semantic Delta Contract。
2. **约束式语义变异：** 每轮同时声明必须保留、只允许修改和必须避免的行为，降低整文件生成漂移。
3. **执行驱动搜索：** 根据父子候选的真实执行结果更新 gap 和 operator，而不是固定 Prompt 重试。

因果链：

```text
显式差异 + preserve/change/avoid
  -> LLM 修改更聚焦且少破坏已满足行为
  -> 无效重复和 Oracle 漂移减少
  -> generation accepted precision 提高
  -> Official F2P@1 提高
```

外部相关工作尚未在本文中重新检索，因此“相对最近工作的独占新颖性”仍需后续文献检索确认；实现与实验不能把未检索的新颖性写成既成事实。

## 3. 为什么不采用 AST 变异

- 当前只有 91/990 次 plan 为 VALID，严格 anchor 会让大多数实例直接失去主算法。
- BRT 经常需要跨语句状态构造、framework fixture、mock、生命周期和调用链变化，不适合只做局部语法替换。
- AST materialization 会限制 LLM 对项目惯例和复杂 setup 的修复能力，可能降低当前 47.46% 基线。
- 论文贡献应放在语义差异如何指导搜索，而不是声明一个覆盖率很低的 AST 编辑器。

最终约束：不新增 AST 改写器；不把文本包含关系包装成“真实语义变异率”。

## 4. 最小代码改动范围

### 4.1 生产 Python 文件

只修改：

1. `core/schema.py`
   - 给 `StrictVerifierResult` 增加 Semantic Delta Diagnosis 字段。
2. `mutation/seed_mutator.py`
   - 将 AST-anchor plan 改为语义变异计划；保留现有 LLM 调用位置和次数。
3. `validation/mutation_plan_validator.py`
   - 从“必须逐 AST anchor”改为“必须有 issue/source/seed 证据、明确 operator、preserve/change/expected effect”。
4. `validation/strict_semantic_verifier.py`
   - 在现有 verifier 调用中返回 delta diagnosis、failure origin 和 Gold-fixed 反事实风险。
5. `execution/feedback.py`
   - 扩展昨晚 residual，实现 Semantic Delta Contract、换 operator、父候选回退、PASS 门禁和 Top-3 共识排序。
6. `validation/semantic_guard.py`
   - 增加最小 Oracle、无关 baseline 和过强格式断言检查。
7. `core/ablation.py`
   - 增加一个 `semantic_delta` 核心消融开关。

不新建生产算法模块，不重构 Docker、pipeline、generator 或 evaluation。现有 generator 和 repair 已经能接收 `mutation_plan`、`verifier_feedback` 和完整代码，因此只修改 Prompt 即可。

### 4.2 Prompt 文件

修改：

- `prompts/mutation_plan/system.md`
- `prompts/mutation_plan/user.md`
- `prompts/strict_semantic_verifier/system.md`
- `prompts/strict_semantic_verifier/user.md`
- `prompts/repair_setup/user.md`
- `prompts/repair_trigger/user.md`
- `prompts/repair_oracle/user.md`
- `prompts/repair_generic/user.md`
- `prompts/generation/user.md`

### 4.3 测试与运行文件

- 修改 `tests/test_mutation_planner.py`。
- 修改 `tests/test_feedback_ablations.py`。
- 修改 `tests/test_official_benchmarks.py`。
- 修改 `scripts/run_p0_simple_llm_selector_full.sh`。
- 新增一个薄入口 `scripts/run_semantic_delta_swt_full.sh`，只设置现有参数。
- 新增 `analysis/semantic_delta_metrics.py`，只读取原始运行产物并机械汇总。

## 5. 具体实现任务

### 任务 1：扩展现有 StrictVerifier 输出

**文件：**
- 修改：`core/schema.py`
- 修改：`validation/strict_semantic_verifier.py`
- 修改：`prompts/strict_semantic_verifier/system.md`
- 修改：`prompts/strict_semantic_verifier/user.md`
- 测试：`tests/test_feedback_ablations.py`

- [ ] 在 `StrictVerifierResult` 增加：

```python
observed_behavior: str = ""
target_behavior: str = ""
semantic_gap: str = ""
preserve: list[str] = field(default_factory=list)
change: list[str] = field(default_factory=list)
avoid: list[str] = field(default_factory=list)
next_operator: str = ""
expected_effect: str = ""
failure_origin: str = ""
post_fix_failure_risk: str = "unknown"  # low | medium | high | unknown
```

- [ ] 保留现有 `decision/failure_class/target_hit/oracle_*` 字段，避免改动下游接口。
- [ ] verifier 必须先区分 setup、test-body target failure 和 unrelated side path，再填写 gap。
- [ ] verifier 在同一次调用中回答：如果 Issue 描述的正确行为已经实现，当前测试是否仍可能因为其他断言、格式猜测、私有状态或 baseline 失败。
- [ ] `post_fix_failure_risk=high` 时不得 accept，必须转 `repair_oracle` 或 `reject`。
- [ ] 不增加第二个 verifier 调用。
- [ ] 验证旧 JSON 缺少新字段时仍能读取。

### 任务 2：把 Mutation Plan 改为语义计划

**文件：**
- 修改：`mutation/seed_mutator.py`
- 修改：`validation/mutation_plan_validator.py`
- 修改：`prompts/mutation_plan/system.md`
- 修改：`prompts/mutation_plan/user.md`
- 测试：`tests/test_mutation_planner.py`

- [ ] 删除“before/seed_anchor 必须逐 AST 匹配，否则 ABSTAIN”的硬要求。
- [ ] 继续要求 target API、trigger 条件和修改理由来自 Issue、检索源码或 seed，不能凭空编造。
- [ ] 每个 plan 输出：

```json
{
  "status": "PROPOSED|ABSTAIN",
  "operator": "STATE_MUTATION",
  "gap": "当前 seed 与目标 trigger 的具体差异",
  "preserve": ["runner/fixture/setup/已有正确 Oracle"],
  "change": ["输入、状态、调用链中的具体修改"],
  "avoid": ["已知错误路径"],
  "expected_effect": "buggy 执行后应出现的可观察变化",
  "evidence": ["Issue/source/seed 中的依据"]
}
```

- [ ] validator 只验证字段完整、operator 合法、evidence 非空、计划不修改 Oracle、计划不违反 BehaviorTarget safety constraints。
- [ ] 不扩展 `MutationPlan` schema；将新语义字段映射到现有结构：`gap -> trigger_goal`、`operator/change -> MutationStep`、`preserve -> preserve_from_seed`、`expected_effect -> why_target_will_be_reached`，`avoid/evidence` 放入现有 `validation_evidence`。
- [ ] plan 可以包含短代码示例，但不能要求 AST 可替换性。
- [ ] 计划风险为 high 或没有证据时仍然 ABSTAIN。
- [ ] 保留当前每 seed 一次初始 plan 和最多一次 trigger re-plan，不增加调用。

### 任务 3：把昨晚 residual 升级为 Semantic Delta Contract

**文件：**
- 修改：`execution/feedback.py`
- 修改：`tests/test_feedback_ablations.py`

- [ ] 复用现有 `_seed_residual_state()`、`_candidate_residual_state()`、`_residual_transition()`、`_semantic_feedback_payload()` 和 `residual_trace.json` 写入位置。
- [ ] seed PASS 仍然只证明 HostContext 可执行，不能证明接近目标行为。
- [ ] candidate state 合并机械 execution、StrictVerifierResult 和 Oracle risk，生成 `observed/target/gap/preserve/change/avoid/operator/expected_effect`。
- [ ] `SEARCH_ACCEPTED` 只代表生成阶段停止，不使用 `COMPLETE`，避免与正式 F2P 混淆。
- [ ] 父子 transition 规则：

```text
stage 前进且 preserve 未破坏     -> IMPROVED
stage 不变且 failure/gap 基本相同 -> STAGNANT
stage 回退或 preserve 被破坏      -> REGRESSED
strict accept 且通过硬门禁         -> SEARCH_ACCEPTED
```

- [ ] STAGNANT 后继续使用现有 repair 调用，但要求选择不同 operator；同一 gap 连续两次 STAGNANT 时结束当前 seed，切换下一个 Top-3 seed。
- [ ] REGRESSED 时使用现有最佳 checkpoint 作为下一轮 parent，不从退化候选继续修改。
- [ ] 不增加 feedback 轮数、候选执行或 LLM 调用。

### 任务 4：强化 Preserve/Change Prompt

**文件：**
- 修改：`prompts/repair_setup/user.md`
- 修改：`prompts/repair_trigger/user.md`
- 修改：`prompts/repair_oracle/user.md`
- 修改：`prompts/repair_generic/user.md`
- 修改：`prompts/generation/user.md`

- [ ] 所有生成/修复 Prompt 显式读取 Semantic Delta Contract。
- [ ] 统一要求：完整复制 `preserve` 行为，只修改 `change`，不得重新引入 `avoid`。
- [ ] Setup repair 不改变 Trigger 和 Oracle。
- [ ] Trigger mutation 不改变已有 Oracle；如果旧 Oracle 阻塞目标调用，只允许删除阻塞性的非目标 baseline。
- [ ] Oracle repair 不改变输入、状态和目标调用链。
- [ ] 模型继续返回完整 Python 文件，保留其处理复杂 framework setup 的能力。

### 任务 5：修复当前结果中最确定的损失

**文件：**
- 修改：`validation/semantic_guard.py`
- 修改：`execution/feedback.py`
- 测试：`tests/test_feedback_ablations.py`

- [ ] Buggy PASS 永远不能成为 seed final 或 instance final；预算结束仍 PASS 时标为 `TRIGGER_UNRESOLVED`。
- [ ] setup/fixture/import/collection failure 先走机械 setup 路由，不能因为日志含目标词而进入 issue-aligned。
- [ ] 一个测试允许多条 Python assert，但默认只能表达一个目标行为契约；无关 baseline、精确长文本、私有状态和 mock 内部调用次数记为高风险。
- [ ] Issue 明确允许多种合法输出时，Oracle 必须保留析取语义，不能固定成模型偏好的一种表示。
- [ ] `post_fix_failure_risk=high`、Oracle risk high 或 strict fields 不完整时不得 accept。
- [ ] 当前 20 个 Buggy PASS 全部进入继续 trigger search 或失败，不再被导出。

### 任务 6：调整候选与 Top-3 选择

**文件：**
- 修改：`execution/feedback.py`
- 测试：`tests/test_feedback_ablations.py`

- [ ] 删除“LLM-derived residual depth × 1000”对 seed 选择的支配作用。
- [ ] 单 seed rank 固定优先级：可执行非 setup failure、通过 accept 硬门禁、低 post-fix risk、低 Oracle risk、preserve 未破坏、较短测试、较早轮次。
- [ ] Top-3 使用现有结果构造轻量语义签名：`target API + failure class + oracle kind + next gap`。
- [ ] 2/3 或 3/3 一致时在共识候选中选择风险最低者；无共识时沿用单候选硬门禁排序。
- [ ] 不增加 seed、不增加 judge 调用，不因为 seed0 更早而覆盖风险更低的 seed1/2。

### 任务 7：增加一个核心开关和诚实统计

**文件：**
- 修改：`core/ablation.py`
- 修改：`pipeline/run.py`
- 修改：`scripts/run_p0_simple_llm_selector_full.sh`
- 新增：`analysis/semantic_delta_metrics.py`
- 测试：`tests/test_feedback_ablations.py`
- 测试：`tests/test_official_benchmarks.py`

- [ ] 新增 `semantic_delta` 开关；关闭时保留相同模型、Top-3、证据、反馈轮数和执行预算，但不提供 delta contract、不做父子差异换 operator/回退。
- [ ] 统计：Official F2P/276、Macro-F2P、generation accepted precision、false-accept count、Buggy PASS final count、LLM calls、candidate executions、平均成功轮数。
- [ ] residual progress、plan valid、LLM accept 只能作为诊断字段，不能出现在主结果列中。
- [ ] 旧 47.46% 运行中没有 residual trace，分析脚本必须显示 `not_available`，不得回填。

### 任务 8：增加薄的端到端入口

**文件：**
- 新增：`scripts/run_semantic_delta_swt_full.sh`

- [ ] 只封装现有 `run_p0_simple_llm_selector_full.sh`，不重写编排逻辑。
- [ ] 固定：

```bash
export BRT_MODEL_ID=DeepSeek-V4-Flash
export ISSUE_WORKERS=14
export GENERATION_WORKERS=14
export EVALUATION_WORKERS=14
export COMPUTE_PATCH_COVERAGE=false
export RUNTIME_BACKEND=official_docker
```

- [ ] Full run 不传旧 BehaviorTarget cache，从 276 条 IssueRewrite 开始。
- [ ] 所有运行、缓存和临时文件保存在 `/root/Baxxhy/BugReproduce/brt6/results/runs/`。
- [ ] 保持官方镜像名、官方容器名和常驻复用方式。
- [ ] 缺失候选不阻塞评测，按 276 固定分母计失败。
- [ ] 不增加哈希、SHA256 或新的工程兼容层。

## 6. 实施后验证与全量执行

### 6.1 最小验证

直接实现完成后运行：

```bash
cd /root/Baxxhy/BugReproduce
/root/conda/ENTER/envs/icore/bin/python -m py_compile \
  brt6/core/schema.py \
  brt6/mutation/seed_mutator.py \
  brt6/validation/mutation_plan_validator.py \
  brt6/validation/strict_semantic_verifier.py \
  brt6/validation/semantic_guard.py \
  brt6/execution/feedback.py

/root/conda/ENTER/envs/icore/bin/python -m unittest \
  brt6.tests.test_mutation_planner \
  brt6.tests.test_feedback_ablations \
  brt6.tests.test_official_benchmarks -v
```

再运行 10 条 canary，只验证：

- delta contract 确实进入 generator/repair；
- 同 gap 停滞会换 operator；
- regression 使用父 checkpoint；
- Buggy PASS 不导出；
- high post-fix risk 不 accept；
- 官方评测仍按固定分母工作。

Canary 不是论文结果，不用于声明指标提升。

### 6.2 Full 276

```bash
cd /root/Baxxhy/BugReproduce/brt6
bash scripts/run_semantic_delta_swt_full.sh --preflight-only

RUN_TIMESTAMP=$(date +%Y%m%d_%H%M%S) \
bash scripts/run_semantic_delta_swt_full.sh
```

顺序必须是：

```text
276 IssueRewrite
-> 276 Top-3 generation
-> prediction export
-> official SWTBench F2P evaluation
-> honest metric aggregation
```

不复用旧 final_test、旧 checkpoint 或旧 candidate；同一个新 RUN_DIR 中断后可以断点续跑。

## 7. 无水分实验协议

### 7.1 唯一主指标

```text
Official F2P@1 = resolved / 276
```

以下全部按失败进入分母：

- 未生成；
- Buggy PASS；
- patch 未应用；
- parse/collect/setup 失败；
- Buggy 和 Gold-fixed 都失败；
- evaluator error。

Macro-F2P、accepted precision、false accept、成本和轮数是辅助指标。Residual progress、plan calls、plan valid、LLM accept 和共识数量不能替代 F2P。

### 7.2 公平对照

Full 之后完整运行三个 276 实例消融：

1. `w/o Semantic Delta Search`：核心消融。保留所有输入、模型、预算、Oracle guard 和 Top-3，只删除 delta contract、差异驱动 operator 切换与父候选回退。
2. `w/o Counterfactual Oracle Guard`：保留主算法，只移除 post-fix risk 和最小 Oracle 门禁。
3. `w/o Cross-seed Consensus`：保留主算法，只恢复原始 score-only Top-3 选择。

三个消融共享本轮 Full 新生成并冻结的 BehaviorTarget，以排除 IssueRewrite 随机性；不能复用 Full 的候选、执行结果或 prediction。每个变体独立重跑 276 generation + official evaluation。

### 7.3 主算法中心性门禁

定义：

```text
Delta_component = F2P_full - F2P_without_component
```

必须检验：

```text
Delta_semantic_delta_search
  > Delta_oracle_guard

Delta_semantic_delta_search
  > Delta_cross_seed_consensus
```

这是验证条件，不是预造结果：

- 核心消融下降最大，才能把差异驱动搜索写成主贡献。
- 如果 Oracle guard 下降最大，说明提升主要来自验证修复，不能把主算法排第一。
- 如果核心消融不下降，必须报告主张不成立；不能换用 accepted precision 或 residual progress 掩盖。
- 不得故意降低核心消融的模型、上下文或预算制造下降。
- 所有预注册消融都必须报告，不能删除不利结果。

### 7.4 防止同一 benchmark 反复调参

- 本计划和 Prompt 在 Full 正式运行前冻结。
- 运行后不根据单个 Gold-fixed 失败实例增加 issue ID 特判。
- 如果根据 Full Gold-side 结果再次修改算法，该次 Full 自动降级为开发实验；修改后必须重新运行 Full 和全部消融。
- 论文最终泛化证据还需要未用于调参的 TDD-Bench 或其他 held-out 集合；当前 SWT276 可作为工程主结果，但不能隐瞒其已被用于诊断。

## 8. 最终验收条件

### 8.1 指标提升

- 新 Full 必须严格高于旧 131/276。
- 为排除模型随机性，还必须高于同协议重新运行的 `w/o Semantic Delta Search`。
- Accepted Precision 必须高于 60.6%。
- Buggy PASS 最终导出必须为 0。
- fail-both accepted 必须低于 68。
- evaluator error 必须为 0，正式分母必须为 276。

目标区间是 145–165 条 F2P，但这只是工程目标，不能提前写成实验结果。

### 8.2 算法成立

- 每轮 LLM 修改前都收到明确的 `gap/preserve/change/avoid/operator/expected_effect`。
- STAGNANT 不重复相同 operator，REGRESSED 不从退化候选继续。
- StrictVerifier accept 不能越过 Buggy PASS、setup failure、high post-fix risk 和 high Oracle risk 门禁。
- 核心消融是三个单组件消融中 F2P 下降最大的一个。

### 8.3 未满足时的诚实处理

- Full 不超过 131：不能宣布指标提升。
- Full 不高于公平核心消融：不能宣布主算法有效。
- Oracle guard 的消融下降最大：贡献应定位为验证方法，或继续改进差异驱动机制后完整重跑。
- 单次差异很小：使用 paired McNemar 或实例级 bootstrap 判断不确定性，不写“稳定提升”。
- 任何失败都保留完整原始运行产物，不改分母、不删项目、不挑实例。

## 9. 完成定义

- 没有 AST 代码变换器。
- 没有新增生产算法模块。
- 没有新增 LLM 调用、候选执行、seed 数量或反馈预算。
- 现有 residual 被升级为 Semantic Delta Contract，而不是重新起一套框架。
- LLM 继续生成完整测试，复杂项目适配能力不被削弱。
- 结果驱动修复与主算法分别消融。
- Full 从 IssueRewrite 开始重跑 276；三个消融分别重跑完整 276 generation + official evaluation。
- 最终只用官方 F2P/276 判断效果，所有代理指标只做解释。

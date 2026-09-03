# 最小 C-I-O 语义差异变异实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在不修改执行反馈判定、checkpoint、Top-3 共识、候选排序和官方评测链路的前提下，把当前 11 个稀疏且重叠的 Trigger 算子升级为 C-I-O（Context、Interaction、Observation）三族、证据绑定、单差异驱动的语义变异，并修复“计划文字非空即视为已落实”的失真检查；`execution/feedback.py` 只调整 plan 路由和传参。

**架构：** Planner 只选择 `CONTEXT_MUTATION`、`INTERACTION_MUTATION`、`OBSERVATION_MUTATION` 或 `KEEP`，并输出一个 `seed_fact -> target_fact` 的原子 `frontier_delta` 和可机械审计的 `edit`。解析器把 `family + slot` 确定性映射为现有细粒度 `MutationStep.op`，从而保持下游接口不变；Generator 仍生成完整测试文件，Adherence 使用父代码、候选代码和精确 edit fragment 审计变异是否真正落实。`execution/feedback.py` 只允许调整 mutation plan 的调用路由，使 Trigger/Oracle 都能针对当前父候选形成一次 delta；checkpoint、Top-3 和最终筛选完全不变。

**技术栈：** Python dataclass、现有 Markdown Prompt、`ast`/`tokenize` 只读结构审计、pytest、现有 SWTBench 官方 F2P harness。禁止引入网络可信认证、Gold patch/test、SHA 或其他 hash 标识；不做 AST 代码改写。

---

## 1. 设计依据与范围锁定

第一次 SWT 276 官方结果为 `131/276 = 47.46%`。以 275 个 generation summary 为正式口径，共记录 990 次 planner call，其中 91 次 `VALID`。文件系统另有 997 份 plan artifact，其中 92 份 `VALID`；多出的 7 份是未进入最终 summary 计数的中间/遗留产物。以下算子频率仅用于 artifact-level 机制诊断，论文正式结果必须使用 990/91 的 summary 口径。119 个 artifact-level VALID step 中：

```text
ARG_VALUE_REPLACE       83
CALL_CHAIN_EXTEND       20
CONFIG_MUTATION          6
STATE_MUTATION           3
SERIALIZATION_TRIGGER    3
FIXTURE_DATA_MUTATION    2
LIFECYCLE_TRIGGER        1
OPERATOR_FLIP            1
ARG_BOUNDARY_EXPAND      0
MOCK_BEHAVIOR_MUTATION   0
WARNING_LOG_TRIGGER      0
```

前两个标签占 `103/119 = 86.6%`；最终候选中只有 2 个保留 `VALID plan`，且二者都是 `ARG_VALUE_REPLACE`。因此最小高收益修改不是新增更多算子，而是降低 Planner 分类歧义、提高 plan 覆盖率和 materialization 真实性。

### 本计划允许修改

```text
core/schema.py
mutation/seed_mutator.py
validation/mutation_plan_validator.py
validation/mutation_adherence.py
generation/generator.py                  # 仅修改 plan 消费提示和 adherence 入参
execution/feedback.py                    # 仅修改 mutation plan 调用路由，不改评分筛选
prompts/mutation_plan/system.md
prompts/mutation_plan/user.md
prompts/generation/user.md
tests/test_mutation_planner.py
tests/test_feedback_ablations.py          # 仅补兼容/生成消费回归测试
```

### 本计划明确冻结

```text
validation/strict_semantic_verifier.py
validation/semantic_guard.py
core/ablation.py
pipeline/run.py
repair_* prompts
checkpoint score/rank_key
Top-3 seed exploration 与 semantic consensus
最终候选选择
模型、temperature、轮数和调用预算
SWTBench 执行和官方评测逻辑
```

实现期间不得顺手重构冻结文件。若测试暴露冻结文件必须改变才能继续，应停止并记录依赖，不得扩大本计划。

`execution/feedback.py` 虽进入允许列表，但只允许修改初始/反馈轮的 plan 构造与传参：不得修改 `_checkpoint_score()`、`rank_key`、`_seed_result_score()`、`_semantic_signature()`、Top-3 循环或最终 selector。

### 端到端与退化语义

本文的“一次性端到端”指一次无人干预的系统运行完成：

```text
Issue + Repository + Retrieved Seeds
  -> 同一 C-I-O 差异变异算法的内部生成/执行/反馈
  -> 一个 final_test.py
```

内部允许沿用既有有限轮执行反馈，但不得读取 Gold、调用另一套旧算法或让人工挑选候选。`KEEP` 和 `ABORT_MUTATION -> KEEP` 都是同一状态机中的零编辑退化；最终仍只输出一个测试文件。

### 脏工作树执行约束

当前工作树在本计划生成前已有用户修改。每个任务中的 commit 命令仅适用于隔离且干净的专用 worktree；若在当前工作树执行，必须先记录目标文件的起始 diff，完成后只报告本任务新增 hunks，不得用整文件 `git add` 把既有用户修改一并提交，也不得 reset/checkout 任何既有改动。

## 2. 对外数据契约

Planner 的新输出只有两种。

### 2.1 单个原子变异

```json
{
  "schema_version": "semantic_delta.v2",
  "status": "PROPOSED",
  "action": "MUTATE",
  "family": "CONTEXT_MUTATION",
  "slot": "config",
  "seed_fact": "seed 显式设置 FILE_UPLOAD_PERMISSIONS",
  "target_fact": "目标场景保持 FILE_UPLOAD_PERMISSIONS 未设置",
  "edit": {
    "kind": "REMOVE",
    "before_fragment": "@override_settings(FILE_UPLOAD_PERMISSIONS=0o777)",
    "after_fragment": ""
  },
  "target_file": "django/core/files/storage.py",
  "target_symbol": "FileSystemStorage",
  "seed_evidence": [
    {"source": "seed", "content": "seed 中显式配置权限"}
  ],
  "target_evidence": [
    {"source": "issue", "content": "Issue 明确限定未显式配置场景"},
    {"source": "source", "content": "源码默认配置分支"}
  ],
  "protected": ["TemporaryUploadedFile", "storage.save", "现有 runner/fixture"],
  "forbidden": ["改成 MemoryUploadedFile", "修改无关 Oracle"],
  "done_when": {
    "execution_observation": "buggy 执行进入默认权限保存路径",
    "expected_relation": "保存后模式不是目标 0o644"
  },
  "confidence": "high",
  "risk": "low"
}
```

### 2.2 合法空动作

```json
{
  "schema_version": "semantic_delta.v2",
  "status": "ABSTAIN",
  "action": "KEEP",
  "reason": "没有足够的 issue/source/seed 证据支持安全变异",
  "family": "",
  "slot": "",
  "protected": ["当前测试协议"],
  "forbidden": [],
  "confidence": "low",
  "risk": "low"
}
```

`KEEP` 是同一规划器的零编辑动作；为兼容现有下游，内部继续表现为 `status=ABSTAIN` 且 `steps=[]`。不得新增“旧候选兜底”分支。

当已选择 `MUTATE` 但生成结果违反 plan 时，现有无 plan 重生成分支保留调用预算和兼容状态名 `FALLBACK_DIRECT`，但语义统一记录为 `ABORT_MUTATION -> KEEP`：它是同一算法内部终止未落实事务后的零编辑退化，不是调用另一个旧系统。

## 3. 三族与旧标签的兼容映射

Planner 只看三族，旧标签只作为解析后的实现记录：

```python
LEGACY_OP_BY_FAMILY_SLOT = {
    ("CONTEXT_MUTATION", "argument"): "ARG_VALUE_REPLACE",
    ("CONTEXT_MUTATION", "boundary"): "ARG_BOUNDARY_EXPAND",
    ("CONTEXT_MUTATION", "fixture"): "FIXTURE_DATA_MUTATION",
    ("CONTEXT_MUTATION", "config"): "CONFIG_MUTATION",
    ("CONTEXT_MUTATION", "mock"): "MOCK_BEHAVIOR_MUTATION",
    ("INTERACTION_MUTATION", "operator"): "OPERATOR_FLIP",
    ("INTERACTION_MUTATION", "call_chain"): "CALL_CHAIN_EXTEND",
    ("INTERACTION_MUTATION", "state"): "STATE_MUTATION",
    ("INTERACTION_MUTATION", "lifecycle"): "LIFECYCLE_TRIGGER",
    ("INTERACTION_MUTATION", "serialization"): "SERIALIZATION_TRIGGER",
    ("OBSERVATION_MUTATION", "return"): "ORACLE_REFOCUS",
    ("OBSERVATION_MUTATION", "exception"): "ORACLE_REFOCUS",
    ("OBSERVATION_MUTATION", "state"): "ORACLE_REFOCUS",
    ("OBSERVATION_MUTATION", "output"): "ORACLE_REFOCUS",
    ("OBSERVATION_MUTATION", "warning"): "ORACLE_REFOCUS",
    ("OBSERVATION_MUTATION", "log"): "ORACLE_REFOCUS",
}
```

`MutationStep.op` 保留，避免改动 Candidate、checkpoint、summary 和统计读取器。所有新产物同时记录 `family` 和 `slot`，论文只报告三族；旧标签可在附录中作为 realization slot。

---

### 任务 1：建立兼容的 C-I-O 原子差异契约

**文件：**
- 修改：`core/schema.py:189-232`
- 修改：`mutation/seed_mutator.py:24-241`
- 测试：`tests/test_mutation_planner.py`

- [ ] **步骤 1：添加新格式规范化的失败测试**

在 `MutationPlannerTests` 中增加：

```python
def test_v2_context_delta_normalizes_to_one_legacy_step(self) -> None:
    plan = _normalize_plan("demo__repo-1", 0, {
        "schema_version": "semantic_delta.v2",
        "status": "PROPOSED",
        "action": "MUTATE",
        "family": "CONTEXT_MUTATION",
        "slot": "argument",
        "seed_fact": "target_api receives 1",
        "target_fact": "target_api receives 2",
        "edit": {
            "kind": "REPLACE",
            "before_fragment": "target_api(1)",
            "after_fragment": "target_api(2)",
        },
        "target_file": "pkg/mod.py",
        "target_symbol": "target_api",
        "seed_evidence": [{"source": "seed", "content": "target_api(1)"}],
        "target_evidence": [{"source": "issue", "content": "value 2 is required"}],
        "protected": ["existing runner and fixture"],
        "forbidden": ["change the assertion"],
        "done_when": {
            "execution_observation": "target_api is invoked with value 2",
            "expected_relation": "buggy reaches the target branch",
        },
        "confidence": "high",
        "risk": "low",
    })
    self.assertEqual(plan.action, "MUTATE")
    self.assertEqual(plan.family, "CONTEXT_MUTATION")
    self.assertEqual(plan.slot, "argument")
    self.assertEqual(plan.mutation_ops, ["ARG_VALUE_REPLACE"])
    self.assertEqual(len(plan.steps), 1)
```

再增加：

```python
def test_v2_keep_is_a_safe_abstention(self) -> None:
    plan = _normalize_plan("demo__repo-1", 0, {
        "schema_version": "semantic_delta.v2",
        "status": "ABSTAIN",
        "action": "KEEP",
        "reason": "insufficient evidence",
        "protected": ["current protocol"],
    })
    self.assertEqual(plan.status, "ABSTAIN")
    self.assertEqual(plan.action, "KEEP")
    self.assertEqual(plan.steps, [])
```

保留并扩展现有旧格式测试，确认旧 `operator/gap/change` 和旧 `steps` JSON 仍能解析。

- [ ] **步骤 2：运行测试并确认失败原因正确**

运行：

```bash
PYTHONPATH=.. python -m pytest -q \
  tests/test_mutation_planner.py::MutationPlannerTests::test_v2_context_delta_normalizes_to_one_legacy_step \
  tests/test_mutation_planner.py::MutationPlannerTests::test_v2_keep_is_a_safe_abstention
```

预期：FAIL，原因是 `MutationPlan` 尚无 `action/family/slot`，且解析器尚不认识 `semantic_delta.v2`。

- [ ] **步骤 3：最小扩展 schema，不删除旧字段**

给 `MutationStep` 增加默认字段：

```python
family: str = ""
slot: str = ""
seed_fact: str = ""
target_fact: str = ""
seed_evidence: list[dict[str, str]] = field(default_factory=list)
target_evidence: list[dict[str, str]] = field(default_factory=list)
done_when: dict[str, str] = field(default_factory=dict)
edit_kind: str = "REPLACE"
```

给 `MutationPlan` 增加默认字段：

```python
schema_version: str = "semantic_delta.v1"
action: str = "MUTATE"
family: str = ""
slot: str = ""
protected: list[str] = field(default_factory=list)
forbidden: list[str] = field(default_factory=list)
confidence: str = "medium"
reason: str = ""
```

旧 plan 读取时保持 `semantic_delta.v1`；新 plan 写出 `semantic_delta.v2`。不要创建新的生产模块或重构 `JsonMixin`。

- [ ] **步骤 4：在 `seed_mutator.py` 增加三族常量和确定性映射**

加入：

```python
MUTATION_FAMILY_SLOTS = {
    "CONTEXT_MUTATION": {"argument", "boundary", "fixture", "config", "mock"},
    "INTERACTION_MUTATION": {"operator", "call_chain", "state", "lifecycle", "serialization"},
    "OBSERVATION_MUTATION": {"return", "exception", "state", "output", "warning", "log"},
}
```

使用本计划第 3 节的 `LEGACY_OP_BY_FAMILY_SLOT`。给现有 `ALLOWED_MUTATION_OPS` 增加 `ORACLE_REFOCUS`，但不得把三族和 12 个旧标签同时交给 LLM 选择。

- [ ] **步骤 5：实现 v2-first、v1-compatible 的 `_normalize_plan()`**

规则：

```text
action=KEEP
  -> status=ABSTAIN, steps=[]

action=MUTATE 且 family/slot 可映射
  -> 只构造一个 MutationStep
  -> step.op 使用兼容映射得到旧标签
  -> edit.kind 映射到 step.edit_kind
  -> edit.before_fragment/after_fragment 映射到旧 step.before/after，供机械落实检查

旧 operator/gap/change 或 steps 格式
  -> 继续走现有解析路径
  -> 不改变历史产物可读性
```

禁止 v2 plan 生成多个 step；遇到未知 family/slot 时返回 `INVALID`，不得猜测默认算子。

`edit.kind` 只允许：

```text
REPLACE：before_fragment 与 after_fragment 都必须非空；
INSERT：after_fragment 必须非空，before_fragment 可为空；
REMOVE：before_fragment 必须非空，after_fragment 必须为空。
```

- [ ] **步骤 6：运行契约和旧格式回归测试**

运行：

```bash
PYTHONPATH=.. python -m pytest -q tests/test_mutation_planner.py
```

预期：新契约测试通过；原有旧格式、ABSTAIN、service failure 和 lineage 测试继续通过。

- [ ] **步骤 7：提交本任务**

```bash
git add core/schema.py mutation/seed_mutator.py tests/test_mutation_planner.py
git commit -m "feat: 引入兼容的C-I-O原子差异契约"
```

---

### 任务 2：把 plan 有效性从“字段非空”升级为“两端证据绑定”

**文件：**
- 修改：`validation/mutation_plan_validator.py:155-301`
- 测试：`tests/test_mutation_planner.py`

- [ ] **步骤 1：为证据绑定和互斥边界编写失败测试**

先在测试类中增加一个返回完整合法 v2 输入的 helper：

```python
def _v2_delta(self) -> dict:
    return {
        "schema_version": "semantic_delta.v2",
        "status": "PROPOSED",
        "action": "MUTATE",
        "family": "CONTEXT_MUTATION",
        "slot": "argument",
        "seed_fact": "target_api receives 1",
        "target_fact": "target_api receives 2",
        "edit": {
            "kind": "REPLACE",
            "before_fragment": "target_api(1)",
            "after_fragment": "target_api(2)",
        },
        "target_file": "pkg/mod.py",
        "target_symbol": "target_api",
        "seed_evidence": [{"source": "seed", "content": "target_api(1)"}],
        "target_evidence": [{"source": "issue", "content": "value 2 is required"}],
        "protected": ["existing runner and fixture"],
        "forbidden": ["change the assertion"],
        "done_when": {
            "execution_observation": "target_api is called with 2",
            "expected_relation": "buggy reaches the target branch",
        },
        "confidence": "high",
        "risk": "low",
    }
```

然后实现以下具体测试：

```python
def test_v2_delta_requires_seed_and_target_evidence(self) -> None:
    for missing in ("seed_evidence", "target_evidence"):
        with self.subTest(missing=missing):
            data = self._v2_delta()
            data[missing] = []
            plan = self._validate(data)
            self.assertEqual(plan.status, "INVALID")
            self.assertTrue(any(missing in error for error in plan.validation_errors))

def test_v2_delta_rejects_equal_seed_and_target_facts(self) -> None:
    data = self._v2_delta()
    data["target_fact"] = data["seed_fact"]
    plan = self._validate(data)
    self.assertEqual(plan.status, "INVALID")
    self.assertTrue(any("distinct" in error for error in plan.validation_errors))

def test_v2_delta_rejects_family_slot_mismatch(self) -> None:
    data = self._v2_delta()
    data["slot"] = "warning"
    plan = self._validate(data)
    self.assertEqual(plan.status, "INVALID")
    self.assertTrue(any("family/slot" in error for error in plan.validation_errors))

def test_keep_does_not_become_invalid_for_having_no_steps(self) -> None:
    plan = self._validate({
        "schema_version": "semantic_delta.v2",
        "status": "ABSTAIN",
        "action": "KEEP",
        "reason": "insufficient evidence",
        "protected": ["current protocol"],
    })
    self.assertEqual(plan.status, "ABSTAIN")
    self.assertEqual(plan.steps, [])
```

另加三项互斥测试，每项都从 `_v2_delta()` 构造输入并明确断言 `status`：

| 测试 | 输入变化 | 预期 |
|---|---|---|
| `test_v2_delta_rejects_multiple_frontier_changes` | 额外加入第二个 `steps`/delta | `INVALID`，错误含 `exactly one` |
| `test_observation_delta_requires_oracle_refocus` | `family=OBSERVATION_MUTATION, slot=warning`，但强制旧 `op=CALL_CHAIN_EXTEND` | `INVALID`，错误含 `ORACLE_REFOCUS` |
| `test_context_delta_cannot_use_oracle_refocus` | `family=CONTEXT_MUTATION, slot=argument, op=ORACLE_REFOCUS` | `INVALID`，错误含 `family/slot` |

证据来源测试使用以下白名单：

```python
{"issue", "source", "seed", "execution", "verifier"}
```

至少要求：

```text
seed_fact 由 seed/execution/verifier 中至少一种支持；
target_fact 由 issue/source 中至少一种支持；
BehaviorTarget 不能作为唯一真实性证据；
seed_fact 与 target_fact 规范化后不能相同。
```

- [ ] **步骤 2：运行新增测试确认旧 validator 会错误放行或错误拒绝**

运行：

```bash
PYTHONPATH=.. python -m pytest -q tests/test_mutation_planner.py -k 'v2_delta or observation_delta or keep_does'
```

预期：FAIL，显示当前 validator 不认识 family/slot 和分端证据，且仍无条件拒绝 Oracle edit。

- [ ] **步骤 3：实现 `_valid_evidence_items()` 和 `_evidence_sources()`**

只接受形如：

```python
{"source": "issue", "content": "non-empty text"}
```

的证据。空内容、未知 source、非对象条目记录进 `validation_errors`。不要把模型置信度当事实证据。

- [ ] **步骤 4：为三族执行互斥验证**

规则：

```text
CONTEXT_MUTATION
  只接受 context slots；禁止 ORACLE_REFOCUS。

INTERACTION_MUTATION
  只接受 interaction slots；禁止 ORACLE_REFOCUS。

OBSERVATION_MUTATION
  只接受 observation slots；兼容 op 必须为 ORACLE_REFOCUS。
```

将当前 `_touches_oracle(step)` 的无条件拒绝改成：

```python
if step.family != "OBSERVATION_MUTATION" and _touches_oracle(step):
    reject
elif step.family == "OBSERVATION_MUTATION" and step.op != "ORACLE_REFOCUS":
    reject
```

保留现有 target file、target symbol、safety constraints 和 high-risk 拒绝逻辑。

- [ ] **步骤 5：验证 `done_when` 和 edit 可审计性**

所有 `MUTATE` plan 必须满足：

```text
edit.kind=REPLACE 时 before_fragment/after_fragment 都非空；
edit.kind=INSERT 时 after_fragment 非空；
edit.kind=REMOVE 时 before_fragment 非空且 after_fragment 为空；
提供的 fragment 必须是可在 seed/candidate 中进行 AST/token 审计的代码片段，不能是“修改一下”一类自然语言；
done_when.execution_observation 非空；
done_when.expected_relation 非空；
protected 至少一项；
risk 不能为 high。
```

`KEEP` 只要求 `reason` 非空，不要求 step、target file 或 target symbol。

- [ ] **步骤 6：在 validation evidence 中保留完整审计结果**

保存：

```json
{
  "schema_version": "semantic_delta.v2",
  "action": "MUTATE",
  "family_slot_valid": true,
  "seed_evidence_sources": ["seed"],
  "target_evidence_sources": ["issue", "source"],
  "facts_distinct": true,
  "edit_kind_valid": true,
  "done_when_complete": true,
  "validated_steps": 1
}
```

不修改 checkpoint rank 或最终筛选读取逻辑。

- [ ] **步骤 7：运行 validator 全量回归并提交**

```bash
PYTHONPATH=.. python -m pytest -q tests/test_mutation_planner.py
git add validation/mutation_plan_validator.py tests/test_mutation_planner.py
git commit -m "feat: 校验语义差异两端证据"
```

---

### 任务 3：把 Planner 的选择空间从 11 个标签收缩为三族

**文件：**
- 修改：`prompts/mutation_plan/system.md`
- 修改：`prompts/mutation_plan/user.md`
- 测试：`tests/test_mutation_planner.py`

- [ ] **步骤 1：添加 Prompt 契约失败测试**

把现有 `test_prompt_contains_source_and_seed_but_not_oracle_plan_fields` 改名并扩展为：

```python
def test_planner_prompt_exposes_three_families_and_keep(self) -> None:
    response = json.dumps({
        "schema_version": "semantic_delta.v2",
        "status": "ABSTAIN",
        "action": "KEEP",
        "reason": "seed already matches the evidenced trigger",
        "protected": ["current protocol"],
    })
    llm = _PlannerLLM(response=response)
    with tempfile.TemporaryDirectory() as tmp:
        build_mutation_plan(
            "demo__repo-1",
            0,
            self.behavior,
            self.host,
            self.protocol,
            llm,
            tmp,
            related_source=self.source,
            related_test=self.seed,
        )
    prompt = llm.prompts[0]
    self.assertIn("CONTEXT_MUTATION", prompt)
    self.assertIn("INTERACTION_MUTATION", prompt)
    self.assertIn("OBSERVATION_MUTATION", prompt)
    self.assertIn('"action": "KEEP"', prompt)
    output_contract = prompt.split("有安全计划时只输出：", 1)[-1]
    self.assertNotIn('"operator": "ARG_VALUE_REPLACE"', output_contract)
    self.assertNotIn("FAIL_TO_PASS", output_contract)
    self.assertNotIn("golden patch", output_contract.lower())
```

- [ ] **步骤 2：运行测试确认当前 Prompt 仍暴露 11 个同级算子**

```bash
PYTHONPATH=.. python -m pytest -q \
  tests/test_mutation_planner.py::MutationPlannerTests::test_planner_prompt_exposes_three_families_and_keep
```

预期：FAIL。

- [ ] **步骤 3：重写 system prompt 的唯一决策规则**

system prompt 只描述：

```text
把测试建模为 T=<Context, Interaction, Observation>。
比较 seed 与 Issue 目标，找到最前置、有证据支持的非空差异：
ΔC -> CONTEXT_MUTATION
否则 ΔI -> INTERACTION_MUTATION
否则 ΔO -> OBSERVATION_MUTATION
否则 KEEP
每轮只能输出一个 frontier delta。
```

保留禁止 Gold patch/test、禁止猜测源码符号、保留 ProtocolRecovery、安全约束和只输出 JSON 的现有规则。

- [ ] **步骤 4：重写 user prompt 输出契约**

Prompt 不再要求 LLM 从 11 个 `op` 中选择；只展示三族及合法 slot。明确：

```text
seed_fact 必须来自当前父候选（初始轮为 retrieved seed）或 execution；
target_fact 必须来自 Issue/source；
edit.kind 只能是 REPLACE/INSERT/REMOVE；before_fragment/after_fragment 是供落实审计使用的精确代码片段，不能填自然语言；
Observation Mutation 只能聚焦公开、可证伪、Issue 支持的观察；
不得增加 Issue 未规定的完整顺序、完整字符串或内部状态猜测；
证据不足时输出 KEEP，而不是编造变异。
```

- [ ] **步骤 5：验证 Prompt 大小、安全词和 JSON 示例**

运行：

```bash
PYTHONPATH=.. python -m pytest -q tests/test_mutation_planner.py -k 'prompt'
```

预期：Prompt 截断测试通过；输出契约只暴露三族；禁止 Gold 信息的断言通过。

- [ ] **步骤 6：提交本任务**

```bash
git add prompts/mutation_plan/system.md prompts/mutation_plan/user.md tests/test_mutation_planner.py
git commit -m "refactor: 将变异规划收缩为三个语义族"
```

---

### 任务 4：让 Generator 消费统一差异，而不改变候选筛选

**文件：**
- 修改：`generation/generator.py:425-650,655-1000`
- 修改：`execution/feedback.py:1811-1855,2215-2295`（只改 plan 路由）
- 修改：`mutation/seed_mutator.py:261-360`（接收当前父候选代码）
- 修改：`prompts/generation/user.md`
- 测试：`tests/test_feedback_ablations.py`

- [ ] **步骤 1：添加三族消费和后半段不变的失败测试**

增加三个测试，全部复用该文件现有的 `BehaviorTarget`、`HostContext`、
`ProtocolRecovery`、`_FakeLLM` 和 `generate_candidate(write_to_repo=False)` 测试夹具，
但输入与断言必须按下表写死：

| 测试 | plan 输入 | LLM 返回 | 必须断言 |
|---|---|---|---|
| `test_generation_renders_semantic_delta_family_not_trigger_only_label` | `VALID/MUTATE/CONTEXT_MUTATION/argument`，`edit=REPLACE(target_api(1), target_api(2))` | `def test_generated():\n    assert target_api(2) == 1\n` | `llm.calls[0][1]` 含 `Semantic Delta Plan`、`CONTEXT_MUTATION`，不含 `只在 Trigger 部分执行` |
| `test_keep_plan_is_not_injected_as_mutation_guidance` | `ABSTAIN/KEEP/steps=[]` | `def test_generated():\n    assert target_api(1) == 1\n` | user prompt 不含 `Semantic Delta Plan`；candidate plan status 为空 |
| `test_observation_delta_does_not_change_candidate_ranking_fields` | `VALID/MUTATE/OBSERVATION_MUTATION/return`，legacy op 为 `ORACLE_REFOCUS`；该测试的 BehaviorTarget 明确写 `expected result is 2` | `def test_generated():\n    assert target_api(1) == 2\n` | candidate plan status 为 `VALID`、risk 为 `low`，且 Candidate 字典没有 `rank_key` |

再使用现有 `_run_forced_decisions()` 增加路由测试：

```python
def test_oracle_focus_replans_once_from_current_parent_candidate(self) -> None:
    result, planner, repair, _, _, _, temp = self._run_forced_decisions(
        AblationConfig(), ["repair_oracle", "accept"]
    )
    self.addCleanup(temp.cleanup)
    self.assertEqual(planner.call_count, 2)  # initial + one post-execution replan
    self.assertEqual(result.trigger_replan_calls, 1)  # legacy summary field
    replan_kwargs = planner.call_args_list[1].kwargs
    self.assertIn("current_candidate_code", replan_kwargs)
    self.assertTrue(replan_kwargs["current_candidate_code"].strip())
    self.assertEqual(repair.call_count, 1)
```

该测试只验证 mutation 路由和预算，不断言或修改任何 rank key。

第三个测试只断言现有字段保持原语义：

```python
self.assertEqual(candidate.mutation_plan_status, "VALID")
self.assertEqual(candidate.mutation_plan_risk, "low")
self.assertNotIn("rank_key", candidate.to_dict())
```

不得在本任务测试或实现中 import/patch `_seed_result_score`、`_semantic_signature` 或最终 selector。

- [ ] **步骤 2：运行新增测试确认当前文案仍硬编码 Trigger-only**

```bash
PYTHONPATH=.. python -m pytest -q tests/test_feedback_ablations.py -k 'semantic_delta_family or keep_plan or observation_delta or oracle_focus_replans'
```

预期：FAIL，当前生成提示包含 `Trigger Mutation Plan` 和“只在 Trigger 部分执行”。

- [ ] **步骤 3：最小修改 plan 注入文案**

把 `generate_candidate()` 和 `repair_candidate()` 中的：

```text
Trigger Mutation Plan
只在 Trigger 部分执行
```

替换为统一规则：

```text
已通过结构与证据校验的 Semantic Delta Plan
只修改 plan.family 对应坐标：
CONTEXT 保留 Interaction/Observation；
INTERACTION 保留 Context/Observation；
OBSERVATION 保留 Context/Interaction。
把 seed_fact 转为 target_fact，保留 protected，避开 forbidden，满足 done_when。
```

不要提高每个 seed 的 planner 最大调用预算：仍为“初始 plan + 至多一次执行后 replan”。执行后这一次不再只保留给 Trigger，而是由当前 `focus` 在 Trigger/Oracle 中二选一。不要改变 generation/repair 的重试上限和 Candidate 构造字段。

- [ ] **步骤 4：更新基础 Generation Prompt**

在 `prompts/generation/user.md` 中将现有 `preserve/change/avoid` 说明替换为 v2-first 说明，同时保留旧 plan 兼容句：

```text
若 plan.schema_version=semantic_delta.v2，按 family/slot、seed_fact、target_fact、edit、protected、forbidden、done_when 执行；
若为旧 plan，继续按 preserve/change/avoid/operator 执行。
```

- [ ] **步骤 5：让执行后唯一一次 replan 使用当前父候选**

给 `build_mutation_plan()` 增加末尾可选参数，保持所有旧调用兼容：

```python
current_candidate_code: str = ""
```

构造 Prompt 时使用：

```python
seed_code = (
    current_candidate_code
    or (related_test.code_content if related_test else host.seed_test_code)
)
```

在 `execution/feedback.py` 中保留初始 plan；把现有仅 `focus == "trigger"` 的一次 replan 改为 `focus in {"trigger", "oracle"}`，并传入：

```python
current_candidate_code=candidate.code
```

Oracle 分支只把该 plan 作为 `repair_candidate(..., mutation_plan=plan)` 的指导，不得修改 verifier decision、checkpoint score 或最终选择。为兼容 summary，现有 `trigger_replan_calls` 字段暂不改名；在 v2 语义下它表示“执行后 delta replan 次数”，不再解释成只统计 Trigger。

- [ ] **步骤 6：给 adherence 提供父代码但不参与排序**

初始生成调用：

```python
assess_mutation_adherence(
    candidate.code,
    effective_plan,
    protocol,
    source_code=seed_test_code,
)
```

修复生成调用：

```python
assess_mutation_adherence(
    code,
    lineage_plan,
    protocol,
    source_code=candidate.code,
    oracle_baseline=existing_value,
)
```

`source_code` 只供落实审计，不写入 rank key，也不触发额外 Docker 执行。

这里的“不增加执行或模型调用”指不提高现有每个 seed 的最大预算：执行后仍至多一次 planner replan；只是把这一次从 Trigger-only 扩展为 Trigger/Oracle 二选一。

- [ ] **步骤 7：把未落实 plan 记录为统一退化语义**

保留现有重生成行为和兼容状态 `FALLBACK_DIRECT`，避免影响下游统计读取；同时把 fallback artifact 和 candidate adherence 增加：

```json
{
  "effective_action": "KEEP",
  "transition": "ABORT_MUTATION",
  "reason": "candidate did not materialize the validated delta"
}
```

不得调用另一个模型、另一个生成器或旧结果；无 plan prompt 是同一 Generator 的 `KEEP` 分支。

- [ ] **步骤 8：运行生成和反馈兼容测试并提交**

```bash
PYTHONPATH=.. python -m pytest -q \
  tests/test_feedback_ablations.py \
  tests/test_mutation_planner.py
git add generation/generator.py execution/feedback.py mutation/seed_mutator.py \
  prompts/generation/user.md tests/test_feedback_ablations.py
git commit -m "feat: 统一生成器的三族差异消费"
```

---

### 任务 5：修复语义 plan 的虚假落实判定

**文件：**
- 修改：`validation/mutation_adherence.py:187-320`
- 测试：`tests/test_mutation_planner.py`

- [ ] **步骤 1：添加当前实现会错误通过的失败测试**

至少增加四类；每个测试的父代码、候选代码和预期如下，不允许只断言字段存在：

| 测试 | source code | candidate code | 预期 |
|---|---|---|---|
| `test_v2_context_delta_requires_target_fragment_in_candidate` | `result = target_api(1)` | 仍为 `result = target_api(1)` | `VIOLATED`，`edit_materialized=False` |
| `test_v2_interaction_delta_requires_declared_call_chain_change` | `obj.prepare()` | 仍只有 `obj.prepare()`，plan 要求新增 `obj.commit()` | `VIOLATED`，新增调用不存在 |
| `test_v2_observation_delta_requires_target_oracle_and_forbidden_absence` | 同时包含冲突 baseline 和目标断言 | candidate 仍保留冲突 baseline | `VIOLATED`，`forbidden_absent=False` |
| `test_v2_delta_rejects_target_fact_mentioned_only_in_comment` | `target_api(1)` | 注释写 `target_api(2)`，实际仍调用 `target_api(1)` | `VIOLATED`，`edit_materialized=False` |

关键负例：

```python
source = "def test_x():\n    assert target_api(1) == 1\n"
candidate = (
    "def test_x():\n"
    "    # plan says target_api(2)\n"
    "    assert target_api(1) == 1\n"
)
```

当前代码会因为 `after` 非空且 target symbol 被调用而把 semantic plan 视为落实；新测试必须要求 `VIOLATED`。

- [ ] **步骤 2：运行新增测试确认旧 `after_matched = bool(after)` 问题**

```bash
PYTHONPATH=.. python -m pytest -q tests/test_mutation_planner.py -k 'v2_context_delta or v2_interaction_delta or v2_observation_delta or mentioned_only'
```

预期：至少 context/comment 负例失败。

- [ ] **步骤 3：扩展函数签名并保持调用兼容**

```python
def assess_mutation_adherence(
    candidate_code: str,
    plan: MutationPlan | None,
    protocol: ProtocolRecovery | None,
    oracle_baseline: str = "",
    assertion_baseline: str = "",
    source_code: str = "",
) -> dict[str, Any]:
```

把 `source_code` 放在末尾并设默认值，避免破坏现有位置参数调用。

- [ ] **步骤 4：删除 v2 的非空即匹配分支**

禁止继续使用：

```python
after_matched = bool(str(step.after or "").strip())
```

统一按照 `edit.kind` 通过 AST/token fragment 比较父代码与候选代码。注释和字符串中的提及不能视为代码 materialization。

- [ ] **步骤 5：实现三族最小落实证据**

共同条件：

```text
target symbol 仍在可执行调用中；
REPLACE：before_fragment 在父代码中，after_fragment 在候选中，且候选目标位置不再保留 before_fragment；
INSERT：after_fragment 出现在候选代码中；
REMOVE：before_fragment 在父代码中存在、在候选代码中消失；
forbidden 中可机械定位的代码片段不出现；
ProtocolRecovery 的 marks/decorators 继续审计。
```

分族条件：

```text
CONTEXT_MUTATION：候选落实 context edit；被替换/删除的旧参数或配置不能仍在目标调用路径生效。

INTERACTION_MUTATION：目标调用存在，且 edit 指定的新操作、状态修改或调用链结构存在；仅文字描述不算。

OBSERVATION_MUTATION：候选至少有一个公开、可证伪 Oracle 或 Issue 明确支持的 NO_EXCEPTION 观察；edit 指定的目标观察存在；被替换/删除的冲突 Oracle 或 forbidden 精确片段已移除；不得退化成裸 assert False。
```

AST 仅用于 `ast.parse/ast.walk/ast.dump` 的只读结构比较；不得生成或改写候选代码，不得使用 SHA/hash。

- [ ] **步骤 6：扩展 adherence 审计输出**

在不删除旧 key 的前提下增加：

```json
{
  "family": "CONTEXT_MUTATION",
  "slot": "argument",
  "source_fact_observed": true,
  "edit_kind": "REPLACE",
  "edit_materialized": true,
  "target_symbol_matched": true,
  "forbidden_absent": true,
  "status": "FULL"
}
```

`done_when` 是下一次真实执行需要验证的预期效果，Adherence 只检查代码层的 `edit_materialized`，不得在尚未执行时声称 `done_when` 已满足。

现有下游仍只读取 `status/violations/planned_steps`，因此筛选行为接口不变。

- [ ] **步骤 7：更新 lineage 重建以保留 v2 元数据**

`mutation_plan_from_candidate()` 从 `planned_steps` 重建时同步恢复：

```text
schema_version
action
family
slot
protected
forbidden
confidence
seed_fact/target_fact/evidence/done_when
```

增加 round-trip 测试，确认 repair round 不会把 v2 plan 退化成无 family 的旧 plan。

- [ ] **步骤 8：运行 adherence 与 lineage 全量测试并提交**

```bash
PYTHONPATH=.. python -m pytest -q tests/test_mutation_planner.py
git add validation/mutation_adherence.py tests/test_mutation_planner.py
git commit -m "fix: 按代码证据审计语义变异落实"
```

---

### 任务 6：回归验证和四实例效果闸门

**文件：**
- 不新增生产文件
- 只读取：`results/runs/brt6_swt276_deepseekv4flash_full_20260902_164451/`
- 运行产物写入新的 run 目录，不覆盖第一次 47.46% 基线

- [ ] **步骤 1：运行 mutation 单元测试**

```bash
PYTHONPATH=.. python -m pytest -q tests/test_mutation_planner.py
```

预期：全部通过。

- [ ] **步骤 2：运行既有核心回归测试**

```bash
PYTHONPATH=.. python -m pytest -q \
  tests/test_feedback_ablations.py \
  tests/test_mutation_planner.py \
  tests/test_official_benchmarks.py \
  tests/test_swtbench_runtime_compat.py
```

预期：无回归；当前基线为 70 tests 和 15 subtests 通过，若测试数量因本计划增加而上升，应记录新总数。

- [ ] **步骤 3：静态确认冻结文件没有被本计划改动**

相对于本计划开始时记录的工作树基线，检查本计划提交只涉及允许列表。特别确认没有新的修改落入：

```text
validation/strict_semantic_verifier.py
validation/semantic_guard.py
core/ablation.py
pipeline/run.py
```

这里只检查 `execution/feedback.py` 的改动是否严格限于 plan 构造/传参；该文件不再要求完全零 diff。评分、排序和筛选函数必须保持零新增修改。

工作树在计划开始前已经存在用户改动；不得把既有脏文件误判为本计划改动，也不得覆盖或回退它们。

- [ ] **步骤 4：选择四个互补 canary，不先跑全量 276**

```text
django__django-11133                 旧 VALID ARG plan，官方成功；检查不回退
scikit-learn__scikit-learn-14087   旧 VALID ARG plan，官方成功；检查不回退
django__django-10914                 旧错误接受 F2F；检查 Observation Mutation 删除冲突 baseline
sympy__sympy-21171                   旧错误接受 F2F；检查 Observation Mutation 不再猜测内部 LaTeX 类名
```

沿用项目当前生成入口和官方 F2P harness，使用新的 run name；不修改模型、temperature、Top-3、轮数和评测参数。不得覆盖旧 run。

- [ ] **步骤 5：检查机制指标而不只看最终 F2P**

四个样本逐项记录：

```text
Planner action/family/slot
seed/target evidence 是否分端存在
VALID/ABSTAIN/INVALID
adherence FULL/PARTIAL/VIOLATED
旧成功是否保持 F2P
旧 F2F 是否转为 F2P
是否出现 setup/collect/syntax 新失败
```

进入完整 276 重跑的最低条件：

```text
两个旧成功样本均不得回退；
四个样本均不得新增 setup/collect/syntax failure；
所有声称 VALID 的 v2 plan 必须有 FULL adherence；
至少一个旧 F2F 转为 F2P，作为进入全量实验的效果闸门。
```

`KEEP` 只表示没有执行差异变异，不能证明 Oracle 可信，也不能阻止现有 Verifier 错误接受，因此不得把 `KEEP` 当作 F2P 或筛选正确性的替代指标。

- [ ] **步骤 6：运行完成前验证并提交验证记录**

如果四实例闸门通过，记录 run 路径、官方报告路径和上述机制指标。只提交代码、Prompt、测试和小型文本记录，不提交大型 run 产物。

```bash
git add core/schema.py mutation/seed_mutator.py \
  validation/mutation_plan_validator.py validation/mutation_adherence.py \
  generation/generator.py execution/feedback.py prompts/mutation_plan/system.md \
  prompts/mutation_plan/user.md prompts/generation/user.md \
  tests/test_mutation_planner.py tests/test_feedback_ablations.py
git commit -m "test: 验证三族语义差异变异"
```

---

## 4. 最终验收标准

### 功能正确性

- Planner 主接口只暴露 C-I-O 三族和 `KEEP`，不再让模型在 11 个重叠标签中选择。
- 每个 `MUTATE` 只有一个 frontier delta。
- `seed_fact` 与 `target_fact` 分别绑定允许来源的证据。
- `family + slot` 确定性映射到现有 `MutationStep.op`。
- `KEEP` 安全退化为现有 ABSTAIN 路径，不新增旧算法 fallback。
- 未落实的 `MUTATE` 统一记录为 `ABORT_MUTATION -> KEEP`；`FALLBACK_DIRECT` 仅作为历史兼容状态名。
- Observation Mutation 可以删除冲突/过强 Oracle，但不能凭空增加 Issue 未支持的完整输出猜测。
- semantic plan 不再因文字字段非空就获得 FULL adherence；必须按 `edit.kind` 验证代码 fragment。

### 兼容性

- 历史 v1 plan JSON 仍能读取。
- `MutationStep.op`、`MutationPlan.is_usable`、Candidate 字段和 summary 字段不删除、不改名。
- `execution/feedback.py` 仅允许修改 mutation plan 路由；Top-3、rank key、最终选择和官方评测不变。
- 每个 seed 的最大 planner 调用预算、generation/repair 次数、执行轮数和 Docker 次数不增加。

### 论文可解释性

正文只需解释：

```text
Test = <Context, Interaction, Observation>
Delta = <ΔC, ΔI, ΔO>
选择最前置、有证据支持的非空坐标进行一次原子变异；
证据不足时执行 KEEP。
```

旧 11 个标签只作为 implementation slots 放在附录，不再声称是 11 个独立算法设计。

### 停止条件

出现以下任一情况时停止扩大实现：

- 需要改 candidate ranking 或 Top-3 selector 才能让测试通过；
- 需要提高每个 seed 的现有模型调用上限或新增 Docker 执行；
- v2 plan 只能通过降低证据要求获得覆盖率；
- Observation Mutation 必须读取 Gold patch/test 才能判断；
- canary 中两个旧成功样本出现回退。

这些问题应形成下一阶段独立计划，而不是混入本次最小变异修改。

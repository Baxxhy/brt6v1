# 执行反馈驱动的单 Delta 闭环重构计划

> **面向当前会话的实现者：** 使用 `superpowers:executing-plans` 在当前会话内逐任务执行；禁止使用子智能体。每完成一个任务先运行该任务的定向测试，再创建一个只包含该任务文件的 Git commit。不得使用 `git reset --hard`、`git checkout --` 或覆盖用户已有修改。

**目标：** 将当前“规划—重型静态验证—adherence—无计划 fallback—有限 repair”流水线，重构为三个 seed 相互独立的执行反馈闭环：每轮只提出并应用一个 C/I/O Delta，任何候选语法、收集、运行或语义错误都进入下一轮反馈，每个 seed 最多执行 5 轮，最后使用现有候选选择策略导出一个 `final_test.py`。

**架构：** 每个 seed 维护唯一的 `current_test` 和按轮次增长的 `delta_history`。统一循环执行 `分析残余差异 → 生成单 Delta → 修改当前测试 → 最低限度静态检查 → 真实执行 → Verifier`；Verifier 接受时结束该 seed，否则把错误原样交给下一轮。静态检查只保护数据格式、Python 语法、唯一测试入口、测试文件边界和无 Gold 泄漏，不再尝试静态证明语义正确。三个 seed 必须全部进入各自循环，互不共享候选和反馈；最终 selector 只消费每个 seed 的最佳 checkpoint，评分公式不在本计划中修改。

**技术栈：** Python dataclass、现有 LLM client、现有项目 runner、现有 Strict Verifier、pytest/unittest、JSON 轨迹文件、Git。禁止使用网络可信认证、Gold patch/test、FAIL_TO_PASS、PASS_TO_PASS、SHA 或其他 hash 标识。

---

## 1. 最终算法契约

### 1.1 三个独立 seed 轨迹

```text
Issue + BehaviorTarget + Retrieved Top-3 Tests
                    |
        +-----------+-----------+
        |           |           |
      Seed 0      Seed 1      Seed 2
        |           |           |
      <=5 轮      <=5 轮      <=5 轮
        |           |           |
        +-----------+-----------+
                    |
      现有 selector 选择一个 final_test.py
```

“三个 seed 都要运行”定义为：只要存在三个检索 seed，三个分支都必须被调用。某一分支获得 Verifier `accept` 后可提前结束该分支，但不能跳过其他分支。错误 seed 在 5 轮后允许保持 unresolved；不能要求三个 seed 全部成功才输出 final test，否则无关 seed 会否决已经成功的候选。

### 1.2 单 seed 闭环

```python
current_test = retrieved_seed
best_checkpoint = None

for round_id in range(5):
    delta = propose_delta(
        issue=issue,
        target=behavior_target,
        current_test=current_test,
        previous_execution=previous_execution,
        previous_verifier=previous_verifier,
        delta_history=delta_history,
    )
    candidate = generate_from_delta(current_test, delta)
    static_result = check_candidate(candidate)

    if not static_result.ok:
        execution = synthetic_execution_from_static_error(static_result)
        verifier = synthetic_repair_decision(static_result)
    else:
        execution = run_candidate(candidate)
        verifier = verify_candidate(issue, candidate, execution)

    save_round(delta, candidate, execution, verifier)
    update_best_checkpoint(candidate, execution, verifier)

    if verifier.decision == "accept":
        break

    current_test = candidate
    previous_execution = execution
    previous_verifier = verifier

return best_checkpoint
```

每轮只能有一次语义生成，不允许在同一轮中因 adherence 失败再次进行无计划生成。JSON 解析失败可以进行一次“仅修复 JSON 格式”的协议重试；该重试不得改变语义，也不计作新的 Delta 轮。

### 1.3 Delta 唯一数据格式

删除 `semantic_delta.v1`、`MutationStep`、`steps`、细粒度 legacy op、`target_file`、`target_symbol`、精确 before/after AST 契约和双版本解析。运行时代码只接受：

```json
{
  "schema_version": "semantic_delta.v2",
  "action": "MUTATE",
  "dimension": "CONTEXT",
  "seed_fact": "当前候选的可观察事实",
  "target_fact": "Issue 或源码要求的目标事实",
  "change": "本轮唯一需要实施的修改",
  "preserve": ["当前轮默认保持的行为"],
  "avoid": ["从此前执行中已经证伪的路径"],
  "reason": "为什么该 Delta 是当前最前置差异"
}
```

`dimension` 只允许：

```text
CONTEXT
INTERACTION
OBSERVATION
```

证据不足时只允许：

```json
{
  "schema_version": "semantic_delta.v2",
  "action": "KEEP",
  "dimension": "",
  "seed_fact": "当前候选事实",
  "target_fact": "当前无法可靠到达的目标事实",
  "change": "",
  "preserve": [],
  "avoid": [],
  "reason": "当前 seed 无法产生可靠的单步差异"
}
```

`KEEP` 不调用旧算法。若尚未达到 5 轮，下一轮使用相同 `current_test`，但把本次 KEEP 和原因加入历史，提示 Planner 不得重复相同判断；连续两次语义相同的 KEEP 后结束该 seed，避免五轮完全空转。

### 1.4 错误反馈统一进入 Delta

| 当前结果 | 下一轮默认 dimension | 反馈内容 |
| --- | --- | --- |
| JSON 结构错误 | 不进入语义轮；协议重试一次 | JSON 解析错误 |
| Python `SyntaxError` / `IndentationError` | CONTEXT | 文件、行号、异常文本、当前候选 |
| pytest/unittest collect error | CONTEXT | runner 命令、导入/fixture/nodeid 错误 |
| 候选自身 import/setup/runtime error | CONTEXT | traceback 和 failure origin |
| 测试执行通过，未触发 buggy 行为 | INTERACTION | 未命中目标 API/路径的证据 |
| 在无关路径失败 | INTERACTION 或 CONTEXT，由 Verifier 决定 | 已到达路径、实际失败位置 |
| 命中目标但断言错误/过强 | OBSERVATION | 实际输出和 Issue 支持的期望关系 |
| Issue-aligned buggy failure 且 Oracle 合法 | ACCEPT | 当前 seed 结束 |
| Docker/image/仓库本身不可用 | 不交给 LLM | 保持环境错误状态，防止让测试代码修基础设施 |

静态错误必须保存为与真实执行相同结构的 `ExecutionResult`，使下一轮 Planner 不需要第二套输入协议。

### 1.5 上游变化与下游失效

“每轮一个 Delta”不等于其他坐标永远严格不变：

```text
Delta C 可以使 I/O 变成 UNKNOWN；本轮不能同时修 I/O。
Delta I 可以使 O 变成 UNKNOWN；本轮不能同时修 O。
下一轮根据执行结果继续闭合最前置残余差异。
```

Planner prompt 必须明确：`preserve` 是默认保持，不是静态硬约束；如果执行证明下游契约因上游变化失效，应在下一轮修改下游，而不是回退本轮候选。

## 2. 文件级变更与删除清单

### 创建

```text
validation/delta_guard.py              # 最低限度 Delta/候选静态检查
validation/oracle_contract.py          # 从旧 adherence 文件迁出的 Oracle 工具
execution/delta_loop.py                # 单 seed 五轮统一闭环
tests/test_semantic_delta.py            # 新 Delta 数据与 guard 测试
tests/test_delta_loop.py                # 反馈闭环与 Top-3 行为测试
```

### 重写或修改

```text
core/schema.py                          # 使用 SemanticDelta，删除 MutationStep/旧 MutationPlan
core/config.py                          # 新增 DEFAULT_MAX_SEMANTIC_ROUNDS = 5
mutation/seed_mutator.py                # 只解析 v2 单 Delta，不再做重型验证
generation/generator.py                 # 统一 current_test + delta → candidate
execution/feedback.py                   # 委托 delta_loop；保留环境准备/checkpoint/selector
pipeline/run.py                         # 暴露 --max_semantic_rounds，默认 5
validation/semantic_guard.py            # Oracle 工具 import 改到 oracle_contract
validation/oracle_risk.py               # Oracle 工具 import 改到 oracle_contract
prompts/mutation_plan/system.md          # 简化为残余差异选择
prompts/mutation_plan/user.md            # 唯一 v2 JSON 契约
prompts/generation/user.md               # 强制基于 current_test 和单 Delta 输出完整文件
tests/test_feedback_ablations.py         # 删除旧 replanning/fallback 断言，保留非本算法测试
docs/BRT6_CURRENT_IMPLEMENTATION_FLOW_DETAILED.md
```

### 删除

```text
validation/mutation_plan_validator.py    # 重型静态语义验证全部移除
validation/mutation_adherence.py         # gate/lineage 删除；Oracle 工具迁出后删除
generation/observation_oracle.py         # Observation 统一由 Delta loop 处理
tests/test_mutation_planner.py            # 旧 v1/v2/legacy/adherence 测试整体删除
docs/superpowers/plans/2026-09-03-minimal-cio-semantic-delta-mutation.md
docs/superpowers/plans/2026-09-03-brt6-semantic-difference-end-to-end.md
```

删除后必须运行：

```bash
rg -n "semantic_delta\.v1|MutationStep|FALLBACK_DIRECT|ABORT_MUTATION|mutation_plan_validator|mutation_adherence|mutation_plan_from_candidate|trigger_replan_calls < 1|LEGACY_OP_BY_FAMILY_SLOT" . --glob '!results/**' --glob '!.git/**'
```

预期：源代码、测试和当前文档中无匹配；历史只存在于 Git，不保留兼容代码、注释掉的实现或 `deprecated_*` 文件。

---

## 3. 实施任务

### 任务 1：建立唯一 SemanticDelta 契约并删除 legacy 数据模型

**文件：**
- 修改：`core/schema.py:188-252`
- 重写：`mutation/seed_mutator.py`
- 创建：`tests/test_semantic_delta.py`
- 删除：`tests/test_mutation_planner.py`

- [ ] **步骤 1：为唯一 v2 契约编写失败测试**

在 `tests/test_semantic_delta.py` 覆盖：

```python
def test_mutate_delta_accepts_one_dimension():
    delta = normalize_delta({
        "schema_version": "semantic_delta.v2",
        "action": "MUTATE",
        "dimension": "CONTEXT",
        "seed_fact": "seed calls api(1)",
        "target_fact": "issue requires api(2)",
        "change": "replace api(1) with api(2)",
        "preserve": ["runner and test entry"],
        "avoid": [],
        "reason": "input is the earliest missing behavior",
    })
    assert delta.is_actionable
    assert delta.dimension == "CONTEXT"


def test_keep_delta_is_not_actionable():
    delta = normalize_delta({
        "schema_version": "semantic_delta.v2",
        "action": "KEEP",
        "dimension": "",
        "seed_fact": "seed fact",
        "target_fact": "unknown target",
        "change": "",
        "preserve": [],
        "avoid": [],
        "reason": "insufficient evidence",
    })
    assert not delta.is_actionable


def test_v1_and_steps_contract_are_rejected():
    with pytest.raises(ValueError):
        normalize_delta({"schema_version": "semantic_delta.v1", "steps": []})
```

- [ ] **步骤 2：运行测试确认旧模型不满足契约**

运行：

```bash
pytest -q tests/test_semantic_delta.py
```

预期：FAIL，原因是 `SemanticDelta`/`normalize_delta` 尚不存在或 v1 仍被接受。

- [ ] **步骤 3：删除旧数据模型并实现 SemanticDelta**

在 `core/schema.py` 删除：

```text
MutationStep
MutationPlan.steps
MutationPlan.mutation_ops
family/slot 双层 legacy 表示
target_file/target_symbol/before/after/edit_kind/done_when
```

新增：

```python
@dataclass
class SemanticDelta(JsonMixin):
    instance_id: str
    round_id: int
    action: str
    dimension: str = ""
    seed_fact: str = ""
    target_fact: str = ""
    change: str = ""
    preserve: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    reason: str = ""
    schema_version: str = "semantic_delta.v2"

    @property
    def is_actionable(self) -> bool:
        return self.action == "MUTATE" and self.dimension in {
            "CONTEXT", "INTERACTION", "OBSERVATION"
        }
```

将 `mutation/seed_mutator.py` 重写为：

```text
render_delta_prompt()
normalize_delta()
build_semantic_delta()
save delta_round_N_raw.json / delta_round_N.json
```

删除 `_step_from_dict()`、`_step_from_string()`、`LEGACY_OP_BY_FAMILY_SLOT`、`ALLOWED_MUTATION_OPS`、v1 兼容解析和 `validate_mutation_plan()` 调用。

- [ ] **步骤 4：运行新契约测试**

```bash
pytest -q tests/test_semantic_delta.py
```

预期：PASS。

- [ ] **步骤 5：提交唯一 Delta 契约**

```bash
git add core/schema.py mutation/seed_mutator.py tests/test_semantic_delta.py tests/test_mutation_planner.py
git commit -m "refactor: replace legacy mutation plans with semantic delta"
```

### 任务 2：把重型静态验证替换成最低限度 guard

**文件：**
- 创建：`validation/delta_guard.py`
- 删除：`validation/mutation_plan_validator.py`
- 修改：`tests/test_semantic_delta.py`

- [ ] **步骤 1：编写 guard 失败测试**

测试必须覆盖：

```python
def test_guard_accepts_valid_python_without_exact_fragment_matching():
    result = check_delta_candidate(
        delta=make_delta("CONTEXT"),
        candidate_code="def test_brt():\n    assert target_api(2)\n",
        expected_test_name="test_brt",
    )
    assert result.ok


def test_guard_returns_feedback_for_syntax_error():
    result = check_delta_candidate(
        delta=make_delta("CONTEXT"),
        candidate_code="def test_brt(:\n    pass\n",
        expected_test_name="test_brt",
    )
    assert not result.ok
    assert result.error_kind == "SYNTAX_ERROR"
    assert "invalid syntax" in result.feedback.lower()


def test_guard_rejects_multiple_dimensions():
    delta = make_delta("CONTEXT,OBSERVATION")
    assert not check_delta_structure(delta).ok


def test_guard_rejects_production_file_target():
    result = check_candidate_scope(
        candidate_repo_path="pkg/production.py",
        expected_test_path="tests/test_brt.py",
    )
    assert not result.ok
```

- [ ] **步骤 2：确认测试失败**

```bash
pytest -q tests/test_semantic_delta.py
```

- [ ] **步骤 3：实现最小 guard**

`delta_guard.py` 只实现：

```text
check_delta_structure(delta)
check_python_syntax(code)
check_unique_test_entry(code, expected_name)
check_candidate_scope(candidate_repo_path, expected_test_path)
check_forbidden_leakage(prompt_or_delta)
check_delta_candidate(...)
```

禁止在该文件加入：

```text
精确 fragment 匹配
target symbol grounding
Oracle fingerprint 相等门禁
evidence source 完整性证明
family-slot-to-op 映射
risk score
adherence 状态
```

- [ ] **步骤 4：删除重型 validator 并确认无 import**

```bash
rg -n "mutation_plan_validator|validate_mutation_plan" . --glob '!results/**' --glob '!.git/**'
```

预期：无匹配。

- [ ] **步骤 5：运行测试并提交**

```bash
pytest -q tests/test_semantic_delta.py
git add validation/delta_guard.py validation/mutation_plan_validator.py tests/test_semantic_delta.py
git commit -m "refactor: reduce static validation to executable guards"
```

### 任务 3：删除 adherence gate，保留独立 Oracle 工具

**文件：**
- 创建：`validation/oracle_contract.py`
- 删除：`validation/mutation_adherence.py`
- 修改：`validation/semantic_guard.py`
- 修改：`validation/oracle_risk.py`
- 修改：`generation/generator.py`
- 修改：`tests/test_semantic_delta.py`

- [ ] **步骤 1：为迁移后的 Oracle 工具编写测试**

```python
def test_oracle_contract_detects_assert_and_exception():
    code = (
        "def test_x():\n"
        "    with pytest.raises(ValueError):\n"
        "        target_api()\n"
    )
    assert "EXCEPTION" in oracle_kinds(code)


def test_oracle_contract_is_diagnostic_not_a_delta_gate():
    report = describe_oracle_contract("def test_x():\n    assert target_api() == 2\n")
    assert report["kinds"]
    assert "status" not in report
```

- [ ] **步骤 2：迁移可复用函数**

从 `mutation_adherence.py` 仅迁移：

```text
oracle_kinds()
oracle_fingerprint()
describe_oracle_contract()
```

更新三个调用文件的 import。

- [ ] **步骤 3：删除所有 adherence 和 lineage 路径**

删除：

```text
assess_mutation_adherence()
mutation_plan_from_candidate()
mutation_adherence 字段的运行时写入
mutation_round_N_adherence.json
mutation_round_N_nonadherent.py
FALLBACK_DIRECT
ABORT_MUTATION
generation_round_N_plan_fallback.txt
```

`CandidateTest` 中若 `mutation_plan_status`、`mutation_plan_risk`、`mutation_adherence` 只服务旧路径，则同时从 `core/schema.py` 删除；输出 summary 使用 `delta_history` 和 `last_delta` 替代。

- [ ] **步骤 4：修复候选落盘一致性**

所有轮次统一写：

```python
write_text(candidate_path, candidate.code)
```

禁止在 candidate 已被替换后继续写旧局部变量 `code`。

- [ ] **步骤 5：运行测试与删除扫描**

```bash
pytest -q tests/test_semantic_delta.py
rg -n "FALLBACK_DIRECT|ABORT_MUTATION|assess_mutation_adherence|mutation_plan_from_candidate" . --glob '!results/**' --glob '!.git/**'
```

预期：测试 PASS；扫描无匹配。

- [ ] **步骤 6：提交 adherence 删除**

```bash
git add core/schema.py generation/generator.py validation/oracle_contract.py validation/mutation_adherence.py validation/semantic_guard.py validation/oracle_risk.py tests/test_semantic_delta.py
git commit -m "refactor: remove mutation adherence gate and fallback"
```

### 任务 4：统一候选生成接口

**文件：**
- 修改：`generation/generator.py`
- 删除：`generation/observation_oracle.py`
- 修改：`prompts/generation/user.md`
- 创建：`tests/test_delta_loop.py`

- [ ] **步骤 1：编写统一生成接口测试**

测试使用 fake LLM，验证传入信息：

```python
def test_generate_from_delta_uses_current_candidate_not_original_seed(tmp_path):
    current = "def test_brt():\n    assert api(2) == 1\n"
    delta = make_delta(
        "OBSERVATION",
        change="replace the stale expected value with the issue-supported relation",
    )
    candidate = generate_from_delta(
        current_test=current,
        delta=delta,
        execution_feedback="assert 2 == 1",
        verifier_feedback="target hit; oracle wrong",
        llm_client=FakeLLM("def test_brt():\n    assert api(2) == 2\n"),
        output_dir=tmp_path,
    )
    assert "api(2) == 2" in candidate.code
    assert current in FakeLLM.last_prompt
```

- [ ] **步骤 2：确认测试失败**

```bash
pytest -q tests/test_delta_loop.py::test_generate_from_delta_uses_current_candidate_not_original_seed
```

- [ ] **步骤 3：实现唯一生成入口**

保留或新建：

```python
generate_from_delta(
    instance_id,
    current_test,
    semantic_delta,
    execution_feedback,
    verifier_feedback,
    protocol,
    behavior,
    source_context,
    llm_client,
    output_dir,
    round_id,
) -> CandidateTest
```

删除旧的分叉语义：

```text
initial plan generation
trigger-specific repair
oracle-specific repair
observation-oracle rebind
guidance_plan/lineage_plan
no-plan fallback generation
```

环境依赖修复可以保留在 `execution/feedback.py` 的基础设施层，但不能再生成测试代码。

- [ ] **步骤 4：修改 generation prompt**

Prompt 必须明确：

```text
输入中的“当前候选”是本轮唯一父节点。
只实施 delta.change 对应的一个 dimension。
返回完整 Python 测试文件。
不得回到 retrieved seed 重写。
不得重复 delta_history 中已证伪的 change。
允许上游变化使下游状态变为 UNKNOWN，但不得同轮主动修两个 dimension。
```

- [ ] **步骤 5：删除单独 Observation Oracle 文件并更新 import**

```bash
rg -n "observation_oracle|rebind_observation_oracle" . --glob '!results/**' --glob '!.git/**'
```

预期：无匹配。

- [ ] **步骤 6：运行测试并提交**

```bash
pytest -q tests/test_delta_loop.py tests/test_semantic_delta.py
git add generation/generator.py generation/observation_oracle.py prompts/generation/user.md tests/test_delta_loop.py
git commit -m "refactor: unify test generation around one semantic delta"
```

### 任务 5：实现每轮错误反馈和五轮闭环

**文件：**
- 创建：`execution/delta_loop.py`
- 修改：`execution/feedback.py:1804-2469`
- 修改：`core/config.py`
- 修改：`pipeline/run.py`
- 修改：`tests/test_delta_loop.py`

- [ ] **步骤 1：编写语法错误反馈测试**

```python
def test_syntax_error_is_fed_to_next_delta_round():
    planner = FakePlanner([
        make_delta("CONTEXT", change="add target argument"),
        make_delta("CONTEXT", change="repair malformed function signature"),
    ])
    generator = FakeGenerator([
        "def test_brt(:\n    pass\n",
        "def test_brt():\n    assert target_api(2)\n",
    ])
    verifier = FakeVerifier(["accept"])

    result = run_seed_delta_loop(...)

    assert result.accepted
    assert result.rounds_used == 2
    assert "SYNTAX_ERROR" in planner.calls[1].execution_feedback
```

- [ ] **步骤 2：编写 collection/runtime/semantic 反馈参数化测试**

```python
@pytest.mark.parametrize(
    ("status", "expected_dimension"),
    [
        ("COLLECT_ERROR", "CONTEXT"),
        ("SETUP_ERROR", "CONTEXT"),
        ("PASS", "INTERACTION"),
        ("UNRELATED_FAIL", "INTERACTION"),
        ("ORACLE_WRONG", "OBSERVATION"),
    ],
)
def test_each_failure_becomes_next_round_feedback(status, expected_dimension):
    ...
```

- [ ] **步骤 3：编写停止与预算测试**

覆盖：

```text
round 1 accept → planner/generator/executor 各调用 1 次
前 4 轮失败、round 5 accept → 各调用 5 次
5 轮全部失败 → rounds_used=5 且 unresolved
不得出现第 6 个 delta/candidate/execution 文件
连续两次相同 KEEP → 结束 seed
```

- [ ] **步骤 4：实现 `run_seed_delta_loop()`**

职责限制为：

```text
维护 current_test
调用 build_semantic_delta
调用 generate_from_delta
运行最低限度 guard
把 guard 错误转换为 ExecutionResult
运行项目 runner
调用现有 Strict Verifier
保存 round artifact
维护 best checkpoint
ACCEPT/预算停止
```

每轮保存：

```text
delta_round_N_raw.json
delta_round_N.json
candidate_round_N.py
execution_round_N.json
verifier_round_N.json
checkpoint_round_N.json
```

- [ ] **步骤 5：从旧 feedback 主循环删除分叉**

删除：

```text
trigger_replan_calls < 1
initial_plan 与 usable_initial_plan
oracle/trigger 分别规划
semantic_repairs_used 与 brt_attempt 的双预算
连续 STAGNANT 直接终止
RESTORE_PARENT 后绕开统一 Delta 的 repair
FALLBACK_DIRECT 统计
```

`execution/feedback.py` 保留：

```text
仓库/容器准备
Top-3 seed 调度
checkpoint 评分函数
最终 selector
summary 导出
官方运行所需 command/nodeid
```

- [ ] **步骤 6：新增独立预算参数**

在 `core/config.py`：

```python
DEFAULT_MAX_SEMANTIC_ROUNDS = 5
```

在 `pipeline/run.py`：

```python
parser.add_argument(
    "--max_semantic_rounds",
    type=int,
    default=DEFAULT_MAX_SEMANTIC_ROUNDS,
)
```

旧 `max_feedback_rounds/max_brt_rounds` 若只服务被删除循环，则从 CLI、函数签名、配置导出和脚本中删除；环境预算继续使用独立 `max_env_rounds`，默认不因语义轮数增加而扩大。

- [ ] **步骤 7：运行闭环测试并提交**

```bash
pytest -q tests/test_delta_loop.py tests/test_semantic_delta.py
git add execution/delta_loop.py execution/feedback.py core/config.py pipeline/run.py tests/test_delta_loop.py
git commit -m "feat: iterate one execution-guided delta for up to five rounds"
```

### 任务 6：保证三个 seed 全部运行并只在末尾选择

**文件：**
- 修改：`execution/feedback.py:1328-1616`
- 修改：`tests/test_delta_loop.py`
- 修改：`tests/test_feedback_ablations.py`

- [ ] **步骤 1：编写固定 Top-3 测试**

```python
def test_all_three_seed_trajectories_run_even_when_first_accepts():
    results = run_top3(..., seed_results=["accept", "unresolved", "accept"])
    assert [item.seed_index for item in results.attempts] == [0, 1, 2]
    assert all(item.rounds_used >= 1 for item in results.attempts)


def test_final_selector_receives_one_best_checkpoint_per_seed():
    results = run_top3(...)
    assert selector.call_count == 1
    assert len(selector.call_args.args[0]) == 3
```

- [ ] **步骤 2：确认当前提前退出逻辑导致测试失败**

```bash
pytest -q tests/test_delta_loop.py -k "three_seed or selector"
```

- [ ] **步骤 3：固定运行三个 seed**

将 Top-3 循环改成：

```python
attempts = []
for seed_index, seed in enumerate(seeds_to_try[:3]):
    attempts.append(run_seed_delta_loop(seed_index=seed_index, ...))

selected_attempt = select_best_seed_attempt(attempts)
```

删除 `_should_try_next_seed()` 对是否运行后续 seed 的控制。如果该函数没有其他调用，则删除函数及对应测试。保留现有 selector 的 rank key、语义一致性和风险规则，不因 Delta 是否存在而强行加分。

- [ ] **步骤 4：更新旧测试**

从 `tests/test_feedback_ablations.py` 删除或改写：

```text
test_full_method_limits_trigger_replanning_to_one_call
FALLBACK_DIRECT 断言
trigger_replan_calls == 1 断言
旧 mutation_ops/MutationStep 构造
```

保留并适配：

```text
mutation=False 时三个 seed 仍独立运行
环境失败不污染其他 seed
最终 selector 不选择不可导出的候选
runner/protocol 被每个 seed 保留
```

- [ ] **步骤 5：运行 Top-3 与 ablation 测试并提交**

```bash
pytest -q tests/test_delta_loop.py tests/test_feedback_ablations.py
git add execution/feedback.py tests/test_delta_loop.py tests/test_feedback_ablations.py
git commit -m "refactor: run three independent delta trajectories before selection"
```

### 任务 7：简化 Planner 提示词并强化残余差异反馈

**文件：**
- 重写：`prompts/mutation_plan/system.md`
- 重写：`prompts/mutation_plan/user.md`
- 修改：`tests/test_semantic_delta.py`

- [ ] **步骤 1：编写 prompt 契约测试**

断言 prompt：

```text
包含 current_test、execution_feedback、verifier_feedback、delta_history
包含 CONTEXT/INTERACTION/OBSERVATION
包含“最前置未满足差异”和“每轮一个 Delta”
包含语法/收集/运行错误如何路由
不包含 steps、legacy op、target_file、target_symbol、before_fragment、after_fragment
不包含静态证明 adherence 的要求
```

- [ ] **步骤 2：重写 system prompt**

System prompt 只承担：

```text
识别当前候选已满足事实
识别 Issue 目标事实
根据真实执行判断最前置残余差异
只输出一个 C/I/O Delta 或 KEEP
避免重复历史中已证伪的 change
不得读取或猜测 Gold
```

- [ ] **步骤 3：重写 user prompt**

输入顺序固定为：

```text
Issue
BehaviorTarget
Protocol
相关源码
当前候选
上一轮执行结果
上一轮 Verifier 结果
Delta 历史
唯一 JSON schema
```

初始轮的执行和 Verifier 字段使用空对象，不切换到另一份初始 prompt。

- [ ] **步骤 4：运行测试并提交**

```bash
pytest -q tests/test_semantic_delta.py
git add prompts/mutation_plan/system.md prompts/mutation_plan/user.md tests/test_semantic_delta.py
git commit -m "refactor: prompt one residual semantic delta per round"
```

### 任务 8：删除旧文件、旧字段和陈旧文档

**文件：**
- 删除：`validation/mutation_plan_validator.py`
- 删除：`validation/mutation_adherence.py`
- 删除：`generation/observation_oracle.py`
- 删除：`tests/test_mutation_planner.py`
- 删除：`docs/superpowers/plans/2026-09-03-minimal-cio-semantic-delta-mutation.md`
- 删除：`docs/superpowers/plans/2026-09-03-brt6-semantic-difference-end-to-end.md`
- 修改：`docs/BRT6_CURRENT_IMPLEMENTATION_FLOW_DETAILED.md`
- 修改：项目内仍引用旧参数/字段的脚本与测试

- [ ] **步骤 1：运行全仓旧代码扫描**

```bash
rg -n "semantic_delta\.v1|MutationStep|MutationPlan|mutation_ops|FALLBACK_DIRECT|ABORT_MUTATION|mutation_plan_status|mutation_plan_risk|mutation_adherence|mutation_plan_validator|assess_mutation_adherence|mutation_plan_from_candidate|observation_oracle|rebind_observation_oracle|LEGACY_OP_BY_FAMILY_SLOT|trigger_replan_calls" . --glob '!results/**' --glob '!.git/**'
```

- [ ] **步骤 2：逐个删除剩余引用**

要求：

```text
不创建 compatibility.py
不保留 deprecated alias
不保留注释掉的旧实现
不保留双写旧字段和新字段
不为历史结果目录修改解析兼容
```

历史实验继续由当时的 JSON 文件保存；新代码只读取新运行生成的 `delta_history`。

- [ ] **步骤 3：更新当前流程文档**

文档只描述：

```text
Top-3 独立 seed
每 seed 最多 5 轮
单 Delta
最低限度 guard
错误反馈
Verifier ACCEPT
最终 selector
```

删除旧的重型 validator、adherence、fallback 和一次 replan 流程描述。

- [ ] **步骤 4：确认扫描无匹配并提交**

```bash
rg -n "semantic_delta\.v1|MutationStep|FALLBACK_DIRECT|ABORT_MUTATION|mutation_plan_validator|mutation_adherence|observation_oracle|LEGACY_OP_BY_FAMILY_SLOT|trigger_replan_calls" . --glob '!results/**' --glob '!.git/**'
git add -A validation generation tests docs core mutation execution pipeline prompts scripts
git commit -m "chore: remove superseded mutation pipeline code"
```

在脏工作树执行时，不得使用上面的宽范围 `git add -A`；必须改为逐文件暂存本计划产生的 hunks，避免提交用户已有改动。

### 任务 9：完整静态与单元验证

**文件：**
- 不修改生产代码；测试失败时回到对应任务修复

- [ ] **步骤 1：编译受影响模块**

```bash
python -m py_compile \
  core/schema.py \
  core/config.py \
  mutation/seed_mutator.py \
  validation/delta_guard.py \
  validation/oracle_contract.py \
  generation/generator.py \
  execution/delta_loop.py \
  execution/feedback.py \
  pipeline/run.py
```

预期：退出码 0。

- [ ] **步骤 2：运行定向测试**

```bash
pytest -q \
  tests/test_semantic_delta.py \
  tests/test_delta_loop.py \
  tests/test_feedback_ablations.py
```

预期：全部 PASS。

- [ ] **步骤 3：运行项目测试集**

```bash
pytest -q tests
```

预期：全部 PASS；任何失败必须根据首个失败回到对应任务修复，不得通过跳过、xfail 或放宽无关断言掩盖。

- [ ] **步骤 4：检查算法不变量**

通过单元测试确认：

```text
每个 seed 每轮恰好一个 Delta
每轮最多一次语义生成
每轮最多一次候选执行
语法/收集/运行错误进入下一轮反馈
单 seed 最多 5 轮
三个 seed 全部运行
一个 seed ACCEPT 不跳过其他 seed
不存在 fallback direct
不存在旧 v1/legacy 代码
最终仍只导出一个 final_test.py
```

- [ ] **步骤 5：检查 Git 变更边界**

```bash
git status --short
git diff --check
git diff --stat
```

预期：无空白错误；所有变更都属于本计划；不包含结果目录、密钥、环境文件或无关用户修改。

- [ ] **步骤 6：提交验证收尾**

仅当本任务产生测试/文档修复时提交：

```bash
git add <本任务实际修改的测试或文档文件>
git commit -m "test: verify execution-guided delta loop"
```

## 4. 明确不做的事情

本计划不修改：

```text
检索算法和 Top-3 排名来源
BehaviorTarget 生成方式
Strict Verifier 的语义判断标准
最终 selector 的 rank_key/score
官方 SWTBench F2P evaluator
LLM provider/model
生产项目代码
Gold 数据访问规则
```

本计划不新增：

```text
Agent 框架
多智能体协作
长期记忆或向量数据库
新的细粒度 mutation operator
第二套 fallback 算法
为了兼容历史结果而保留的旧运行时代码
```

## 5. 完成条件

只有同时满足以下条件才算实现完成：

1. 三个 seed 都进入独立闭环；
2. 每个 seed 最大语义轮数为 5；
3. 每轮生成并应用一个且仅一个 Delta；
4. Syntax/collect/candidate runtime/trigger/oracle 错误都能进入下一轮反馈；
5. 静态 guard 不再因为 exact fragment、target symbol 或 Oracle 修改猜测拒绝候选；
6. 不存在 adherence gate 和 `FALLBACK_DIRECT`；
7. 不存在 v1、legacy op 或双解析路径；
8. 三个分支结束后仍只输出一个 `final_test.py`；
9. 定向测试和全量测试通过；
10. 当前实现文档只描述新闭环，旧实现由 Git 历史保存。

## 6. 实现后的实验边界

本计划只实现并验证算法，不自动启动正式实验。代码验证通过后，下一阶段再使用冻结的 BehaviorTarget、相同模型配置和相同 seed 排名进行配对评测，至少分别报告：

```text
官方 F2P
每轮 ACCEPT 累积曲线
Syntax/collect/runtime/trigger/oracle 错误修复率
Delta dimension 分布
平均成功轮数
Top-1 与 Top-3 差异
1/3/5 轮消融
无约束 iterative repair 对照
```

这样才能判断 F2P 提升来自单 Delta 闭环，而不是 BehaviorTarget 重生成、随机采样或最终 selector 波动。

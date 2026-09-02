# BRT6 目录与入口重构实现计划

> **面向 AI 代理的工作者：** 本计划按 TDD 执行；步骤使用复选框记录进度。

**目标：** 将嵌套 BRT6 项目上移到工作区根目录，统一当前入口为 `brt6`，并保持运行行为与持久化兼容契约不变。

**架构：** 保留功能包和三个 CLI 入口，移除没有当前引用的转发模块，收敛项目根目录、当前导入路径、活动脚本与文档。持久化环境名、缓存路径、schema 版本和历史结果标识不改名。

**技术栈：** Python 3、`unittest`、bash、现有 BRT6 功能包与官方 benchmark 运行时。

---

## 文件职责

- 移动：嵌套目录 `brt6/` 的全部源码、测试、脚本、数据和文档到项目根目录。
- 修改：`__init__.py`，声明 BRT6 身份。
- 删除：没有当前引用的根目录和子包转发模块；保留 `run.py`、`run_issue_rewrite.py`、`direct_eval.py` 三个 CLI 入口。
- 修改：`scripts/*.py`、`scripts/*.sh`，将当前代码导入和 CLI 模块统一为 `brt6`；保留历史环境变量与环境名前缀。
- 修改：`tests/*.py`，让测试验证当前 `brt6` 包，而不是旁边 checkout 中的 `brt5` 包。
- 修改：`README.md`、`README_RUN.md`、`README_STRUCTURE.md`、`README_REPRODUCE.md` 及当前流程文档，统一可复制的路径和入口。
- 新增：`tests/test_project_layout.py`，保护单层目录、当前包名和活动入口契约。
- 新增：本设计文档与本实现计划，记录边界与验证方式。

## 任务 1：目录布局与当前包身份

**文件：**
- 新增：`tests/test_project_layout.py`
- 修改：`__init__.py`
- 移动：嵌套 `brt6/` 到项目根目录

- [x] **步骤 1：编写失败测试**

```python
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_checkout_is_single_layer_brt6_root():
    assert PROJECT_ROOT.name == "brt6"
    assert not (PROJECT_ROOT / "brt6").is_dir()
    assert (PROJECT_ROOT / "__init__.py").is_file()
```

- [x] **步骤 2：运行测试确认失败**

运行：`cd /root/Baxxhy/BugReproduce/brt6/brt6/.. && python -m unittest discover -s tests -p 'test_project_layout.py' -v`

预期：失败，因为源码根目录下仍有嵌套 `brt6/`。

- [x] **步骤 3：实现最小布局变更**

执行：将当前 `/root/Baxxhy/BugReproduce/brt6/brt6` 的全部内容移动到其父目录；不覆盖父目录已有文件（当前父目录为空）。

修改 `__init__.py`：

```python
"""BRT6: behavior-preserving bug reproduction test generation."""

__version__ = "0.1.0"
```

- [x] **步骤 4：运行测试确认通过**

运行：`cd /root/Baxxhy/BugReproduce && python -m unittest discover -s brt6/tests -p 'test_project_layout.py' -v`

预期：PASS。

## 任务 2：当前导入路径与脚本入口

**文件：**
- 修改：`tests/*.py` 中指向本项目的 `brt5.*` 导入和 patch 字符串
- 修改：`scripts/bootstrap_fresh_swt_server.sh`、`scripts/run_generate.sh`、`scripts/run_issue_rewrite.sh`、`scripts/run_evaluate.sh`、`scripts/run_formal_eval_after_generation.py`、`scripts/prepare_tdd_template_environments.py`、`scripts/prewarm_swt_environments.py`、`scripts/preflight_tdd_local_conda.py`、`scripts/rerun_tdd_no_run_140.py`
- 新增：`tests/test_project_layout.py` 中的入口断言

- [x] **步骤 1：编写失败测试**

```python
def test_active_launchers_use_brt6_module():
    root = Path(__file__).resolve().parents[1]
    assert "python -m brt6.run" in (root / "scripts/run_generate.sh").read_text()
    assert "python -m brt6.run_issue_rewrite" in (root / "scripts/run_issue_rewrite.sh").read_text()
    assert "python -m brt6.direct_eval" in (root / "scripts/run_evaluate.sh").read_text()
```

- [x] **步骤 2：运行测试确认失败**

运行：`cd /root/Baxxhy/BugReproduce && python -m unittest discover -s brt6/tests -p 'test_project_layout.py' -v`

预期：失败，因为活动 shell 入口仍调用 `brt4.*`。

- [x] **步骤 3：编写最小实现**

将活动脚本中的本项目导入从 `brt5.*` 改为 `brt6.*`，将 `python -m brt4.*` 改为 `python -m brt6.*`；不改 `BRT4_*`、`BRT3_*`、`brt5i_*` 等兼容环境标识。测试 patch 目标同步指向 `brt6.*`。

- [x] **步骤 4：运行测试确认通过**

运行：`cd /root/Baxxhy/BugReproduce && python -m unittest discover -s brt6/tests -p 'test_project_layout.py' -v`

预期：PASS。

## 任务 3：运行文档和历史边界

**文件：**
- 修改：`README.md`、`README_RUN.md`、`README_STRUCTURE.md`、`README_REPRODUCE.md`
- 修改：`docs/BRT4_CURRENT_PIPELINE_20260706.md`、`docs/BRt4_IMPROVEMENTS_20260706.md`、`SELF_CHECK.md`、`FINAL_CODE_AUDIT.md` 中当前入口说明
- 新增：`tests/test_project_layout.py` 中的文档入口断言

- [x] **步骤 1：编写失败测试**

```python
def test_run_guide_points_to_brt6_root():
    root = Path(__file__).resolve().parents[1]
    run_guide = (root / "README_RUN.md").read_text()
    structure = (root / "README_STRUCTURE.md").read_text()
    assert "cd /root/Baxxhy/BugReproduce/brt6" in run_guide
    assert "python -m brt6.run" in run_guide
    assert "BRT6 Structure" in structure
```

- [x] **步骤 2：运行测试确认失败**

运行：`cd /root/Baxxhy/BugReproduce && python -m unittest discover -s brt6/tests -p 'test_project_layout.py' -v`

预期：失败，因为文档仍以 BRT4 或 BRT5 为当前项目。

- [x] **步骤 3：编写最小实现**

把可执行命令、项目路径、包名和当前项目标题改为 BRT6；在保留的历史文档中明确其日期和历史版本性质，不改历史结果数字、缓存 schema 或运行标识。

- [x] **步骤 4：运行测试确认通过**

运行：`cd /root/Baxxhy/BugReproduce && python -m unittest discover -s brt6/tests -p 'test_project_layout.py' -v`

预期：PASS。

## 任务 4：完整离线验证

**文件：** 不新增代码；复核任务 1–3 的全部变更。

- [x] 运行：`cd /root/Baxxhy/BugReproduce && python -m compileall -q brt6`
- [x] 运行：`cd /root/Baxxhy/BugReproduce && python -m unittest discover -s brt6/tests -p 'test_*.py' -v`（247 项通过，含转发壳清理回归测试）
- [x] 运行：`cd /root/Baxxhy/BugReproduce/brt6 && bash -n scripts/*.sh`
- [x] 运行：`cd /root/Baxxhy/BugReproduce && python -m brt6.run --help`、`python -m brt6.run_issue_rewrite --help`、`python -m brt6.direct_eval --help`
- [x] 确认本次未启动项目长进程、LLM 请求、Docker 容器、正式生成或评测；仅运行离线测试与 CLI help。
- [x] 检查 Git：该目录及其父目录没有有效 Git 仓库，因此未伪造提交或 diff 状态。
- [x] 清理验证生成的 `__pycache__`；保留作为 Python 包标记的空 `__init__.py`。
- [x] 删除 35 个无当前引用的转发模块；保留三个 CLI 入口文件和功能实现包。

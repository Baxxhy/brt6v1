"""Candidate generation and repair."""

from __future__ import annotations

import json
import os
import ast
import re
import textwrap
from pathlib import Path
from typing import Any

from ..io.io_utils import format_code_context
from ..core.prompts import (
    MUTATION_GENERATION_SYSTEM_PROMPT,
    MUTATION_GENERATION_USER_PROMPT,
    REPAIR_ORACLE_SYSTEM_PROMPT,
    REPAIR_ORACLE_USER_PROMPT,
    REPAIR_GENERIC_SYSTEM_PROMPT,
    REPAIR_GENERIC_USER_PROMPT,
    REPAIR_SETUP_SYSTEM_PROMPT,
    REPAIR_SETUP_USER_PROMPT,
    REPAIR_TRIGGER_SYSTEM_PROMPT,
    REPAIR_TRIGGER_USER_PROMPT,
)
from ..core.ablation import (
    AblationConfig,
    behavior_prompt_payload,
    render_ablation_prompt,
)
from ..core.behavior_evidence import (
    BehaviorEvidence,
    expected_behavior_text,
    issue_evidence_text,
    is_behavior_target,
    render_evidence_prompt,
    suspected_bug_locations,
)
from ..core.schema import CandidateTest, ExecutionResult, HostContext, MutationPlan, ProtocolRecovery, RetrievedCode, RetrievedTest
from ..validation.mutation_adherence import (
    assess_mutation_adherence,
    mutation_plan_from_candidate,
    oracle_contract_is_strict_subset,
    oracle_fingerprint,
    oracle_kinds,
)
from ..validation.semantic_guard import audit_candidate, oracle_contract_summary
from ..core.utils import (
    clean_code_block,
    ensure_dir,
    safe_json_dump,
    sanitize_instance_id,
    truncate_text,
    write_text,
)


MAX_PROMPT_BEHAVIOR_CHARS = 30_000
MAX_PROMPT_HOST_CHARS = 30_000
MAX_PROMPT_SOURCE_CHARS = 60_000
MAX_PROMPT_SEED_CHARS = 40_000
MAX_PROMPT_ISSUE_CHARS = 30_000
MAX_PROMPT_PROTOCOL_CHARS = 20_000
MAX_PROMPT_EXECUTION_CHARS = 25_000
MAX_PROMPT_CANDIDATE_CHARS = 25_000
MAX_PROMPT_FEEDBACK_CHARS = 12_000


def _prompt_text(value: str, limit: int) -> str:
    return truncate_text(str(value or ""), limit)


def _prompt_json(value: Any, limit: int) -> str:
    return _prompt_text(json.dumps(value, ensure_ascii=False), limit)


def _allows_target_reachability_oracle_pruning(
    focus: str,
    verifier_feedback: dict[str, Any] | None,
    before: str,
    after: str,
    behavior: BehaviorEvidence,
) -> bool:
    """Allow only removal of a blocking control Oracle on target-not-hit."""

    feedback = verifier_feedback or {}
    return bool(
        focus == "trigger"
        and str(feedback.get("failure_class") or "") == "target_not_hit"
        and oracle_contract_is_strict_subset(before, after)
        and oracle_contract_summary(behavior, after).get("falsifiable")
    )


def _source_window(path: Path, object_name: str, max_chars: int = 10000) -> tuple[str, int, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if len(text) <= max_chars:
        return text, 1, len(lines)
    simple_name = object_name.rsplit(".", 1)[-1].strip()
    match_index = -1
    if simple_name:
        pattern = re.compile(
            rf"^\s*(?:class|def|async\s+def)\s+{re.escape(simple_name)}\b"
        )
        for index, line in enumerate(lines):
            if pattern.search(line):
                match_index = index
                break
    if match_index < 0:
        match_index = 0
    start = max(0, match_index - 25)
    end = min(len(lines), match_index + 150)
    excerpt = "\n".join(lines[start:end])
    return excerpt[:max_chars], start + 1, end


def _effective_source_context(
    behavior: BehaviorEvidence,
    related_source: list[RetrievedCode],
    buggy_repo: str,
) -> list[RetrievedCode]:
    effective = list(related_source)
    seen = {(item.path, item.obj_name) for item in effective}
    repo_root = Path(buggy_repo).resolve() if buggy_repo else None
    if not repo_root or not repo_root.is_dir():
        return effective
    for location in suspected_bug_locations(behavior):
        relative = str(location.get("path") or "").strip().replace("\\", "/")
        object_name = str(location.get("object") or "").strip()
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            continue
        source_path = (repo_root / relative).resolve()
        try:
            source_path.relative_to(repo_root)
        except ValueError:
            continue
        if not source_path.is_file():
            continue
        try:
            content, start, end = _source_window(source_path, object_name)
        except OSError:
            continue
        matching_retrievals = [
            item
            for item in effective
            if item.path == relative and item.obj_name == object_name
        ]
        # A retrieval hit with the same path/object may contain only a narrow
        # method fragment. Keep the current buggy-repo window unless an
        # existing hit already contains that whole window, otherwise lifecycle
        # methods such as check()/deconstruct() can disappear from constraints.
        if content.strip() and any(
            content.strip() in item.code_content for item in matching_retrievals
        ):
            continue
        effective.append(
            RetrievedCode(
                instance_id=behavior.instance_id,
                obj_name=object_name,
                node_type="inferred_source_location",
                path=relative,
                code_start_line=start,
                code_end_line=end,
                code_content=content,
                raw={"source": "behavior_target.suspected_bug_locations"},
            )
        )
        seen.add((relative, object_name))
    return effective


def format_effective_source_context(
    behavior: BehaviorEvidence,
    related_source: list[RetrievedCode],
    buggy_repo: str,
) -> str:
    return format_code_context(
        _effective_source_context(behavior, related_source, buggy_repo)
    )


def _persistent_setup_failure(code: str, execution_log: str) -> str:
    if (
        'near "[]": syntax error' in execution_log
        and "ArrayField" in code
        and not re.search(r"\bmanaged\s*=\s*False\b", code)
    ):
        return (
            "SQLite 在测试数据库建表阶段尝试创建 PostgreSQL ArrayField，尚未进入"
            "目标测试。若 Issue 只需要表单/field/formset 行为，所有测试内声明且含"
            "ArrayField 的临时 Model 必须设置 Meta.managed = False，并避免依赖这些"
            "表的数据库读写；或复用 HostContext 中已验证的 PostgreSQL 测试环境。"
            "skipUnlessDBFeature 无法阻止测试数据库在测试方法执行前建表，因此不是修复。"
        )
    if (
        "fetch_command" in execution_log
        and "KeyError:" in execution_log
        and (
            "execute_from_command_line" in code
            or re.search(r"\.\s*run_manage\s*\(", code)
        )
    ):
        return (
            "management command 未注册，执行日志在 fetch_command 中抛出 KeyError，"
            "但返回代码仍通过 execute_from_command_line()/run_manage() 查找该临时命令。"
            "对于 parser/help 行为，应直接实例化测试内 Command，调用 create_parser()、"
            "format_help()/print_help() 或 run_from_argv()；不要依赖运行中进程刷新"
            "临时 app 的命令注册表。"
        )
    if "<locals>" in execution_log:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            tree = None
        nested_definitions: list[str] = []
        if tree is not None:
            def visit(node: ast.AST, inside_function: bool = False) -> None:
                is_function = isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                )
                if inside_function and isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    nested_definitions.append(node.name)
                for child in ast.iter_child_nodes(node):
                    visit(child, inside_function or is_function)

            visit(tree)
        if nested_definitions:
            names = ", ".join(sorted(set(nested_definitions)))
            return (
                "执行日志中的序列化路径包含 '<locals>'，而返回代码仍在测试函数"
                f"内部定义待序列化的类或函数（{names}）。必须把这些定义真正移动到"
                "模块顶层，使其 __module__ 和 __qualname__ 可导入；只在注释中声称"
                "“module-level”或仅重命名无效。MigrationWriter、deconstruct、"
                "pickle 和 serializer 使用的自定义类型都适用此规则。"
            )
    missing_import = re.search(
        r"ImportError:\s+cannot import name\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)",
        execution_log,
        re.IGNORECASE,
    )
    if missing_import:
        symbol = missing_import.group(1)
        if re.search(
            rf"(?m)^\s*from\s+\S+\s+import\s+[^\n]*\b{re.escape(symbol)}\b",
            code,
        ):
            return (
                f"返回代码仍然直接导入执行日志明确不存在的符号 {symbol!r}。"
                "必须删除这条 direct import，并采用 HostContext 中已验证的导入方式；"
                "若目标是带数字前缀的迁移模块，可使用 importlib.import_module。"
            )
    missing_module = re.search(
        r"ModuleNotFoundError:\s+No module named\s+['\"]([^'\"]+)",
        execution_log,
        re.IGNORECASE,
    )
    if missing_module:
        module = missing_module.group(1)
        if re.search(re.escape(module), code):
            return (
                f"返回代码仍然引用环境中不存在的模块 {module!r}，"
                "可能位于 import、INSTALLED_APPS、app_label 或配置字符串中。"
                "必须删除该虚构模块，并复用 HostContext 中已存在的项目模型、应用和 import。"
            )
    return ""


def _semantic_path_problem(
    behavior: BehaviorEvidence,
    source_context: str,
    code: str,
    *,
    issue_text: str = "",
    execution_log: str = "",
) -> str:
    generic_problem = audit_candidate(
        behavior,
        code,
        issue_text=issue_text,
        execution_log=execution_log,
    )
    if generic_problem:
        return generic_problem
    behavior_text = issue_evidence_text(behavior).lower()
    mentions_check = any(
        marker in behavior_text
        for marker in ("check", "检查", "验证")
    )
    describes_missing_check = any(
        marker in behavior_text
        for marker in (
            "add check",
            "missing",
            "no check",
            "缺少",
            "没有",
            "新增",
            "添加",
        )
    )
    if (
        mentions_check
        and describes_missing_check
        and re.search(r"\bdef\s+check\s*\(", source_context)
    ):
        if not re.search(r"\.\s*check\s*\(", code):
            evidence_name = (
                "BehaviorTarget" if is_behavior_target(behavior) else "原始 Issue"
            )
            return (
                f"{evidence_name} 描述的是缺失检查，且相关源码明确暴露 check() 生命周期，"
                "但返回测试没有调用 check()。必须保留正确 setup，并调用真实 check()，"
                "断言修复后应出现的检查结果。"
            )
        expects_exception = any(
            marker in expected_behavior_text(behavior).lower()
            for marker in ("抛出", "异常", "raise", "exception")
        )
        if not expects_exception and re.search(
            r"(?:assertRaises|pytest\.raises)", code
        ):
            return (
                "Expected behavior 没有要求抛异常；不得用 raises 包裹 check()。"
                "应断言 check() 返回的稳定错误/警告证据存在。"
            )
    mentions_logging = any(
        marker in behavior_text
        for marker in ("logger", "logging", "log message", "日志", "记录异常")
    )
    describes_missing_logging = any(
        marker in behavior_text
        for marker in ("missing", "doesn't have", "没有", "缺少", "未记录")
    )
    if mentions_logging and describes_missing_logging:
        if re.search(r"(?:patch|patch\.object)\s*\([^)]*logger", code):
            return (
                "Issue 描述的是生产代码缺少日志；不得 patch 一个 buggy 源码中尚不存在的"
                " logger 属性。应使用 assertLogs/caplog 捕获目标模块的日志命名空间，"
                "让 buggy 因没有日志证据而失败。"
            )
        if "assertLogs" not in code and "caplog" not in code:
            return (
                "缺失日志类 BRT 必须通过 assertLogs 或 caplog 观察修复后应出现的日志，"
                "不能仅检查异常返回值。"
            )
    return ""


def _wrap_if_needed(code: str, host: HostContext, safe_id: str) -> str:
    code = textwrap.dedent(clean_code_block(code)).strip() + "\n"
    if not code.strip():
        return f"def test_brt_{safe_id}():\n    assert True\n"
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            if node.args.args and node.args.args[0].arg == "self":
                class_name = "TestBRT" + "".join(part.capitalize() for part in safe_id.split("_") if part)
                return "import unittest\n\n\nclass " + class_name + "(unittest.TestCase):\n" + textwrap.indent(code, "    ")
            break
    if safe_id.startswith("django__django_"):
        top_level_tests = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name.startswith("test")
            and not node.args.args
            and not node.args.kwonlyargs
            and node.args.vararg is None
            and node.args.kwarg is None
        ]
        class_tests = [
            child
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name.startswith("test")
        ]
        if top_level_tests and not class_tests:
            function_name = top_level_tests[0].name
            class_name = (
                "TestBRT"
                + "".join(
                    part.capitalize()
                    for part in safe_id.split("_")
                    if part
                )
                + "Adapter"
            )
            code += (
                "\n\nimport unittest as _brt_unittest\n\n\n"
                f"class {class_name}(_brt_unittest.TestCase):\n"
                f"    def {function_name}(self):\n"
                f"        return {function_name}()\n"
            )
    return code


def _candidate_paths(instance_id: str, buggy_repo: str, host: HostContext) -> tuple[str, str]:
    safe_id = sanitize_instance_id(instance_id)
    host_dir = os.path.dirname(host.host_file) if host.host_file else "tests"
    if instance_id.startswith("sympy__sympy-") and not (
        host_dir == "sympy" or host_dir.startswith("sympy/")
    ):
        # SymPy's bin/test only discovers test_*.py below the sympy package.
        host_dir = "sympy/tests"
    rel = os.path.join(host_dir, f"test_brt_{safe_id}.py")
    return rel, str(Path(buggy_repo) / rel)


def write_candidate_to_repo(candidate: CandidateTest, buggy_repo: str) -> CandidateTest:
    ensure_dir(Path(candidate.candidate_file_path).parent)
    write_text(candidate.candidate_file_path, candidate.code)
    candidate.pytest_nodeid = candidate.candidate_repo_path
    candidate.command = f"python -m pytest {candidate.candidate_repo_path} -q"
    return candidate


def generate_candidate(
    instance_id: str,
    behavior: BehaviorEvidence,
    host: HostContext,
    related_test: RetrievedTest | None,
    related_source: list[RetrievedCode],
    llm_client: Any,
    output_dir: str,
    buggy_repo: str,
    round_id: int = 0,
    feedback: str = "",
    write_to_repo: bool = True,
    protocol: ProtocolRecovery | None = None,
    mutation_plan: MutationPlan | None = None,
    ablation_config: AblationConfig | None = None,
    issue_text: str = "",
) -> CandidateTest:
    ensure_dir(Path(output_dir) / "prompts")
    ensure_dir(Path(output_dir) / "responses")
    safe_id = sanitize_instance_id(instance_id)
    config = (ablation_config or AblationConfig()).validate()
    behavior_json = _prompt_json(
        behavior_prompt_payload(behavior, config), MAX_PROMPT_BEHAVIOR_CHARS
    )
    source_context = _prompt_text(
        format_effective_source_context(
            behavior, related_source, buggy_repo
        ),
        MAX_PROMPT_SOURCE_CHARS,
    )
    host_context_json = _prompt_json(host.to_dict(), MAX_PROMPT_HOST_CHARS)
    seed_test_code = _prompt_text(
        related_test.code_content if related_test else host.seed_test_code,
        MAX_PROMPT_SEED_CHARS,
    )
    user_prompt = MUTATION_GENERATION_USER_PROMPT.format(
        instance_id=instance_id,
        safe_instance_id=safe_id,
        insert_strategy=host.insert_strategy,
        behavior_json=behavior_json,
        host_context_json=host_context_json,
        code_context=source_context,
        seed_test_code=seed_test_code,
        feedback=_prompt_text(feedback or "无", MAX_PROMPT_FEEDBACK_CHARS),
    )
    system_prompt = MUTATION_GENERATION_SYSTEM_PROMPT
    user_prompt = render_evidence_prompt(user_prompt, behavior)
    if protocol is not None:
        user_prompt += (
            "\n\n【必须保留的测试协议】\n"
            + _prompt_json(protocol.to_dict(), MAX_PROMPT_PROTOCOL_CHARS)
        )
    direct_user_prompt = user_prompt
    effective_plan = (
        mutation_plan if mutation_plan is not None and mutation_plan.is_usable else None
    )
    if effective_plan is not None:
        user_prompt += (
            "\n\n【已通过结构与证据校验的 Trigger Mutation Plan】\n"
            + json.dumps(effective_plan.to_dict(), ensure_ascii=False)
            + "\n只在 Trigger 部分执行这些有证据的小修改，不得自由扩大场景或重写无关 setup。"
            + "Oracle 必须独立依据 BehaviorTarget.expected_behavior 构造；不得把计划中的"
            + " buggy observation、异常或示例文本直接当作 expected value。"
        )
    user_prompt = render_ablation_prompt(user_prompt, config)
    if config.mutation:
        direct_user_prompt = render_ablation_prompt(
            direct_user_prompt
            + "\n\n显式 Trigger Plan 未能产生可验证候选时，直接依据 Issue、"
            "BehaviorTarget、源码、ProtocolRecovery 与 seed 构造一个最小 BRT；"
            "不要猜测计划内容，也不要扩大场景。",
            config,
        )
    system_prompt = render_ablation_prompt(
        system_prompt, config, include_banner=False
    )
    prompt_path = str(Path(output_dir) / "prompts" / f"generation_round_{round_id}.txt")
    response_path = str(Path(output_dir) / "responses" / f"generation_round_{round_id}.txt")
    write_text(prompt_path, system_prompt + "\n\n" + user_prompt)
    response = llm_client.chat(system_prompt, user_prompt)
    write_text(response_path, response)
    code = _wrap_if_needed(response, host, safe_id)
    for validation_attempt in range(2):
        semantic_problem = _semantic_path_problem(
            behavior, source_context, code, issue_text=issue_text
        )
        if not semantic_problem:
            break
        retry_prompt = (
            user_prompt
            + "\n\n初始候选执行前语义校验失败："
            + semantic_problem
            + "\n当前无效候选如下：\n"
            + code
            + "\n请返回修复后的完整 Python 文件。"
        )
        retry_response_path = str(
            Path(output_dir)
            / "responses"
            / (
                f"generation_round_{round_id}_semantic_validation_retry_"
                f"{validation_attempt + 1}.txt"
            )
        )
        response = llm_client.chat(system_prompt, retry_prompt)
        write_text(retry_response_path, response)
        code = _wrap_if_needed(response, host, safe_id)
    rel_path, full_path = _candidate_paths(instance_id, buggy_repo, host)
    candidate = CandidateTest(
        instance_id=instance_id,
        round_id=round_id,
        code=code,
        candidate_file_path=full_path,
        candidate_repo_path=rel_path,
        prompt_path=prompt_path,
        response_path=response_path,
        mutation_plan_status=effective_plan.status if effective_plan else "",
        mutation_plan_risk=effective_plan.risk if effective_plan else "",
        oracle_contract_kinds=oracle_kinds(code),
    )
    candidate.mutation_adherence = assess_mutation_adherence(
        candidate.code, effective_plan, protocol
    )
    if effective_plan is not None:
        safe_json_dump(
            candidate.mutation_adherence,
            str(
                Path(output_dir)
                / f"mutation_round_{effective_plan.round_id}_adherence.json"
            ),
        )
        if candidate.mutation_adherence.get("status") == "VIOLATED":
            # A plan that was not actually followed must not be credited to
            # Mutation or ranked as a plan-derived candidate. Persist it for
            # audit, then explicitly generate a no-plan fallback from the same
            # evidence instead of forcing or silently relabeling it.
            nonadherent_path = str(
                Path(output_dir)
                / f"mutation_round_{effective_plan.round_id}_nonadherent.py"
            )
            write_text(nonadherent_path, candidate.code)
            fallback_prompt_path = str(
                Path(output_dir)
                / "prompts"
                / f"generation_round_{round_id}_plan_fallback.txt"
            )
            fallback_response_path = str(
                Path(output_dir)
                / "responses"
                / f"generation_round_{round_id}_plan_fallback.txt"
            )
            write_text(
                fallback_prompt_path,
                system_prompt + "\n\n" + direct_user_prompt,
            )
            fallback_response = llm_client.chat(
                system_prompt, direct_user_prompt
            )
            write_text(fallback_response_path, fallback_response)
            fallback_code = _wrap_if_needed(
                fallback_response, host, safe_id
            )
            for validation_attempt in range(2):
                semantic_problem = _semantic_path_problem(
                    behavior,
                    source_context,
                    fallback_code,
                    issue_text=issue_text,
                )
                if not semantic_problem:
                    break
                retry_prompt = (
                    direct_user_prompt
                    + "\n\n无计划降级候选执行前语义校验失败："
                    + semantic_problem
                    + "\n当前无效候选如下：\n"
                    + fallback_code
                    + "\n请返回修复后的完整 Python 文件。"
                )
                fallback_response = llm_client.chat(
                    system_prompt, retry_prompt
                )
                retry_path = str(
                    Path(output_dir)
                    / "responses"
                    / (
                        f"generation_round_{round_id}_plan_fallback_retry_"
                        f"{validation_attempt + 1}.txt"
                    )
                )
                write_text(retry_path, fallback_response)
                fallback_code = _wrap_if_needed(
                    fallback_response, host, safe_id
                )
            safe_json_dump(
                {
                    "status": "FALLBACK_DIRECT",
                    "reason": "validated plan candidate failed adherence",
                    "plan_status": effective_plan.status,
                    "plan_round_id": effective_plan.round_id,
                    "violations": candidate.mutation_adherence.get(
                        "violations", []
                    ),
                    "nonadherent_candidate": nonadherent_path,
                    "fallback_prompt": fallback_prompt_path,
                    "fallback_response": fallback_response_path,
                },
                str(
                    Path(output_dir)
                    / f"mutation_round_{effective_plan.round_id}_fallback.json"
                ),
            )
            candidate.code = fallback_code
            candidate.prompt_path = fallback_prompt_path
            candidate.response_path = fallback_response_path
            candidate.mutation_plan_status = "FALLBACK_DIRECT"
            candidate.mutation_plan_risk = ""
            candidate.mutation_adherence = assess_mutation_adherence(
                fallback_code, None, protocol
            )
            candidate.oracle_contract_kinds = oracle_kinds(fallback_code)
    if write_to_repo:
        write_candidate_to_repo(candidate, buggy_repo)
    else:
        candidate.pytest_nodeid = candidate.candidate_repo_path
        candidate.command = f"python -m pytest {candidate.candidate_repo_path} -q"
    write_text(str(Path(output_dir) / f"candidate_round_{round_id}.py"), code)
    return candidate


def repair_candidate(
    instance_id: str,
    behavior: BehaviorEvidence,
    host: HostContext,
    candidate: CandidateTest,
    execution: ExecutionResult,
    llm_client: Any,
    output_dir: str,
    round_id: int,
    focus: str,
    related_source: list[RetrievedCode] | None = None,
    observation_json: str = "{}",
    verifier_feedback: dict[str, Any] | None = None,
    buggy_repo: str = "",
    protocol: ProtocolRecovery | None = None,
    mutation_plan: MutationPlan | None = None,
    ablation_config: AblationConfig | None = None,
    issue_text: str = "",
) -> CandidateTest:
    config = (ablation_config or AblationConfig()).validate()
    feedback_json = _prompt_json(
        verifier_feedback or {}, MAX_PROMPT_FEEDBACK_CHARS
    )
    behavior_json = _prompt_json(
        behavior_prompt_payload(behavior, config), MAX_PROMPT_BEHAVIOR_CHARS
    )
    source_context = _prompt_text(
        format_effective_source_context(
            behavior, related_source or [], buggy_repo
        ),
        MAX_PROMPT_SOURCE_CHARS,
    )
    reference_seed_payload = _prompt_text(
        host.seed_test_code, MAX_PROMPT_SEED_CHARS
    )
    host_context_json = _prompt_json(host.to_dict(), MAX_PROMPT_HOST_CHARS)
    protocol_json = _prompt_json(
        protocol.to_dict() if protocol else {}, MAX_PROMPT_PROTOCOL_CHARS
    )
    candidate_code = _prompt_text(
        candidate.code, MAX_PROMPT_CANDIDATE_CHARS
    )
    execution_log = _prompt_text(
        execution.stdout + "\n" + execution.stderr,
        MAX_PROMPT_EXECUTION_CHARS,
    )
    if focus == "generic":
        system = REPAIR_GENERIC_SYSTEM_PROMPT
        template = REPAIR_GENERIC_USER_PROMPT
        kwargs = {
            "issue_text": _prompt_text(issue_text, MAX_PROMPT_ISSUE_CHARS),
            "behavior_json": behavior_json,
            "host_context_json": host_context_json,
            "protocol_json": protocol_json,
            "seed_test_code": reference_seed_payload,
            "code_context": source_context,
            "candidate_code": candidate_code,
            "command": execution.command or candidate.command,
            "execution_status": execution.status,
            "execution_log": execution_log,
            "verifier_feedback": feedback_json,
        }
    elif focus == "setup":
        system = REPAIR_SETUP_SYSTEM_PROMPT
        template = REPAIR_SETUP_USER_PROMPT
        kwargs = {
            "behavior_json": behavior_json,
            "host_context_json": host_context_json,
            "candidate_code": candidate_code,
            "execution_log": execution_log,
            "verifier_feedback": feedback_json,
        }
    elif focus == "oracle":
        system = REPAIR_ORACLE_SYSTEM_PROMPT
        template = REPAIR_ORACLE_USER_PROMPT
        kwargs = {
            "behavior_json": behavior_json,
            "candidate_code": candidate_code,
            "execution_log": execution_log,
            "observation_json": observation_json,
            "verifier_feedback": feedback_json,
        }
    else:
        system = REPAIR_TRIGGER_SYSTEM_PROMPT
        template = REPAIR_TRIGGER_USER_PROMPT
        kwargs = {
            "behavior_json": behavior_json,
            "host_context_json": host_context_json,
            "seed_test_code": reference_seed_payload,
            "code_context": source_context,
            "candidate_code": candidate_code,
            "execution_log": execution_log,
            "verifier_feedback": feedback_json,
        }
    user_prompt = render_evidence_prompt(template.format(**kwargs), behavior)
    if focus != "generic":
        # Specialized feedback is a focus constraint, not a reduced-evidence
        # branch. Every semantic repair receives the same complete evidence as
        # Generic Iteration so the comparison changes routing, not information.
        user_prompt += (
            "\n\n【所有反馈类型共享的完整证据】"
            "\n完整 Issue：" + _prompt_text(issue_text, MAX_PROMPT_ISSUE_CHARS)
            + "\nBehaviorTarget/Issue 证据：" + behavior_json
            + "\nHostContext：" + host_context_json
            + "\nProtocolRecovery：" + protocol_json
            + "\n参考测试证据：" + reference_seed_payload
            + "\n相关源码：" + source_context
            + "\n执行命令：" + (execution.command or candidate.command)
            + "\n执行分类：" + execution.status
            + "\nVerifier 反馈：" + feedback_json
        )
    if protocol is not None:
        user_prompt += "\n\nProtocolRecovery：" + protocol_json
    guidance_plan = (
        mutation_plan if mutation_plan is not None and mutation_plan.is_usable else None
    )
    lineage_plan = guidance_plan or mutation_plan_from_candidate(candidate)
    if guidance_plan is not None:
        user_prompt += (
            "\n\n本轮通过结构与证据校验的 Trigger Mutation Plan："
            + json.dumps(guidance_plan.to_dict(), ensure_ascii=False)
        )
        user_prompt += (
            "\n只执行 plan 中的 Trigger 修改；Issue 目标调用对应的 Oracle 必须保持逐 AST 等价。"
            "不能改写裸 assert、unittest assert*、异常/警告/日志上下文、snapshot/matcher、"
            "显式 failure guard、NO_EXCEPTION 语义或 expected value。"
            "仅当执行日志明确显示一个 baseline/对照 Oracle 在目标调用之前失败时，"
            "可以删除该阻塞性对照 Oracle；不得删除或改写目标 Oracle。"
        )
    user_prompt = render_ablation_prompt(user_prompt, config)
    system = render_ablation_prompt(system, config, include_banner=False)
    prompt_path = str(Path(output_dir) / "prompts" / f"repair_prompt_round_{round_id}.txt")
    response_path = str(Path(output_dir) / "responses" / f"repair_response_round_{round_id}.txt")
    write_text(prompt_path, system + "\n\n" + user_prompt)
    response = llm_client.chat(system, user_prompt)
    write_text(response_path, response)
    code = _wrap_if_needed(response, host, sanitize_instance_id(instance_id))
    if code.strip() == candidate.code.strip():
        retry_prompt = (
            user_prompt
            + "\n\n上一次返回与当前测试完全相同，属于无效修复。"
            + "这次必须根据 Verifier 反馈修改导致失败的具体代码位置；"
            + "如果无法修复，也不能原样返回。仍然只输出完整 Python 文件。"
        )
        retry_response_path = str(
            Path(output_dir)
            / "responses"
            / f"repair_response_round_{round_id}_unchanged_retry.txt"
        )
        response = llm_client.chat(system, retry_prompt)
        write_text(retry_response_path, response)
        code = _wrap_if_needed(response, host, sanitize_instance_id(instance_id))
    if focus == "setup":
        setup_log = execution.stdout + "\n" + execution.stderr
        for validation_attempt in range(2):
            setup_problem = _persistent_setup_failure(code, setup_log)
            if not setup_problem:
                break
            retry_prompt = (
                user_prompt
                + "\n\n返回代码执行前校验失败："
                + setup_problem
                + "\n当前仍然无效的返回代码如下：\n"
                + code
                + "\n必须返回已消除该失败模式的完整 Python 文件。"
            )
            retry_response_path = str(
                Path(output_dir)
                / "responses"
                / (
                    f"repair_response_round_{round_id}_setup_validation_retry_"
                    f"{validation_attempt + 1}.txt"
                )
            )
            response = llm_client.chat(system, retry_prompt)
            write_text(retry_response_path, response)
            code = _wrap_if_needed(
                response, host, sanitize_instance_id(instance_id)
            )
    for validation_attempt in range(2):
        semantic_problem = _semantic_path_problem(
            behavior,
            source_context,
            code,
            issue_text=issue_text,
            execution_log=execution.stdout + "\n" + execution.stderr,
        )
        if not semantic_problem:
            break
        retry_prompt = (
            user_prompt
            + "\n\n返回代码执行前的源码路径校验失败："
            + semantic_problem
            + "\n当前仍然无效的返回代码如下：\n"
            + code
            + "\n必须返回通过该路径校验的完整 Python 文件。"
        )
        retry_response_path = str(
            Path(output_dir)
            / "responses"
            / (
                f"repair_response_round_{round_id}_semantic_validation_retry_"
                f"{validation_attempt + 1}.txt"
            )
        )
        response = llm_client.chat(system, retry_prompt)
        write_text(retry_response_path, response)
        code = _wrap_if_needed(
            response, host, sanitize_instance_id(instance_id)
        )
    oracle_contract_preserved = True
    oracle_contract_violation = ""
    if focus in {"setup", "trigger"}:
        baseline_oracle = oracle_fingerprint(candidate.code)
        authorized_oracle_pruning = _allows_target_reachability_oracle_pruning(
            focus,
            verifier_feedback,
            candidate.code,
            code,
            behavior,
        )
        if (
            baseline_oracle != oracle_fingerprint(code)
            and not authorized_oracle_pruning
        ):
            retry_prompt = (
                user_prompt
                + "\n\n本轮不是 Oracle 修复，但返回代码改变了 Oracle 合约。"
                + "Oracle 包括裸 assert、unittest assert*、pytest.raises、"
                + "assertRaises、pytest.warns、assertWarns、assertLogs、日志/警告"
                + "上下文、pytest.fail、snapshot/matcher 和显式失败 guard。"
                + "必须恢复当前测试原有的全部 Oracle 语义，只修改本轮 focus。"
                + "\n原 Oracle 类型："
                + json.dumps(oracle_kinds(candidate.code), ensure_ascii=False)
                + "\n当前无效返回：\n"
                + code
            )
            retry_response = llm_client.chat(system, retry_prompt)
            retry_path = str(
                Path(output_dir)
                / "responses"
                / f"repair_response_round_{round_id}_oracle_contract_retry.txt"
            )
            write_text(retry_path, retry_response)
            code = _wrap_if_needed(
                retry_response, host, sanitize_instance_id(instance_id)
            )
            authorized_oracle_pruning = (
                _allows_target_reachability_oracle_pruning(
                    focus,
                    verifier_feedback,
                    candidate.code,
                    code,
                    behavior,
                )
            )
        oracle_contract_preserved = bool(
            baseline_oracle == oracle_fingerprint(code)
            or authorized_oracle_pruning
        )
        if authorized_oracle_pruning:
            safe_json_dump(
                {
                    "status": "AUTHORIZED_BLOCKING_CONTROL_ORACLE_PRUNING",
                    "focus": focus,
                    "failure_class": str(
                        (verifier_feedback or {}).get("failure_class") or ""
                    ),
                    "oracle_before": oracle_kinds(candidate.code),
                    "oracle_after": oracle_kinds(code),
                    "reason": (
                        "target_not_hit repair removed only an AST-subset of "
                        "control Oracle clauses; the candidate remains "
                        "falsifiable and must pass execution and re-verification"
                    ),
                },
                str(
                    Path(output_dir)
                    / f"repair_round_{round_id}_{focus}_oracle_pruned.json"
                ),
            )
        if not oracle_contract_preserved:
            rejected_code_path = str(
                Path(output_dir)
                / f"candidate_round_{round_id}_{focus}_oracle_violation.py"
            )
            write_text(rejected_code_path, code)
            safe_json_dump(
                {
                    "status": "REJECTED_ORACLE_CONTRACT_CHANGE",
                    "focus": focus,
                    "oracle_before": oracle_kinds(candidate.code),
                    "oracle_after": oracle_kinds(code),
                    "rejected_code_path": rejected_code_path,
                },
                str(
                    Path(output_dir)
                    / f"repair_round_{round_id}_{focus}_rejected.json"
                ),
            )
            # Do not rank a setup/trigger repair that still changes the Oracle
            # after the preservation retry. Continue from the last valid
            # checkpoint; a subsequent verifier decision may route a genuine
            # observation problem to Oracle feedback.
            code = candidate.code
            oracle_contract_preserved = True
            oracle_contract_violation = ""
    new_candidate = CandidateTest(
        instance_id=instance_id,
        round_id=round_id,
        code=code,
        candidate_file_path=candidate.candidate_file_path,
        candidate_repo_path=candidate.candidate_repo_path,
        prompt_path=prompt_path,
        response_path=response_path,
        mutation_plan_status=lineage_plan.status if lineage_plan else "",
        mutation_plan_risk=lineage_plan.risk if lineage_plan else "",
        oracle_contract_kinds=oracle_kinds(code),
        oracle_contract_preserved=oracle_contract_preserved,
        oracle_contract_violation=oracle_contract_violation,
    )
    new_candidate.mutation_adherence = assess_mutation_adherence(
        code,
        lineage_plan,
        protocol,
        oracle_baseline=(
            candidate.code
            if (
                focus in {"setup", "trigger"}
                and lineage_plan is not None
                and not authorized_oracle_pruning
            )
            else ""
        ),
    )
    if lineage_plan is not None:
        safe_json_dump(
            new_candidate.mutation_adherence,
            str(
                Path(output_dir)
                / f"mutation_round_{round_id}_adherence.json"
            ),
        )
    write_text(new_candidate.candidate_file_path, code)
    new_candidate.pytest_nodeid = new_candidate.candidate_repo_path
    new_candidate.command = candidate.command
    write_text(str(Path(output_dir) / f"candidate_round_{round_id}.py"), code)
    return new_candidate

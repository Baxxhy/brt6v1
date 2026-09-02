"""Static, repository-agnostic semantic checks for generated BRT files."""

from __future__ import annotations

import ast
import builtins
import re

from ..core.behavior_evidence import BehaviorEvidence, expected_behavior_text
from .mutation_adherence import oracle_kinds


_ORACLE_LITERAL_CALLS = {
    "raises",
    "raisesregex",
    "warns",
    "warnsregex",
    "assertwarns",
    "assertwarnsregex",
    "assertlogs",
    "assertnlogs",
    "fnmatch_lines",
    "match_lines",
    "re_match_lines",
    "snapshot",
    "match",
}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _same(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(
        right, include_attributes=False
    )


def _opposites(left: ast.AST, right: ast.AST) -> bool:
    if isinstance(left, ast.UnaryOp) and isinstance(left.op, ast.Not):
        return _same(left.operand, right)
    if isinstance(right, ast.UnaryOp) and isinstance(right.op, ast.Not):
        return _same(right.operand, left)
    if not isinstance(left, ast.Compare) or not isinstance(right, ast.Compare):
        return False
    if len(left.ops) != 1 or len(right.ops) != 1:
        return False
    if not (_same(left.left, right.left) and _same(left.comparators[0], right.comparators[0])):
        return False
    pairs = (
        (ast.Eq, ast.NotEq),
        (ast.Is, ast.IsNot),
        (ast.In, ast.NotIn),
        (ast.Lt, ast.GtE),
        (ast.LtE, ast.Gt),
    )
    return any(
        (isinstance(left.ops[0], a) and isinstance(right.ops[0], b))
        or (isinstance(left.ops[0], b) and isinstance(right.ops[0], a))
        for a, b in pairs
    )


def _expected_text(behavior: BehaviorEvidence) -> str:
    return expected_behavior_text(behavior).lower()


def _oracle_string_literals(tree: ast.AST) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(tree):
        roots: list[ast.AST] = []
        if isinstance(node, ast.Assert):
            roots.append(node.test)
        elif isinstance(node, ast.Call):
            call_name = _name(node.func).lower()
            leaf = call_name.rsplit(".", 1)[-1]
            if (
                leaf.startswith("assert")
                or leaf in _ORACLE_LITERAL_CALLS
            ):
                roots.extend(node.args)
        for root in roots:
            for child in ast.walk(root):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    values.add(child.value.lower())
    return values


def _assigned_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _unresolved_class_scope_name(tree: ast.Module) -> str:
    module_names = set(dir(builtins)) | {
        "__file__",
        "__name__",
        "__package__",
    }
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            module_names.update(_assigned_names(node))

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        class_names: set[str] = set()
        expressions: list[ast.AST] = [*node.bases, *node.decorator_list]
        for keyword in node.keywords:
            expressions.append(keyword.value)
        for child in node.body:
            value: ast.AST | None = None
            if isinstance(child, ast.Assign):
                value = child.value
            elif isinstance(child, ast.AnnAssign):
                value = child.value
            elif isinstance(child, ast.AugAssign):
                value = child.value
            if value is not None:
                expressions.append(value)
                locally_bound = _assigned_names(value)
                missing = sorted(
                    {
                        item.id
                        for item in ast.walk(value)
                        if isinstance(item, ast.Name)
                        and isinstance(item.ctx, ast.Load)
                        and item.id not in locally_bound
                    }
                    - module_names
                    - class_names
                )
                if missing:
                    return missing[0]
            class_names.update(_assigned_names(child))
        for expression in expressions[: len(node.bases) + len(node.decorator_list) + len(node.keywords)]:
            missing = sorted(
                {
                    item.id
                    for item in ast.walk(expression)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
                }
                - module_names
            )
            if missing:
                return missing[0]
    return ""


def _required_message_tokens(behavior: BehaviorEvidence) -> set[str]:
    hints = getattr(behavior, "assertion_hints", []) or []
    required: set[str] = set()
    requirement_markers = (
        "包含", "提及", "显示", "include", "contain", "mention", "display",
    )
    for hint in hints:
        if not isinstance(hint, dict):
            continue
        goal = str(hint.get("assertion_goal") or "")
        if not any(marker in goal.lower() for marker in requirement_markers):
            continue
        required.update(
            token.lower()
            for token in re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", goal)
            if "_" in token and len(token) >= 5
        )
    return required


def oracle_contract_summary(
    behavior: BehaviorEvidence,
    code: str,
) -> dict[str, object]:
    """Describe whether a candidate has a falsifiable public observation.

    A valid Oracle need not contain a bare ``assert``. Framework exception,
    warning, logging, matcher, and explicit-failure protocols are explicit
    Oracles. A direct call is also sufficient for an Issue whose expected
    behavior is specifically no-exception/no-crash.
    """

    kinds = oracle_kinds(code)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "kinds": kinds,
            "falsifiable": False,
            "no_exception_contract": False,
            "reason": "candidate is not parseable",
        }
    explicit = False
    target_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            explicit = True
        elif isinstance(node, ast.Call):
            leaf = _name(node.func).rsplit(".", 1)[-1].lower()
            if leaf.startswith("assert") or leaf in _ORACLE_LITERAL_CALLS or leaf == "fail":
                explicit = True
            elif leaf not in {
                "fixture",
                "mark",
                "parametrize",
                "patch",
                "mock",
            }:
                target_calls += 1
    if "EXPLICIT_FAILURE_GUARD" in kinds:
        explicit = True
    expected = _expected_text(behavior)
    no_exception = any(
        marker in expected
        for marker in (
            "不应抛",
            "不再抛",
            "不应该抛",
            "不报错",
            "正常执行",
            "正常工作",
            "不崩溃",
            "should not raise",
            "without raising",
            "without error",
            "must not raise",
            "should not crash",
            "without crashing",
        )
    ) and target_calls > 0
    falsifiable = explicit or no_exception
    reason = (
        "explicit framework/public observation"
        if explicit
        else "expected behavior is falsified by an unexpected exception"
        if no_exception
        else "no falsifiable Oracle protocol was found"
    )
    if no_exception and "NO_EXCEPTION" not in kinds:
        kinds = sorted(set(kinds) | {"NO_EXCEPTION"})
    return {
        "kinds": kinds,
        "falsifiable": falsifiable,
        "no_exception_contract": no_exception,
        "reason": reason,
    }


def audit_candidate(
    behavior: BehaviorEvidence,
    code: str,
) -> str:
    """Return a repair instruction when the candidate is semantically unsafe."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "候选文件不是合法 Python，必须先修复语法。"

    test_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]
    if len(test_nodes) != 1:
        return (
            f"完整 BRT 文件必须只有一个可收集测试入口，当前有 {len(test_nodes)} 个。"
            "保留最直接复现 Issue 的一个测试，删除 baseline、对照组和备用测试。"
        )

    if "NO_TESTS_COLLECTED" in code:
        return (
            "不得把 ExitCode.NO_TESTS_COLLECTED 当作成功；"
            "候选必须证明至少一个目标测试真实执行。"
        )

    unresolved_class_name = _unresolved_class_scope_name(tree)
    if unresolved_class_name:
        return (
            "类定义阶段引用了未恢复的模块级名称 "
            f"{unresolved_class_name!r}；请从 ProtocolRecovery.module_context "
            "恢复对应赋值和必要 setup 调用，或移除该类级依赖。"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            return (
                "生成测试必须兼容基准实例可能使用的 Python 3.5；"
                "不得使用 f-string，请改用 str.format() 或普通字符串拼接。"
            )
        if isinstance(node, ast.Call):
            call_name = _name(node.func)
            if call_name.endswith(".makepyfile") and node.args:
                return (
                    "pytester.makepyfile() 使用位置参数时会按当前外层测试函数命名内层"
                    "模块；生成的 BRT 外层文件采用同名规则，会触发 ImportPathMismatchError。"
                    "必须改用唯一的关键字文件名，例如 "
                    "pytester.makepyfile(test_brt_inner_case=content)，并让 runpytest() "
                    "显式运行该唯一文件。"
                )
            call_leaf = call_name.rsplit(".", 1)[-1].lower()
            if call_leaf in {"importorskip", "symlink_or_skip"}:
                return (
                    f"BRT 不得调用 {call_leaf}：它可能把未执行目标路径的候选标成成功。"
                    "请直接构造本地、确定性且可执行的输入，并用公开行为验证修复。"
                )
            if call_name in {
                "pytest.skip",
                "unittest.skip",
            } or call_name.endswith(".skip"):
                return (
                    "BRT 不得无条件调用 skip。缺少平台或依赖时应恢复真实"
                    "可执行上下文，不能把未执行的测试当作通过。"
                )
            if call_name in {"pytest.raises", "raises"} and node.args:
                if _name(node.args[0]) in {"Exception", "BaseException"}:
                    return "不得用宽泛 Exception/BaseException 作为 oracle；必须验证 Issue 指定的稳定行为。"
        if isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            for decorator in node.decorator_list:
                name = _name(decorator.func) if isinstance(decorator, ast.Call) else _name(decorator)
                leaf = name.rsplit(".", 1)[-1].lower()
                if (
                    leaf in {"skip", "skipif", "skipunless", "network"}
                    or leaf.startswith("requires_")
                ):
                    return (
                        "BRT 不得使用会跳过完整测试的条件/decorator；"
                        "测试必须在基准环境中真实执行目标路径。"
                    )
                if leaf == "image_comparison":
                    return (
                        "生成测试没有随仓库提交 expected baseline 图片，不能使用 "
                        "image_comparison。请改用数值、对象状态、路径或稳定文本片段"
                        "验证 Issue 的公开行为。"
                    )
                if isinstance(decorator, ast.Call) and any(
                    keyword.arg == "skip_on_importerror"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in decorator.keywords
                ):
                    return (
                        "BRT 不得设置 skip_on_importerror=True；"
                        "请选择基准环境可执行的 backend 或无 GUI 的公开状态验证。"
                    )
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                return "不得使用 assert True 或其他占位 oracle。"
            if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or):
                values = node.test.values
                if any(_opposites(a, b) for i, a in enumerate(values) for b in values[i + 1 :]):
                    return "检测到恒真的 A or not A / 相反比较断言；必须改为 expected_behavior 的可证伪 oracle。"
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                broad = handler.type is None or _name(handler.type) in {"Exception", "BaseException"}
                swallowed = all(isinstance(item, (ast.Pass, ast.Return, ast.Continue)) for item in handler.body)
                if broad and swallowed:
                    return "不得用 broad try/except 吞掉目标路径异常；只捕获 Issue 明确要求观察的异常。"

    expected = _expected_text(behavior)
    no_raise = any(
        marker in expected
        for marker in (
            "不应抛", "不再抛", "不应该抛", "不报错", "正常执行", "正常工作",
            "should not raise", "without raising", "without error", "must not raise",
        )
    )
    if no_raise and re.search(r"(?:pytest\.raises|assertRaises|\braises\s*\()", code):
        return (
            "expected_behavior 要求正常执行/不再抛异常，但候选用 raises 接受了 buggy 异常。"
            "应直接调用目标路径，并对修复后返回值、状态或输出建立正向断言。"
        )

    positive_capability = any(
        marker in expected
        for marker in (
            "应该支持", "应支持", "应该提供", "应提供", "应该存在", "应存在",
            "应该包含", "应包含", "应该显示", "应显示", "应该保留", "应保留",
            "should support", "should provide", "should exist", "should include",
            "should contain", "should display", "should preserve",
        )
    )
    negative_hasattr = re.search(
        r"(?:assert\s+not\s+hasattr\s*\(|assertFalse\s*\(\s*hasattr\s*\()",
        code,
    )
    if positive_capability and negative_hasattr:
        return (
            "expected_behavior 要求能力/属性存在，但候选断言 hasattr 为 False，"
            "这是把 buggy 的缺失行为当成正确结果。必须改成正向存在性和语义断言。"
        )
    required_message_tokens = _required_message_tokens(behavior)
    oracle_literals = _oracle_string_literals(tree)
    if required_message_tokens and not any(
        token in literal
        for token in required_message_tokens
        for literal in oracle_literals
    ):
        tokens = ", ".join(sorted(required_message_tokens))
        return (
            "BehaviorTarget 的 assertion_hints 明确要求修复后的消息包含/提及标识符 "
            f"{tokens}，但候选 oracle 没有断言这些新证据。不得正向匹配 buggy 的旧"
            " error_symptom；应让 buggy 因缺少新标识符而失败、fixed 因包含它而通过。"
        )
    return ""

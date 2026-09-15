"""Static, repository-agnostic semantic checks for generated BRT files."""

from __future__ import annotations

import ast
import builtins
import re

from ..core.behavior_evidence import BehaviorEvidence, expected_behavior_text
from .oracle_contract import oracle_kinds


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


def _unsupported_long_oracle_literal(
    behavior: BehaviorEvidence, issue_text: str, tree: ast.Module
) -> str:
    """Find an exact rendered string that is not promised by the Issue."""

    promised = (issue_text + "\n" + _expected_text(behavior)).lower()
    for literal in sorted(_oracle_string_literals(tree), key=len, reverse=True):
        compact = " ".join(literal.split())
        if len(compact) >= 60 and compact not in promised:
            return compact
    return ""


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


def _issue_import_aliases(issue_text: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for module, imported in re.findall(
        r"(?m)^\s*from\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+import\s+([^\n]+)",
        issue_text,
    ):
        for item in imported.strip(" ()").split(","):
            parts = re.split(r"\s+as\s+", item.strip())
            name = parts[0].strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                continue
            alias = parts[1].strip() if len(parts) == 2 else name
            aliases[alias] = f"{module}.{name}"
    for module, alias in re.findall(
        r"(?m)^\s*import\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
        r"(?:\s+as\s+([A-Za-z_]\w*))?\s*$",
        issue_text,
    ):
        aliases[alias or module.split(".", 1)[0]] = module
    return aliases


def _tree_import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
    return aliases


def _resolve_imported_reference(node: ast.AST, aliases: dict[str, str]) -> str:
    reference = _name(node)
    if not reference:
        return ""
    root, _, remainder = reference.partition(".")
    origin = aliases.get(root)
    if not origin:
        return ""
    return f"{origin}.{remainder}" if remainder else origin


def _namespace_mismatch(issue_text: str, tree: ast.Module) -> tuple[str, str] | None:
    issue_aliases = _issue_import_aliases(issue_text)
    expected: set[str] = set()
    for alias, class_name in re.findall(
        r"\b([A-Za-z_]\w*)\.([A-Z][A-Za-z0-9_]*)\b", issue_text
    ):
        if alias in issue_aliases:
            expected.add(f"{issue_aliases[alias]}.{class_name}")
    for alias, origin in issue_aliases.items():
        if origin.rsplit(".", 1)[-1][:1].isupper() and re.search(
            rf"\b{re.escape(alias)}\s*\(", issue_text
        ):
            expected.add(origin)
    if not expected:
        return None

    candidate_aliases = _tree_import_aliases(tree)
    used = {
        resolved
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for resolved in [_resolve_imported_reference(node.func, candidate_aliases)]
        if resolved
    }
    for expected_reference in sorted(expected):
        leaf = expected_reference.rsplit(".", 1)[-1]
        for actual_reference in sorted(used):
            if (
                actual_reference.rsplit(".", 1)[-1] == leaf
                and actual_reference != expected_reference
            ):
                return expected_reference, actual_reference
    return None


def _explicit_default_override(issue_text: str, tree: ast.Module) -> str:
    lowered = issue_text.lower()
    default_contract = any(
        marker in lowered
        for marker in (
            "set default",
            "by default",
            "not explicitly configured",
            "absence of explicitly configured",
            "未显式配置",
            "默认值",
            "默认配置",
        )
    )
    if not default_contract:
        return ""
    settings = set(re.findall(r"\b[A-Z][A-Z0-9_]{4,}\b", issue_text))
    if not settings:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in settings:
            return node.arg
        targets: list[ast.AST] = []
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(node, ast.Assign):
                targets.extend(node.targets)
            else:
                targets.append(node.target)
        for target in targets:
            if _name(target).rsplit(".", 1)[-1] in settings:
                return _name(target).rsplit(".", 1)[-1]
    return ""


def _leading_type_precondition(
    behavior: BehaviorEvidence, test_node: ast.AST
) -> str:
    expected = _expected_text(behavior)
    if any(
        marker in expected
        for marker in (
            "类型",
            "实例",
            " type",
            "instance of",
            " class",
        )
    ):
        return ""
    assertions: list[tuple[int, str]] = []
    for node in ast.walk(test_node):
        if isinstance(node, ast.Assert):
            kind = "isinstance" if (
                isinstance(node.test, ast.Call)
                and _name(node.test.func).rsplit(".", 1)[-1] == "isinstance"
            ) else "assert"
            assertions.append((getattr(node, "lineno", 0), kind))
        elif isinstance(node, ast.Call):
            leaf = _name(node.func).rsplit(".", 1)[-1]
            if leaf.startswith("assert"):
                kind = "isinstance" if leaf.lower() == "assertisinstance" else "assert"
                assertions.append((getattr(node, "lineno", 0), kind))
    assertions.sort()
    if len(assertions) > 1 and assertions[0][1] == "isinstance":
        return "isinstance"
    return ""


def _unsupported_rendered_literal(issue_text: str, tree: ast.Module) -> str:
    """Reject display text guessed only from an API attribute identifier."""

    literals: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assert)
            and isinstance(node.test, ast.Compare)
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.In)
            and isinstance(node.test.left, ast.Constant)
            and isinstance(node.test.left.value, str)
        ):
            literals.add(node.test.left.value)
        elif isinstance(node, ast.Call) and _name(node.func).lower().endswith(
            "assertin"
        ):
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                literals.add(node.args[0].value)

    for literal in sorted(literals):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,}", literal):
            continue
        matches = list(
            re.finditer(rf"(?i)(?<![A-Za-z0-9_]){re.escape(literal)}(?![A-Za-z0-9_])", issue_text)
        )
        if matches and all(match.start() > 0 and issue_text[match.start() - 1] == "." for match in matches):
            return literal
    return ""


_SCHEMA_KEYWORDS = {
    "columns",
    "dtype",
    "fieldnames",
    "headers",
    "names",
    "schema",
    "usecols",
}


def _unstated_reproducer_schema_keyword(
    behavior: BehaviorEvidence,
    issue_text: str,
    tree: ast.Module,
) -> str:
    expected = _expected_text(behavior)
    no_crash = any(
        marker in expected or marker in issue_text.lower()
        for marker in (
            "rather than crashing",
            "without crashing",
            "should not crash",
            "without raising",
            "不崩溃",
            "不报错",
        )
    )
    if not no_crash:
        return ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _name(node.func)
        if not call_name or not re.search(
            rf"\b{re.escape(call_name)}\s*\(", issue_text
        ):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg in _SCHEMA_KEYWORDS
                and not re.search(
                    rf"\b{re.escape(keyword.arg)}\s*=", issue_text
                )
            ):
                return keyword.arg
    return ""


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
    *,
    issue_text: str = "",
    execution_log: str = "",
) -> str:
    """Return a repair instruction when the candidate is semantically unsafe."""
    del execution_log
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "The candidate is not valid Python; repair its syntax first."

    test_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]
    if len(test_nodes) != 1:
        return (
            f"A complete BRT file must contain exactly one collectable test entry; found {len(test_nodes)}. "
            "Keep the single test that most directly reproduces the issue and remove baselines, controls, and backup tests."
        )

    mismatch = _namespace_mismatch(issue_text, tree) if issue_text else None
    if mismatch:
        expected_reference, actual_reference = mismatch
        return (
            "The candidate uses an API with the same name as the issue but from a different namespace: "
            f"it currently uses {actual_reference}, while the issue explicitly calls {expected_reference}. "
            "Use the import and public API from the issue, then verify that API's target behavior."
        )

    overridden_setting = (
        _explicit_default_override(issue_text, tree) if issue_text else ""
    )
    if overridden_setting:
        return (
            f"The issue concerns default behavior when {overridden_setting} is not explicitly configured. "
            "Remove override_settings, settings assignments, or same-named keyword overrides so the test observes the real default."
        )

    if _leading_type_precondition(behavior, test_nodes[0]):
        return (
            "The leading type assertion fails before the target behavior is executed or observed, "
            "and expected_behavior does not require that return type. Remove this precondition, "
            "construct the issue trigger directly, and let the final oracle verify only public post-fix behavior."
        )

    rendered_literal = (
        _unsupported_rendered_literal(issue_text, tree) if issue_text else ""
    )
    if rendered_literal:
        return (
            f"The candidate guesses that API attribute name {rendered_literal!r} is rendered text, "
            "but the issue uses it only for attribute access and does not provide that rendered value. "
            "Use an exact output example from the issue or assert a more stable public structure; "
            "do not infer formatted text from an identifier."
        )

    schema_keyword = (
        _unstated_reproducer_schema_keyword(behavior, issue_text, tree)
        if issue_text
        else ""
    )
    if schema_keyword:
        return (
            f"The issue provides a directly executable minimal reproducer, but the candidate additionally passes {schema_keyword}=, "
            "which changes the input schema and may make a fixed version fail for another precondition. "
            "Remove the extra argument, preserve the issue input and target call exactly, and observe only the promised fixed behavior."
        )

    unsupported_literal = _unsupported_long_oracle_literal(
        behavior, issue_text, tree
    )
    if unsupported_literal:
        return (
            "The candidate exactly asserts long formatted text not promised by the issue or expected_behavior: "
            + repr(unsupported_literal[:120])
            + ". Keep only stable fragments or public structure explicitly required by the issue, avoiding failures caused by unrelated formatting."
        )

    if "NO_TESTS_COLLECTED" in code:
        return (
            "Do not treat ExitCode.NO_TESTS_COLLECTED as success; "
            "the candidate must demonstrate that at least one target test actually ran."
        )

    unresolved_class_name = _unresolved_class_scope_name(tree)
    if unresolved_class_name:
        return (
            "The class definition references an unrecovered module-level name "
            f"{unresolved_class_name!r}. Restore its assignment and required setup calls from "
            "ProtocolRecovery.module_context, or remove that class-level dependency."
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            return (
                "The generated test must remain compatible with benchmark instances that may use Python 3.5. "
                "Do not use f-strings; use str.format() or ordinary string concatenation."
            )
        if isinstance(node, ast.Call):
            call_name = _name(node.func)
            if call_name.endswith(".makepyfile") and node.args:
                return (
                    "When pytester.makepyfile() uses a positional argument, it names the inner module after "
                    "the current outer test function. The generated BRT follows the same outer naming rule, "
                    "which causes ImportPathMismatchError. Use a unique keyword filename such as "
                    "pytester.makepyfile(test_brt_inner_case=content), and have runpytest() explicitly run that file."
                )
            call_leaf = call_name.rsplit(".", 1)[-1].lower()
            if call_leaf in {"importorskip", "symlink_or_skip"}:
                return (
                    f"A BRT must not call {call_leaf}; it may mark a candidate successful without executing the target path. "
                    "Construct local, deterministic, executable input directly and verify the fix through public behavior."
                )
            if call_name in {
                "pytest.skip",
                "unittest.skip",
            } or call_name.endswith(".skip"):
                return (
                    "A BRT must not call skip unconditionally. Restore a genuinely executable context when a platform or dependency is missing; "
                    "an unexecuted test must not count as passing."
                )
            if call_name in {"pytest.raises", "raises"} and node.args:
                if _name(node.args[0]) in {"Exception", "BaseException"}:
                    return "Do not use broad Exception/BaseException as the oracle; verify the stable behavior specified by the issue."
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
                        "A BRT must not use a condition or decorator that skips the complete test; "
                        "the test must execute the target path in the benchmark environment."
                    )
                if leaf == "image_comparison":
                    return (
                        "The generated test does not ship an expected baseline image with the repository, so it cannot use "
                        "image_comparison. Verify public issue behavior with numeric values, object state, paths, or stable text fragments."
                    )
                if isinstance(decorator, ast.Call) and any(
                    keyword.arg == "skip_on_importerror"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in decorator.keywords
                ):
                    return (
                        "A BRT must not set skip_on_importerror=True. "
                        "Choose a backend executable in the benchmark environment or verify public state without a GUI."
                    )
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                return "Do not use assert True or another placeholder oracle."
            if isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or):
                values = node.test.values
                if any(_opposites(a, b) for i, a in enumerate(values) for b in values[i + 1 :]):
                    return "Detected a tautological A or not A/opposite-comparison assertion; replace it with a falsifiable oracle for expected_behavior."
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                broad = handler.type is None or _name(handler.type) in {"Exception", "BaseException"}
                swallowed = all(isinstance(item, (ast.Pass, ast.Return, ast.Continue)) for item in handler.body)
                if broad and swallowed:
                    return "Do not swallow target-path exceptions with a broad try/except; catch only exceptions the issue explicitly requires observing."

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
            "expected_behavior requires successful execution without the exception, but the candidate uses raises to accept the buggy exception. "
            "Call the target path directly and make a positive assertion about the post-fix return value, state, or output."
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
            "expected_behavior requires the capability or attribute to exist, but the candidate asserts hasattr is False, "
            "treating the buggy absence as correct. Replace it with a positive existence and semantic assertion."
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
            "BehaviorTarget assertion_hints explicitly require the post-fix message to contain or mention identifiers "
            f"{tokens}, but the candidate oracle does not assert this new evidence. Do not positively match the old buggy "
            "error_symptom; the buggy version should fail because the new identifiers are absent, while the fixed version should pass because they are present."
        )
    return ""

"""Generate one candidate from the current test and one Semantic Delta."""

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
)
from ..core.ablation import (
    AblationConfig,
    behavior_prompt_payload,
    render_ablation_prompt,
)
from ..core.behavior_evidence import (
    BehaviorEvidence,
    render_evidence_prompt,
    suspected_bug_locations,
)
from ..core.schema import CandidateTest, HostContext, ProtocolRecovery, RetrievedCode, RetrievedTest, SemanticDelta
from ..validation.oracle_contract import oracle_kinds
from ..validation.delta_guard import assess_delta_application, check_candidate
from ..core.utils import (
    clean_code_block,
    ensure_dir,
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


def materialize_current_test(
    instance_id: str,
    host: HostContext,
    current_test_code: str,
    output_dir: str,
    buggy_repo: str,
    semantic_delta: SemanticDelta | None = None,
    delta_history: list[dict[str, Any]] | None = None,
    write_to_repo: bool = True,
) -> CandidateTest:
    """Place an unchanged seed/current test when the Planner returns KEEP."""

    safe_id = sanitize_instance_id(instance_id)
    code = _wrap_if_needed(current_test_code, host, safe_id)
    rel_path, full_path = _candidate_paths(instance_id, buggy_repo, host)
    candidate = CandidateTest(
        instance_id=instance_id,
        round_id=semantic_delta.round_id if semantic_delta else 0,
        code=code,
        candidate_file_path=full_path,
        candidate_repo_path=rel_path,
        status="UNCHANGED",
        notes="Planner returned KEEP or an invalid protocol response.",
        semantic_delta=semantic_delta.to_dict() if semantic_delta else {},
        delta_history=list(delta_history or [])
        + ([semantic_delta.to_dict()] if semantic_delta else []),
        delta_application=assess_delta_application(
            current_test_code, code, semantic_delta
        ),
        oracle_contract_kinds=oracle_kinds(code),
    )
    guard = check_candidate(code, rel_path)
    if not guard.ok:
        candidate.status = "STATIC_ERROR"
        candidate.notes = "\n".join(guard.errors)
    if write_to_repo:
        write_candidate_to_repo(candidate, buggy_repo)
    else:
        candidate.pytest_nodeid = candidate.candidate_repo_path
        candidate.command = f"python -m pytest {candidate.candidate_repo_path} -q"
    write_text(str(Path(output_dir) / f"candidate_round_{candidate.round_id}.py"), code)
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
    semantic_delta: SemanticDelta | None = None,
    current_test_code: str = "",
    delta_history: list[dict[str, Any]] | None = None,
    ablation_config: AblationConfig | None = None,
    issue_text: str = "",
) -> CandidateTest:
    ensure_dir(Path(output_dir) / "prompts")
    ensure_dir(Path(output_dir) / "responses")
    safe_id = sanitize_instance_id(instance_id)
    config = (ablation_config or AblationConfig()).validate()
    prompt_behavior = behavior_prompt_payload(behavior, config)
    if prompt_behavior.get("schema_version") == "raw_issue_context.v1":
        # This prompt has a dedicated lossless Issue field. Avoid giving the
        # ablation a second copy of the same text while preserving the full
        # RawIssueContext artifact for all other pipeline stages.
        prompt_behavior = dict(prompt_behavior)
        prompt_behavior.pop("issue_text", None)
        prompt_behavior["structured_target"] = "unavailable"
    behavior_json = _prompt_json(prompt_behavior, MAX_PROMPT_BEHAVIOR_CHARS)
    source_context = _prompt_text(
        format_effective_source_context(
            behavior, related_source, buggy_repo
        ),
        MAX_PROMPT_SOURCE_CHARS,
    )
    host_context_json = _prompt_json(host.to_dict(), MAX_PROMPT_HOST_CHARS)
    seed_test_code = _prompt_text(
        current_test_code
        or (related_test.code_content if related_test else host.seed_test_code),
        MAX_PROMPT_SEED_CHARS,
    )
    user_prompt = MUTATION_GENERATION_USER_PROMPT.format(
        instance_id=instance_id,
        safe_instance_id=safe_id,
        insert_strategy=host.insert_strategy,
        issue_text=_prompt_text(issue_text, MAX_PROMPT_ISSUE_CHARS),
        behavior_json=behavior_json,
        host_context_json=host_context_json,
        code_context=source_context,
        seed_test_code=seed_test_code,
        feedback=_prompt_text(feedback or "None", MAX_PROMPT_FEEDBACK_CHARS),
    )
    system_prompt = MUTATION_GENERATION_SYSTEM_PROMPT
    user_prompt = render_evidence_prompt(user_prompt, behavior)
    if protocol is not None:
        user_prompt += (
            "\n\nRequired test protocol to preserve:\n"
            + _prompt_json(protocol.to_dict(), MAX_PROMPT_PROTOCOL_CHARS)
        )
    effective_delta = (
        semantic_delta
        if semantic_delta is not None and semantic_delta.is_actionable
        else None
    )
    if effective_delta is not None:
        user_prompt += (
            "\n\nThe only Semantic Delta for this round:\n"
            + json.dumps(effective_delta.to_dict(), ensure_ascii=False)
            + "\nUse the current test as the only parent and apply only the stated change. "
            + "Preserve is the default; leave downstream effects for a later round."
        )
    if delta_history:
        user_prompt += "\n\nPrevious Semantic Delta trace:\n" + _prompt_json(
            delta_history, MAX_PROMPT_FEEDBACK_CHARS
        )
    user_prompt = render_ablation_prompt(user_prompt, config)
    system_prompt = render_ablation_prompt(
        system_prompt, config, include_banner=False
    )
    prompt_path = str(Path(output_dir) / "prompts" / f"generation_round_{round_id}.txt")
    response_path = str(Path(output_dir) / "responses" / f"generation_round_{round_id}.txt")
    write_text(prompt_path, system_prompt + "\n\n" + user_prompt)
    response = llm_client.chat(system_prompt, user_prompt)
    write_text(response_path, response)
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
        semantic_delta=semantic_delta.to_dict() if semantic_delta else {},
        delta_history=list(delta_history or [])
        + ([semantic_delta.to_dict()] if semantic_delta else []),
        delta_application=assess_delta_application(
            current_test_code or (
                related_test.code_content if related_test else host.seed_test_code
            ),
            code,
            semantic_delta,
        ),
        oracle_contract_kinds=oracle_kinds(code),
    )
    guard = check_candidate(code, rel_path)
    candidate.status = "CREATED" if guard.ok else "STATIC_ERROR"
    candidate.notes = "\n".join(guard.errors)
    if write_to_repo:
        write_candidate_to_repo(candidate, buggy_repo)
    else:
        candidate.pytest_nodeid = candidate.candidate_repo_path
        candidate.command = f"python -m pytest {candidate.candidate_repo_path} -q"
    write_text(str(Path(output_dir) / f"candidate_round_{round_id}.py"), candidate.code)
    return candidate

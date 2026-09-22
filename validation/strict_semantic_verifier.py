"""LLM semantic acceptance gate for tests executed on the buggy source."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from ..core.prompts import (
    STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT,
    STRICT_SEMANTIC_VERIFIER_USER_PROMPT,
)
from ..core.ablation import (
    AblationConfig,
    behavior_prompt_payload,
    render_ablation_prompt,
)
from ..core.behavior_evidence import (
    BehaviorEvidence,
    error_symptom_text,
    is_behavior_target,
    render_evidence_prompt,
    suspected_bug_locations,
    target_apis,
)
from ..core.schema import (
    CandidateTest,
    ExecutionResult,
    ProtocolRecovery,
    StrictVerifierResult,
    VerifierDecision,
)
from ..core.utils import extract_json_object, safe_json_dump, truncate_text, write_text
from .semantic_guard import oracle_contract_summary
from .candidate_defect import DEFECT_PROMPT, validated_candidate_defect


_POST_FIX_RISKS = {"low", "medium", "high", "unknown"}
_EXCEPTION_NAME = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning)\b"
)
_PYTEST_CANDIDATE_FRAME = re.compile(r"(?m)^([^\n]*?\.py):(\d+): in\s+")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _evidence_quote_forms(quote: str) -> set[str]:
    """Allow quotation delimiters without changing the quoted proposition.

    Preserve the original form (which can itself quote a code literal). Strip
    only balanced outer delimiters, never internal punctuation, negation,
    ellipses, or arbitrary mismatching prefixes/suffixes.
    """
    text = quote.strip()
    forms = {_normalized_text(text), _normalized_text(text.strip("`"))}
    pairs = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’'}
    # At most an outer quotation pair and an inner Markdown code pair.
    for _ in range(2):
        if len(text) < 2 or pairs.get(text[0]) != text[-1]:
            break
        text = text[1:-1].strip()
        forms.add(_normalized_text(text))
    return {form for form in forms if len(form) >= 8}


def _validated_oracle_evidence(
    value: Any,
    *,
    issue_text: str,
    behavior: BehaviorEvidence,
    source_context: str,
) -> list[dict[str, str]]:
    """Keep only evidence quotes that occur in an input visible to the model."""

    if not isinstance(value, list):
        return []
    sources = {
        "issue": issue_text,
        "behavior_target": json.dumps(
            behavior_prompt_payload(behavior, AblationConfig()), ensure_ascii=False
        ),
        "repository_context": source_context,
    }
    validated: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip().lower()
        quote = str(item.get("quote") or "").strip()
        normalized_quotes = _evidence_quote_forms(quote)
        if source not in sources or not normalized_quotes:
            continue
        normalized_source = _normalized_text(sources[source])
        if not any(item in normalized_source for item in normalized_quotes):
            continue
        validated.append({"source": source, "quote": quote})
    return validated


def _concrete_post_fix_risk(
    risk: Any, candidate_code: str
) -> tuple[str, bool, str, str, int, str, str]:
    """Require a named constraint and a real candidate location for high risk."""

    if not isinstance(risk, dict):
        return "unknown", False, "", "", 0, "unknown", "unknown"
    level = str(risk.get("level") or "unknown").strip().lower()
    if level not in _POST_FIX_RISKS:
        level = "unknown"
    constraint = str(risk.get("constraint") or "").strip()
    code_quote = str(risk.get("code_quote") or "").strip()
    try:
        line = int(risk.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    lines = candidate_code.splitlines()
    located = bool(code_quote and code_quote in candidate_code) or bool(
        1 <= line <= len(lines) and lines[line - 1].strip()
    )
    concrete = bool(constraint and located)
    role = str(risk.get("role") or "unknown").strip().lower()
    if role not in {"failure_driving", "additional", "unknown"}:
        role = "unknown"
    evidence_status = str(
        risk.get("evidence_status") or "unknown"
    ).strip().lower()
    if evidence_status not in {
        "supported", "contradicted", "unsupported", "unknown"
    }:
        evidence_status = "unknown"
    return level, concrete, constraint, code_quote, line, role, evidence_status


def _candidate_failure_line(
    candidate: CandidateTest, execution: ExecutionResult
) -> int | None:
    """Return the last traceback frame belonging to the generated test."""

    names = {
        Path(str(value)).name
        for value in (candidate.candidate_repo_path, candidate.candidate_file_path)
        if str(value or "").strip()
    }
    frames = _PYTEST_CANDIDATE_FRAME.findall(
        execution.stdout + "\n" + execution.stderr
    )
    matching = [
        int(line)
        for path, line in frames
        if Path(path.strip()).name in names
        or Path(path.strip()).name.startswith("test_brt_")
    ]
    return matching[-1] if matching else None


def _invalid_oracle_construction(
    candidate: CandidateTest, execution: ExecutionResult, behavior: BehaviorEvidence
) -> dict[str, str]:
    """Identify a failed assertion-context constructor, not a product failure.

    Require a real short-traceback frame, an imported pytest assertion API at
    that exact source location, and a terminal framework constructor error.
    If that framework API itself is the target, leave the semantic decision
    to the normal verifier. Missing/ambiguous trace evidence does not reject.
    """
    if execution.returncode == 0 or execution.timeout or not is_behavior_target(behavior):
        return {}
    anchors = _target_execution_anchors(behavior)
    if any(x in {"warns", "raises", "recwarn.py", "python_api.py", "raises.py"}
           or "_pytest/" in x for x in anchors):
        return {}
    log = execution.stdout + "\n" + execution.stderr
    errors = re.findall(r"(?m)^E\s+((?:TypeError|ValueError):[^\n]+)$", log)
    frames = re.findall(r"(?m)^([^\n]+\.py):(\d+): in ([^\n]+)$", log)
    if len(errors) != 1 or len(frames) < 2:
        return {}
    path, _, function = frames[-1]
    if function.strip() != "__init__" or not re.search(
        r"/_pytest/(?:recwarn|python_api|raises)\.py$", path.strip().replace("\\", "/")
    ):
        return {}
    names = {Path(p).name for p in [candidate.candidate_repo_path, candidate.candidate_file_path] if p}
    matching = [(index, int(line)) for index, (path, line, _) in enumerate(frames)
                if Path(path.strip()).name in names]
    if len(matching) != 1 or matching[0][0] != len(frames) - 2:
        return {}
    line = matching[0][1]
    try:
        tree = ast.parse(candidate.code)
    except SyntaxError:
        return {}
    aliases = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    aliases[alias.asname or alias.name] = "pytest"
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                aliases[alias.asname or alias.name] = "pytest." + alias.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.With) or len(node.items) != 1:
            continue
        call = node.items[0].context_expr
        if not isinstance(call, ast.Call) or not call.lineno <= line <= call.end_lineno:
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            name = aliases.get(func.value.id, "") + "." + func.attr
        elif isinstance(func, ast.Name):
            name = aliases.get(func.id, "")
        else:
            continue
        if name in {"pytest.warns", "pytest.raises"}:
            return {"code_quote": ast.get_source_segment(candidate.code, call) or "",
                    "error": errors[0], "api": name}
    return {}


def _target_no_exception_failure(
    behavior: BehaviorEvidence,
    execution: ExecutionResult,
    oracle_summary: dict[str, object],
) -> bool:
    """Recognize a target exception as the witness for a no-crash contract.

    A no-exception BRT is supposed to fail before reaching a later assertion.
    Requiring an LLM to additionally mark that execution as ``target_hit`` can
    incorrectly route a valid, focal failure into trigger repair.  Keep this
    override narrow: the observed log must contain both an exception named by
    the recovered symptom and a target API or source location.
    """

    if not bool(oracle_summary.get("no_exception_contract")):
        return False
    if execution.returncode == 0 or execution.status in {
        "SETUP_ERROR",
        "SYNTAX_ERROR",
        "COLLECT_ERROR",
        "TIMEOUT",
    }:
        return False

    log = _normalized_text(execution.stdout + "\n" + execution.stderr)
    symptom = error_symptom_text(behavior)
    exception_names = {
        item.casefold() for item in _EXCEPTION_NAME.findall(symptom)
    }
    if not exception_names or not any(item in log for item in exception_names):
        return False

    anchors = _target_execution_anchors(behavior)
    return bool(anchors and any(anchor in log for anchor in anchors))


def _target_execution_anchors(behavior: BehaviorEvidence) -> set[str]:
    """Return concrete API and source anchors carried by a structured target."""

    anchors: set[str] = set()
    for item in [*target_apis(behavior), *suspected_bug_locations(behavior)]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("symbol") or "").strip()
        if name:
            leaf = name.rsplit(".", 1)[-1].casefold()
            if len(leaf) >= 4:
                anchors.add(leaf)
        path = str(
            item.get("source_path")
            or item.get("path")
            or item.get("file")
            or ""
        ).strip()
        if path:
            normalized_path = path.replace("\\", "/").casefold()
            anchors.add(normalized_path)
            basename = normalized_path.rsplit("/", 1)[-1]
            if len(basename) >= 6:
                anchors.add(basename)
    return anchors


def _declared_no_exception_witness(
    behavior: BehaviorEvidence,
    candidate: CandidateTest,
    execution: ExecutionResult,
    oracle_kind: str,
) -> dict[str, object]:
    """Check a semantic no-exception observation against the actual traceback.

    The model may recognize a no-exception contract while its target_hit flag
    contradicts the observed target exception. Do not depend on particular
    English words in the recovered target. Require a single terminal exception,
    the candidate frame, and a subsequent named target frame in that traceback.
    This establishes invocation reachability, not correctness of the oracle.
    """
    if oracle_kind != 'NO_EXCEPTION' or not is_behavior_target(behavior):
        return {}
    if execution.returncode in (None, 0) or execution.timeout or execution.status in {
        'SETUP_ERROR', 'SYNTAX_ERROR', 'COLLECT_ERROR', 'TIMEOUT'
    }:
        return {}
    text = execution.stdout + '\n' + execution.stderr
    errors = list(re.finditer(
        r'(?m)^(?:E\s+)?(?P<type>[\w.]*(?:Error|Exception))(?::[^\n]*)?$', text))
    if len(errors) != 1:
        return {}
    error = errors[0]
    name = error.group('type').rsplit('.', 1)[-1]
    if name not in {x.rsplit('.', 1)[-1] for x in _EXCEPTION_NAME.findall(error_symptom_text(behavior))}:
        return {}
    frame_patterns = [
        r'(?m)^\s*File "(?P<path>[^"\n]+\.py)", line (?P<line>\d+), in (?P<func>[^\n]+)$',
        r'(?m)^(?P<path>[^\n]*?\.py):(?P<line>\d+): in (?P<func>[^\n]+)$',
    ]
    frames = sorted((m for pattern in frame_patterns
                     for m in re.finditer(pattern, text[:error.start()])), key=lambda m: m.start())

    def same_path(actual: str, relative: str) -> bool:
        actual = actual.strip().replace('\\', '/')
        relative = relative.strip().replace('\\', '/')
        if relative.startswith('./'):
            relative = relative[2:]
        return bool(relative and (actual == relative or actual.endswith('/' + relative)))

    candidate_paths = [candidate.candidate_repo_path, candidate.candidate_file_path]
    located = [i for i, frame in enumerate(frames) if any(
        same_path(frame['path'], str(path or '')) for path in candidate_paths)]
    if len(located) != 1:
        return {}
    index = located[0]
    if not 1 <= int(frames[index]['line']) <= len(candidate.code.splitlines()):
        return {}
    for frame in frames[index + 1:]:
        for item in [*target_apis(behavior), *suspected_bug_locations(behavior)]:
            path = str(item.get('source_path') or item.get('path') or '')
            symbol = str(item.get('name') or item.get('object') or item.get('symbol') or '')
            if same_path(frame['path'], path) and frame['func'].strip() == symbol.rsplit('.', 1)[-1]:
                return dict(exception=name, candidate_line=int(frames[index]['line']),
                            target_file=frame['path'].strip(), target_function=frame['func'].strip())
    return {}


def _explicit_target_symptom_mismatch(
    behavior: BehaviorEvidence,
    execution: ExecutionResult,
    oracle_summary: dict[str, object],
    *,
    issue_text: str = "",
) -> bool:
    """Reject an explicit observed exception that contradicts the target.

    The LLM sometimes explains that the candidate failed for an unrelated
    warning while still emitting ``target_hit=true``.  For a recovered
    no-exception contract with a named buggy exception, the exception names
    in the real execution log are a stronger signal than that Boolean.  Keep
    this check conservative: it only fires when both sides name exception or
    warning classes and the two sets are disjoint.
    """

    # A structured target tells us whether this is a no-exception contract.
    # The direct fallback intentionally has no structured target, so retain
    # an explicit exception mismatch guard using its raw issue traceback.
    if (
        not bool(oracle_summary.get("no_exception_contract"))
        and is_behavior_target(behavior)
    ):
        return False
    if execution.returncode == 0 or execution.status in {
        "SETUP_ERROR",
        "SYNTAX_ERROR",
        "COLLECT_ERROR",
        "TIMEOUT",
    }:
        return False

    structured_symptom = error_symptom_text(behavior)
    # The bounded direct fallback deliberately omits BehaviorTarget.  Preserve
    # the same concrete symptom guard there by falling back to exception names
    # explicitly present in the issue.  Prefer the structured symptom when it
    # exists because an issue can mention unrelated exceptions as context.
    target_source = structured_symptom or issue_text
    target_names = {
        item.casefold() for item in _EXCEPTION_NAME.findall(target_source)
    }
    observed_names = {
        item.casefold()
        for item in _EXCEPTION_NAME.findall(execution.stdout + "\n" + execution.stderr)
    }
    mismatch = bool(
        target_names and observed_names and target_names.isdisjoint(observed_names)
    )
    if not mismatch:
        return False

    # For a structured no-crash target, the exact exception class is an
    # observed buggy symptom rather than the post-fix oracle. The same defect
    # can surface at a nearby internal check in different repository/runtime
    # variants. Do not discard that candidate solely for the class difference
    # when both observations are failures and the real traceback reaches a
    # target API/source anchor. Warning-only paths remain distinct.
    target_failures = {name for name in target_names if not name.endswith("warning")}
    observed_failures = {
        name for name in observed_names if not name.endswith("warning")
    }
    if (
        is_behavior_target(behavior)
        and bool(oracle_summary.get("no_exception_contract"))
        and target_failures
        and observed_failures
    ):
        log = _normalized_text(execution.stdout + "\n" + execution.stderr)
        anchors = _target_execution_anchors(behavior)
        if anchors and any(anchor in log for anchor in anchors):
            return False
    return True


def _programmatic_verdict(
    execution: ExecutionResult,
    *,
    target_hit: bool,
    evidence_grounded: bool,
    uses_public_behavior: bool,
    oracle_falsifiable: bool,
    concrete_oracle_risk: bool,
    target_no_exception_failure: bool = False,
) -> tuple[str, str, str]:
    """Compute the gate from observable facts rather than an LLM decision."""

    executable_failure = execution.returncode != 0 and execution.status not in {
        "SETUP_ERROR",
        "SYNTAX_ERROR",
        "COLLECT_ERROR",
        "TIMEOUT",
    }
    if not executable_failure:
        route = "repair_oracle" if target_hit else "repair_trigger"
        return route, "buggy_pass", "The buggy version did not produce an executable target failure."
    if not target_hit:
        return "repair_trigger", "target_not_hit", "The execution facts do not establish the target path."
    observable_target_behavior = uses_public_behavior or target_no_exception_failure
    if not (evidence_grounded and observable_target_behavior and oracle_falsifiable):
        return (
            "repair_oracle",
            "oracle_wrong",
            "The failure oracle lacks validated target evidence or a public falsifiable observation.",
        )
    if concrete_oracle_risk:
        return (
            "repair_oracle",
            "oracle_too_strong",
            "A concrete candidate constraint can fail independently after the target fix.",
        )
    return "accept", "issue_aligned", "Programmatic semantic facts satisfy the acceptance gate."


def _route_plan(decision: str) -> dict[str, Any]:
    """Supply one coherent semantic obligation to the existing repair stage."""

    plans = {
        "repair_setup": {
            "preserve": ["the recovered reproduction target"],
            "change": ["restore one executable setup obligation and its required bindings"],
            "operator": "CONFIG_MUTATION",
            "effect": "the candidate reaches the target test body",
        },
        "repair_trigger": {
            "preserve": ["working setup and the evidence-grounded oracle"],
            "change": ["complete one target-trigger obligation and its required bindings"],
            "operator": "CALL_CHAIN_EXTEND",
            "effect": "the buggy execution reaches the issue-specific behavior",
        },
        "repair_oracle": {
            "preserve": ["working setup, input, and target invocation"],
            "change": ["ground one public oracle obligation in traceable target evidence"],
            "operator": "ORACLE_REFOCUS",
            "effect": "the target behavior produces a public falsifiable failure",
        },
        "accept": {
            "preserve": ["the accepted target path and public oracle"],
            "change": [],
            "operator": "",
            "effect": "preserve the accepted candidate",
        },
    }
    return plans.get(decision, plans["repair_trigger"])


def _forced_result(
    instance_id: str,
    execution: ExecutionResult,
    reason: str,
) -> StrictVerifierResult | None:
    """Handle only mechanical outcomes; semantic failures always go to the LLM."""
    mapping = {
        "SETUP_ERROR": ("repair_setup", "setup"),
        "SYNTAX_ERROR": ("repair_setup", "syntax"),
        "COLLECT_ERROR": ("repair_setup", "collect"),
        "TIMEOUT": ("reject", "timeout"),
    }
    if execution.status not in mapping:
        return None
    decision, failure = mapping[execution.status]
    return StrictVerifierResult(
        instance_id=instance_id,
        decision=decision,
        failure_class=failure,
        target_hit=False,
        oracle_grounded_in_issue=False,
        uses_public_behavior=False,
        oracle_kind="",
        oracle_falsifiable=False,
        reason=reason or execution.status,
        next_action=decision,
        observed_behavior=reason or execution.status,
        semantic_gap=("reach the target behavior" if execution.status == "PASS" else "restore executable setup"),
        preserve=["the selected seed protocol and Issue-grounded behavior"],
        change=[decision.replace("repair_", "")],
        avoid=[execution.status],
        next_operator=("CALL_CHAIN_EXTEND" if execution.status == "PASS" else "CONFIG_MUTATION"),
        expected_effect="buggy execution reaches an Issue-aligned, falsifiable failure",
        failure_origin=("test_body" if execution.status == "PASS" else "setup"),
        post_fix_failure_risk="unknown",
    )


def verify_strict_semantics(
    issue_text: str,
    behavior: BehaviorEvidence,
    protocol: ProtocolRecovery | None,
    candidate: CandidateTest,
    execution: ExecutionResult,
    source_context: str,
    llm_client: Any,
    output_dir: str,
    round_id: int,
    ablation_config: AblationConfig | None = None,
    *,
    defect_feedback: bool = False,
) -> tuple[VerifierDecision, StrictVerifierResult]:
    """Judge Issue alignment from the real buggy execution and full Issue text."""
    config = (ablation_config or AblationConfig()).validate()
    result = _forced_result(candidate.instance_id, execution, execution.status)
    construction_error = (
        _invalid_oracle_construction(candidate, execution, behavior)
        if result is None else {}
    )
    if construction_error:
        quote = construction_error["code_quote"]
        result = StrictVerifierResult(
            instance_id=candidate.instance_id,
            decision="repair_oracle", failure_class="oracle_wrong",
            target_hit=False, oracle_grounded_in_issue=False,
            uses_public_behavior=False, oracle_kind="FRAMEWORK_ASSERTION",
            oracle_falsifiable=False,
            reason="The assertion context constructor failed before its body ran: "
                   + construction_error["error"],
            next_action="repair_oracle",
            observed_behavior=construction_error["error"],
            semantic_gap="The assertion context must be constructed before it can observe the target call.",
            preserve=["the issue-required input, target invocation, and expected behavior"],
            change=["Repair the assertion-context construction at " + quote
                    + " using the recorded API error, keeping the intended observation."],
            avoid=["Do not treat the assertion constructor error as a product failure."],
            next_operator="ORACLE_REFOCUS",
            expected_effect="the target call runs under a valid assertion context",
            failure_origin="oracle_construction",
            post_fix_failure_risk="unknown",
        )
    if result is None:
        static_oracle = oracle_contract_summary(behavior, candidate.code)
        prompt = STRICT_SEMANTIC_VERIFIER_USER_PROMPT.format(
            issue_text=issue_text,
            behavior_json=json.dumps(
                behavior_prompt_payload(behavior, config), ensure_ascii=False
            ),
            protocol_json=json.dumps(
                protocol.to_dict() if protocol else {}, ensure_ascii=False
            ),
            candidate_code=candidate.code,
            command=execution.command,
            execution_status=execution.status,
            execution_log=truncate_text(
                execution.stdout + "\n" + execution.stderr, 16000
            ),
            source_context=truncate_text(source_context, 18000),
        )
        prompt = render_evidence_prompt(prompt, behavior)
        prompt = render_ablation_prompt(prompt, config)
        if defect_feedback:
            prompt += "\n" + DEFECT_PROMPT
        system_prompt = render_ablation_prompt(
            STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT,
            config,
            include_banner=False,
        )
        write_text(
            str(
                Path(output_dir)
                / "prompts"
                / f"strict_verifier_round_{round_id}.txt"
            ),
            system_prompt + "\n\n" + prompt,
        )
        # Validation is a fact-extraction step, not a diversity source. Keep
        # generation temperature independent while making this control signal
        # stable across providers and repeated runs.
        verifier_reasoning = (
            "low"
            if str(getattr(llm_client, "provider", "")).lower() == "gpt"
            and str(getattr(llm_client, "model", "")).lower() == "gpt-5-mini"
            else None
        )
        response = llm_client.chat(
            system_prompt,
            prompt,
            temperature=0.0,
            reasoning_effort=verifier_reasoning,
        )
        write_text(
            str(
                Path(output_dir)
                / "responses"
                / f"strict_verifier_round_{round_id}.txt"
            ),
            response,
        )
        data = extract_json_object(response)
        evidence_refs = _validated_oracle_evidence(
            data.get("oracle_evidence"),
            issue_text=issue_text,
            behavior=behavior,
            source_context=source_context,
        )
        risk_payload = data.get("post_fix_risk")
        if not isinstance(risk_payload, dict):
            risk_payload = {
                "level": data.get("post_fix_failure_risk"),
                "constraint": data.get("post_fix_risk_constraint"),
                "code_quote": data.get("post_fix_risk_code_quote"),
                "line": data.get("post_fix_risk_line"),
            }
        (
            risk_level,
            concrete_risk,
            risk_constraint,
            risk_quote,
            risk_line,
            risk_role,
            risk_evidence_status,
        ) = (
            _concrete_post_fix_risk(risk_payload, candidate.code)
        )
        failure_line = _candidate_failure_line(candidate, execution)
        risk_downstream = bool(
            concrete_risk
            and failure_line is not None
            and risk_line > failure_line
        )
        target_hit = _as_bool(data.get("target_hit"))
        public_behavior = _as_bool(data.get("uses_public_behavior"))
        oracle_kind = str(data.get("oracle_kind") or ",".join(static_oracle["kinds"]))
        oracle_falsifiable = bool(static_oracle["falsifiable"]) or _as_bool(
            data.get("oracle_falsifiable")
        )
        evidence_grounded = bool(evidence_refs)
        target_no_exception_failure = _target_no_exception_failure(
            behavior,
            execution,
            static_oracle,
        )
        declared_witness = (_declared_no_exception_witness(
            behavior, candidate, execution, oracle_kind
        ) if evidence_grounded and public_behavior and not target_hit else {})
        target_symptom_mismatch = _explicit_target_symptom_mismatch(
            behavior,
            execution,
            static_oracle,
            issue_text=issue_text,
        )
        target_hit = (target_hit or target_no_exception_failure or bool(declared_witness)) and not target_symptom_mismatch
        program_decision, program_failure, program_reason = _programmatic_verdict(
            execution,
            target_hit=target_hit,
            evidence_grounded=evidence_grounded,
            uses_public_behavior=public_behavior,
            oracle_falsifiable=oracle_falsifiable,
            concrete_oracle_risk=bool(
                concrete_risk
                and (
                    risk_level == "high"
                    or (
                        risk_level == "medium"
                        and risk_role == "additional"
                        and risk_downstream
                        and risk_evidence_status in {"contradicted", "unsupported"}
                    )
                )
            ),
            target_no_exception_failure=target_no_exception_failure,
        )
        defect = validated_candidate_defect(
            data.get("candidate_defect"), candidate.code,
            {"issue": issue_text,
             "repository_context": truncate_text(source_context, 18000),
             "execution_log": truncate_text(execution.stdout + "\n" + execution.stderr, 16000)},
        ) if defect_feedback else {}
        if defect:
            program_decision = "repair_" + defect["kind"]
            program_failure = {"setup": "setup", "trigger": "target_not_hit", "oracle": "oracle_wrong"}[defect["kind"]]
            program_reason = "The model identified a referenced candidate defect: " + defect["explanation"]
        plan = dict(_route_plan(program_decision))
        # Preserve the concrete diagnosis for repair without converting a
        # descriptive statement (possibly desired behavior) into a prohibition.
        if program_decision == "repair_oracle" and concrete_risk:
            location = risk_quote or f"candidate line {risk_line}"
            plan["change"] = [*plan["change"],
                "Recheck the located oracle against the cited evidence: " + location]
        if defect:
            plan["change"] = [defect["repair"]]
        analysis_reason = str(data.get("reason") or "").strip()
        result = StrictVerifierResult(
            instance_id=candidate.instance_id,
            decision=program_decision,
            failure_class=program_failure,
            target_hit=target_hit,
            oracle_grounded_in_issue=any(
                item["source"] == "issue" for item in evidence_refs
            ),
            oracle_grounded_in_target_evidence=evidence_grounded,
            oracle_evidence_refs=evidence_refs,
            uses_public_behavior=public_behavior,
            oracle_kind=oracle_kind,
            oracle_falsifiable=oracle_falsifiable,
            reason=(
                program_reason
                + (' The candidate traceback reaches the named target and raises the reported exception.'
                   if declared_witness else '')
                + ((" " + analysis_reason) if analysis_reason else "")
            ),
            next_action=program_decision,
            observed_behavior=str(data.get("observed_behavior") or ""),
            target_behavior=str(data.get("target_behavior") or ""),
            semantic_gap=str(data.get("semantic_gap") or data.get("gap") or ""),
            preserve=list(plan["preserve"]),
            change=list(plan["change"]),
            # Risk descriptions are diagnostic evidence, not avoid commands.
            avoid=(
                ["Do not retain the identified test defect at: " + defect["code_quote"]]
                if defect
                else []
            ),
            next_operator=str(plan["operator"]),
            expected_effect=str(plan["effect"]),
            failure_origin=str(data.get("failure_origin") or ""),
            post_fix_failure_risk=risk_level,
            post_fix_risk_concrete=concrete_risk,
            post_fix_risk_constraint=risk_constraint,
            post_fix_risk_code_quote=risk_quote,
            post_fix_risk_line=risk_line,
            post_fix_risk_role=risk_role,
            post_fix_risk_evidence_status=risk_evidence_status,
            post_fix_risk_downstream=risk_downstream,
            candidate_defect=defect,
        )

    safe_json_dump(
        result.to_dict(),
        str(Path(output_dir) / f"strict_verifier_round_{round_id}.json"),
    )
    decision = VerifierDecision(
        instance_id=candidate.instance_id,
        decision=result.decision,
        reason=result.reason,
        focus=[result.next_action.replace("repair_", "")],
        next_action=result.next_action,
    )
    return decision, result

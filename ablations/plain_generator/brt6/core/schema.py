"""Dataclasses used by the BRT6 pipeline."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from .utils import safe_json_dump


class JsonMixin:
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def save_json(self, path: str) -> None:
        safe_json_dump(self.to_dict(), path)


@dataclass
class RetrievedCode(JsonMixin):
    instance_id: str
    obj_name: str = ""
    node_type: str = ""
    path: str = ""
    code_start_line: str | int = ""
    code_end_line: str | int = ""
    code_content: str = ""
    parent: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievedTest(JsonMixin):
    instance_id: str
    name: str = ""
    file: str = ""
    code_content: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstanceContext(JsonMixin):
    instance_id: str
    issue_text: str
    repo: str = ""
    base_commit: str = ""
    buggy_repo_path: str = ""
    retrieved_code: list[RetrievedCode] = field(default_factory=list)
    retrieved_tests: list[RetrievedTest] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BehaviorTarget(JsonMixin):
    instance_id: str
    issue_summary: str = ""
    trigger_condition: dict[str, Any] = field(default_factory=dict)
    error_symptom: dict[str, Any] = field(default_factory=dict)
    expected_behavior: dict[str, Any] = field(default_factory=dict)
    target_apis: list[dict[str, Any]] = field(default_factory=list)
    suspected_bug_locations: list[dict[str, Any]] = field(default_factory=list)
    related_test_seeds: list[dict[str, Any]] = field(default_factory=list)
    mutation_hints: list[dict[str, Any]] = field(default_factory=list)
    observation_points: list[dict[str, Any]] = field(default_factory=list)
    assertion_hints: list[dict[str, Any]] = field(default_factory=list)
    setup_hints: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    safety_constraints: list[dict[str, Any]] = field(default_factory=list)
    audit_warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    schema_version: str = "behavior_target.lossless.v1"

    def setup_view(self) -> dict[str, Any]:
        """Return the setup slice without dropping the original rich fields."""
        return {
            "setup_hints": self.setup_hints,
            "related_test_seeds": self.related_test_seeds,
        }

    def trigger_view(self) -> dict[str, Any]:
        """Return the trigger slice used by mutation and semantic validation."""
        return {
            "trigger_condition": self.trigger_condition,
            "error_symptom": self.error_symptom,
            "target_apis": self.target_apis,
            "suspected_bug_locations": self.suspected_bug_locations,
            "mutation_hints": self.mutation_hints,
            "safety_constraints": self.safety_constraints,
            "audit_warnings": self.audit_warnings,
        }

    def oracle_view(self) -> dict[str, Any]:
        """Return the fixed-side oracle slice used by generation and validation."""
        return {
            "expected_behavior": self.expected_behavior,
            "observation_points": self.observation_points,
            "assertion_hints": self.assertion_hints,
        }

    def to_dict(self) -> dict[str, Any]:
        """Persist a three-part view while retaining every P0 evidence field."""
        return {
            "schema_version": self.schema_version,
            "instance_id": self.instance_id,
            "issue_summary": self.issue_summary,
            "setup": self.setup_view(),
            "trigger": self.trigger_view(),
            "oracle": self.oracle_view(),
            "uncertainties": self.uncertainties,
            "raw": self.raw,
        }


@dataclass
class RawIssueContext(JsonMixin):
    """Unstructured evidence used by the ``w/o Behavior Target`` ablation.

    This object deliberately has no setup/trigger/oracle fields.  Retrieved
    production code and tests remain in ``InstanceContext`` and are passed to
    the same downstream stages through their existing arguments.
    """

    instance_id: str
    issue_text: str
    schema_version: str = "raw_issue_context.v1"
    representation: str = "raw_issue"
    ablation_name: str = "w/o Behavior Target"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "representation": self.representation,
            "ablation_name": self.ablation_name,
            "instance_id": self.instance_id,
            "issue_text": self.issue_text,
        }


@dataclass
class HostContext(JsonMixin):
    instance_id: str
    host_file: str = ""
    host_class: str = ""
    seed_test_name: str = ""
    seed_test_code: str = ""
    reference_seed_tests: list[dict[str, Any]] = field(default_factory=list)
    imports: str = ""
    setup_context: str = ""
    model_context: str = ""
    fixtures: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    pytestmark: str = ""
    test_command: str = ""
    seed_execution_status: str = "ERROR"
    seed_execution: dict[str, Any] = field(default_factory=dict)
    insert_strategy: str = "same_dir_new_file"
    insert_location_hint: str = ""
    adjacent_tests: list[str] = field(default_factory=list)
    full_test_file_path: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProtocolRecovery(JsonMixin):
    instance_id: str
    test_file: str = ""
    test_framework: str = "unknown"
    test_command: str = ""
    imports: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    pytest_marks: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    module_context: list[str] = field(default_factory=list)
    class_context: str = ""
    setup_methods: list[str] = field(default_factory=list)
    teardown_methods: list[str] = field(default_factory=list)
    local_helpers: list[dict[str, str]] = field(default_factory=list)
    local_models: list[dict[str, str]] = field(default_factory=list)
    conftest_context: list[dict[str, Any]] = field(default_factory=list)
    runner_hints: list[str] = field(default_factory=list)
    protocol_risks: list[str] = field(default_factory=list)
    selected_seed_name: str = ""
    placement_dir: str = ""


@dataclass
class SemanticDelta(JsonMixin):
    """The one semantic difference applied during a feedback round."""

    instance_id: str
    round_id: int = 0
    schema_version: str = "semantic_delta.v2"
    action: str = "KEEP"
    dimension: str = ""
    seed_fact: str = ""
    target_fact: str = ""
    change: str = ""
    preserve: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    reason: str = ""
    status: str = "INVALID"
    errors: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return self.status == "VALID" and self.action == "MUTATE"


@dataclass
class StrictVerifierResult(JsonMixin):
    instance_id: str
    decision: str = "reject"
    failure_class: str = "side_path"
    target_hit: bool = False
    oracle_grounded_in_issue: bool = False
    uses_public_behavior: bool = False
    oracle_kind: str = ""
    oracle_falsifiable: bool = False
    reason: str = ""
    next_action: str = "reject"
    observed_behavior: str = ""
    target_behavior: str = ""
    semantic_gap: str = ""
    preserve: list[str] = field(default_factory=list)
    change: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    next_operator: str = ""
    expected_effect: str = ""
    failure_origin: str = ""
    post_fix_failure_risk: str = "unknown"


@dataclass
class CandidateTest(JsonMixin):
    instance_id: str
    round_id: int = 0
    code: str = ""
    candidate_file_path: str = ""
    candidate_repo_path: str = ""
    pytest_nodeid: str = ""
    command: str = ""
    prompt_path: str = ""
    response_path: str = ""
    status: str = "CREATED"
    notes: str = ""
    semantic_delta: dict[str, Any] = field(default_factory=dict)
    delta_history: list[dict[str, Any]] = field(default_factory=list)
    delta_application: dict[str, Any] = field(default_factory=dict)
    oracle_contract_kinds: list[str] = field(default_factory=list)
    oracle_contract_preserved: bool = True
    oracle_contract_violation: str = ""


@dataclass
class CandidateCheckpoint(JsonMixin):
    instance_id: str
    round_id: int
    code_path: str = ""
    score: int = 0
    reason: str = ""
    oracle_risk: dict[str, Any] = field(default_factory=dict)
    surrogate_risk: dict[str, Any] = field(default_factory=dict)
    selector_score_before_risk: int = 0
    selector_score_after_risk: int = 0
    selector_penalty_reasons: list[str] = field(default_factory=list)
    execution: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    surrogate: dict[str, Any] = field(default_factory=dict)
    issue_aligned: bool = False
    target_hit: bool = False
    oracle_grounded_in_issue: bool = False
    uses_public_behavior: bool = False
    semantic_delta: dict[str, Any] = field(default_factory=dict)
    delta_history: list[dict[str, Any]] = field(default_factory=list)
    delta_application: dict[str, Any] = field(default_factory=dict)
    oracle_contract_kinds: list[str] = field(default_factory=list)
    oracle_contract_preserved: bool = True
    oracle_contract_violation: str = ""
    rank_key: list[Any] = field(default_factory=list)
    selected: bool = False


@dataclass
class ExecutionResult(JsonMixin):
    instance_id: str = ""
    command: str = ""
    cwd: str = ""
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timeout: bool = False
    status: str = "PASS"
    error_reason: str = ""


@dataclass
class ObservationReport(JsonMixin):
    instance_id: str
    probe_code: str = ""
    probe_file_path: str = ""
    execution: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    status: str = "UNKNOWN"


@dataclass
class VerifierDecision(JsonMixin):
    instance_id: str
    decision: str = "reject"
    reason: str = ""
    focus: list[str] = field(default_factory=list)
    next_action: str = ""


@dataclass
class DualVersionResult(JsonMixin):
    instance_id: str
    mode: str = "buggy_only"
    buggy_execution: dict[str, Any] = field(default_factory=dict)
    patched_execution: dict[str, Any] = field(default_factory=dict)
    status: str = "NOT_RUN"
    notes: str = ""
    surrogate_patch: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SurrogatePatchCandidate(JsonMixin):
    instance_id: str
    round_id: int = 0
    patches: list[dict[str, Any]] = field(default_factory=list)
    applied_paths: list[str] = field(default_factory=list)
    diff: str = ""
    status: str = "CREATED"
    reason: str = ""
    prompt_path: str = ""
    response_path: str = ""


@dataclass
class FinalResult(JsonMixin):
    instance_id: str
    status: str = "BEST_EFFORT"
    final_test_path: str = ""
    rounds_used: int = 0
    buggy_execution: dict[str, Any] = field(default_factory=dict)
    dual_version_result: dict[str, Any] = field(default_factory=dict)
    behavior_target: dict[str, Any] = field(default_factory=dict)
    raw_issue_context: dict[str, Any] = field(default_factory=dict)
    behavior_target_enabled: bool = True
    method_variant: str = "full"
    ablation_id: str = "full"
    ablation_signature: str = ""
    ablation_config: dict[str, Any] = field(default_factory=dict)
    host_context: dict[str, Any] = field(default_factory=dict)
    observation_report: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    protocol_recovery_enabled: bool = False
    seed_mutation_enabled: bool = False
    strict_verifier_enabled: bool = False
    selected_seed_file: str = ""
    selected_seed_name: str = ""
    seed_fallback_used: bool = False
    delta_calls: int = 0
    valid_delta_calls: int = 0
    keep_delta_calls: int = 0
    selected_seed_delta_calls: int = 0
    all_seed_delta_calls: int = 0
    final_semantic_delta: dict[str, Any] = field(default_factory=dict)
    delta_history: list[dict[str, Any]] = field(default_factory=list)
    final_delta_application: dict[str, Any] = field(default_factory=dict)
    repair_route_counts: dict[str, int] = field(default_factory=dict)
    selected_seed_repair_route_counts: dict[str, int] = field(default_factory=dict)
    all_seed_repair_route_counts: dict[str, int] = field(default_factory=dict)
    oracle_type: str = ""
    strict_verifier_decision: str = ""
    strict_failure_class: str = ""
    final_reason: str = ""
    seed_mode: str = ""
    selected_seed_index: int = -1
    seed_attempts_count: int = 0
    seed_attempts_summary: list[dict[str, Any]] = field(default_factory=list)
    seed_switch_reasons: list[str] = field(default_factory=list)
    selected_seed_reason: str = ""
    selection_route: str = "target_primary"
    target_primary_status: str = ""
    direct_fallback_attempted: bool = False
    direct_fallback_used: bool = False
    direct_fallback_status: str = ""
    target_seed_attempts_summary: list[dict[str, Any]] = field(default_factory=list)
    direct_fallback_seed_attempts_summary: list[dict[str, Any]] = field(default_factory=list)
    final_oracle_risk: dict[str, Any] = field(default_factory=dict)
    final_surrogate_risk: dict[str, Any] = field(default_factory=dict)
    candidate_repo_path: str = ""
    pytest_nodeid: str = ""
    command: str = ""
    direct_test_repo_path_hint: str = ""
    placement_dir: str = ""
    runner_kind: str = ""
    selector: str = ""
    method_version: str = "execution-guided-single-delta-v1"
    behavior_schema_version: str = "behavior_target.lossless.v1"
    surrogate_patch_calls: int = 0

"""Small policy primitives for the execution-guided single-Delta loop."""

from __future__ import annotations

from ..core.schema import ExecutionResult, SemanticDelta
from ..validation.delta_guard import GuardResult


MAX_SEMANTIC_ROUNDS = 5


def semantic_round_budget(requested: int) -> int:
    """Keep the paper algorithm bounded to one through five rounds."""

    return min(MAX_SEMANTIC_ROUNDS, max(1, int(requested)))


def static_failure_execution(
    instance_id: str,
    command: str,
    cwd: str,
    guard: GuardResult,
) -> ExecutionResult:
    """Represent static guard failures through the normal execution protocol."""

    return ExecutionResult(
        instance_id=instance_id,
        command=command,
        cwd=cwd,
        returncode=2,
        stderr="\n".join(guard.errors),
        status="SYNTAX_ERROR",
        error_reason="minimal static guard failed",
    )


def repeated_keep(deltas: list[SemanticDelta]) -> bool:
    """Stop only when two consecutive KEEP decisions are semantically identical."""

    return bool(
        len(deltas) >= 2
        and deltas[-1].action == deltas[-2].action == "KEEP"
        and deltas[-1].reason == deltas[-2].reason
        and deltas[-1].target_fact == deltas[-2].target_fact
    )

"""Stable mapping from typed v2 results to legacy-compatible response metadata."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.memory_v2.contracts import MemoryCommandResult, MemoryOutcome


@dataclass(frozen=True)
class MemoryCompatibilityResult:
    outcome: str
    operation_id: str | None
    active_memory_id: str | None
    active_memory_ids: tuple[str, ...]
    affected_memory_ids: tuple[str, ...]
    revision: int | None
    rejection_code: str | None
    error_code: str | None
    review_required: bool
    committed: bool
    message: str | None


def map_compatibility_result(result: MemoryCommandResult) -> MemoryCompatibilityResult:
    review_required = result.outcome is MemoryOutcome.NEEDS_REVIEW
    committed = result.outcome in {
        MemoryOutcome.CREATED,
        MemoryOutcome.RECONFIRMED,
        MemoryOutcome.REFINED,
        MemoryOutcome.REPLACED,
        MemoryOutcome.SUPERSEDED,
        MemoryOutcome.MERGED,
        MemoryOutcome.ARCHIVED,
        MemoryOutcome.FORGOTTEN,
        MemoryOutcome.ERASED_PERMANENTLY,
        MemoryOutcome.RESTORED,
    }
    active = tuple(str(item) for item in result.active_memory_ids)
    return MemoryCompatibilityResult(
        outcome=result.outcome.value,
        operation_id=str(result.operation_id),
        active_memory_id=active[0] if active else None,
        active_memory_ids=active,
        affected_memory_ids=tuple(str(item) for item in result.affected_memory_ids),
        revision=result.current_revision,
        rejection_code=result.rejection_code.value if result.rejection_code else None,
        error_code=result.error_code.value if result.error_code else None,
        review_required=review_required,
        committed=committed,
        message=result.message,
    )

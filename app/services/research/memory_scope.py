"""Scoped memory retrieval for research context — only fetches relevant memories."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.services.memory.prompt import RecallPromptOrchestrator
from app.services.memory.queries import MemoryQueryContext, RecallQuery


class ResearchMemoryRecallResult(BaseModel):
    """Bounded research-memory payload plus text-free structured diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_text: str = ""
    canonical_ids: tuple[str, ...] = ()
    diagnostic: dict[str, Any]


def retrieve_scoped_memory_result(
    query: str,
    *,
    enabled: bool = False,
    orchestrator: RecallPromptOrchestrator | None = None,
    query_context: MemoryQueryContext | None = None,
    usage_purpose: str = "research_plan",
) -> ResearchMemoryRecallResult:
    """Return scoped memory with a diagnostic that never contains recalled text."""
    if enabled:
        if orchestrator is None or query_context is None:
            return ResearchMemoryRecallResult(
                diagnostic={
                    "mode": "canonical",
                    "reason_codes": ["missing_authenticated_owner_binding"],
                    "final_injected_ids": [],
                    "usage_event_ids": [],
                }
            )
        selection = orchestrator.build(
            RecallQuery(context=query_context, text=query),
            purpose=usage_purpose,
        )
        serialized = selection.serialized
        diagnostic = selection.recall.diagnostic.model_dump(mode="json")
        diagnostic["usage_recorded"] = selection.usage_recorded
        diagnostic["usage_failure_code"] = selection.usage_failure_code
        return ResearchMemoryRecallResult(
            context_text=serialized.content if serialized is not None else "",
            canonical_ids=(
                tuple(str(item) for item in serialized.canonical_ids)
                if serialized is not None
                else ()
            ),
            diagnostic=diagnostic,
        )

    return ResearchMemoryRecallResult(
        diagnostic={"mode": "disabled", "reason_codes": ["memory_disabled"]}
    )


def retrieve_scoped_memory(
    query: str,
    *,
    enabled: bool = False,
    orchestrator: RecallPromptOrchestrator | None = None,
    query_context: MemoryQueryContext | None = None,
    usage_purpose: str = "research_plan",
) -> tuple[str, list[str]]:
    """Return (memory_context_text, list_of_memory_keys_used).

    Only retrieves memory categories relevant to the research query.
    Returns empty string if no personal context is needed.
    """
    result = retrieve_scoped_memory_result(
        query,
        enabled=enabled,
        orchestrator=orchestrator,
        query_context=query_context,
        usage_purpose=usage_purpose,
    )
    return result.context_text, list(result.canonical_ids)

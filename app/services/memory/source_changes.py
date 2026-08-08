"""Exact, typed message provenance changes for Phase 3."""

from __future__ import annotations

from uuid import UUID

from app.services.memory.adapters import GenericMemoryAdapter, MemoryAdapterContext
from app.services.memory.contracts import (
    DetachMemorySourceCommand,
    SourceChangeResult,
    TargetRevision,
)
from app.services.memory.idempotency import MemoryIdempotency


class MemorySourceChangeCoordinator:
    """Detach one persisted source; never infer or run a lifecycle command."""

    def __init__(self, adapter: GenericMemoryAdapter) -> None:
        self.adapter = adapter

    def delete_message_source(
        self,
        context: MemoryAdapterContext,
        *,
        message_id: str,
        edit_revision: int,
        target: TargetRevision,
        source_id: UUID,
    ) -> SourceChangeResult:
        key = MemoryIdempotency.source_change(
            context.execution.owner_id,
            message_id,
            edit_revision,
            str(target.memory_id),
            "delete",
        )
        return self.adapter.detach_source(
            context,
            DetachMemorySourceCommand(
                owner_id=context.execution.owner_id,
                idempotency_key=key,
                target=target,
                source_id=source_id,
            ),
        )

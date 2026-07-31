"""Exact, typed message provenance changes for Phase 3."""

from __future__ import annotations

from uuid import UUID

from app.services.memory_v2.adapters import GenericMemoryV2Adapter, MemoryV2AdapterContext
from app.services.memory_v2.contracts import (
    DetachMemorySourceCommand,
    SourceChangeResult,
    TargetRevision,
)
from app.services.memory_v2.idempotency import MemoryV2Idempotency


class MemoryV2SourceChangeCoordinator:
    """Detach one persisted source; never infer or run a lifecycle command."""

    def __init__(self, adapter: GenericMemoryV2Adapter) -> None:
        self.adapter = adapter

    def delete_message_source(
        self,
        context: MemoryV2AdapterContext,
        *,
        message_id: str,
        edit_revision: int,
        target: TargetRevision,
        source_id: UUID,
    ) -> SourceChangeResult:
        key = MemoryV2Idempotency.source_change(
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

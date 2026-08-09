"""Production chat integration for canonical personal memory."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories.memory import MemoryRepository, MemoryRepositoryError
from app.services.memory.prompt import (
    STABLE_MEMORY_POLICY,
    RecallPromptOrchestrator,
    repository_usage_recorder,
)
from app.services.memory.queries import (
    MemoryQueryContext,
    RecallMode,
    RecallPromptSelection,
    RecallQuery,
)
from app.services.memory.recall import CanonicalRecallService
from app.services.memory.runtime import build_memory_recall_dependencies
from app.services.memory.settings import MemoryConfigurationError, MemorySettings

_BROAD_MEMORY_QUERY = re.compile(
    r"\b(?:what do you remember|show (?:me )?(?:my )?saved memories|"
    r"summari[sz]e my current (?:goals|preferences|memories))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChatMemoryRuntime:
    orchestrator: RecallPromptOrchestrator
    context_factory: Callable[[str], MemoryQueryContext]


def build_chat_memory_runtime(
    db: Session,
    *,
    owner_id: str,
    database_identity: str,
    profile_id: str,
    request_id: str,
    session_id: str | None = None,
    active_project_id: str | None = None,
    memory_enabled: bool = True,
    incognito: bool = False,
) -> ChatMemoryRuntime | None:
    """Build request-owned Phase 5 chat wiring or fail closed without legacy fallback."""
    try:
        settings = get_settings()
        flags = MemorySettings.from_settings(settings)
        if not flags.enabled:
            return None
        repository = MemoryRepository(
            db,
            owner_id=owner_id,
            database_identity=database_identity,
        )
    except (MemoryConfigurationError, MemoryRepositoryError, ValueError):
        return None
    recall_dependencies = build_memory_recall_dependencies(
        db.get_bind(),
        owner_id=owner_id,
        database_identity=database_identity,
        flags=flags,
        settings=settings,
    )
    recall = CanonicalRecallService(
        repository,
        flags=flags,
        fts_index=recall_dependencies.fts_index,
        semantic_provider=recall_dependencies.semantic_provider,
        vector_index=recall_dependencies.vector_index,
        repair_scheduler=recall_dependencies.repair_scheduler,
        metric_recorder=recall_dependencies.metric_recorder,
        semantic_weight=settings.memory_semantic_weight,
        semantic_cap=settings.memory_semantic_cap,
        semantic_threshold=settings.semantic_similarity_threshold,
        vector_candidate_limit=settings.memory_vector_candidate_limit,
        fts_candidate_limit=settings.memory_fts_candidate_limit,
    )
    orchestrator = RecallPromptOrchestrator(
        recall,
        usage_recorder=repository_usage_recorder(repository),
    )

    def context_for(prompt: str) -> MemoryQueryContext:
        mode = RecallMode.BROAD if _BROAD_MEMORY_QUERY.search(prompt) else RecallMode.SCOPED_LEXICAL
        return MemoryQueryContext(
            owner_id=owner_id,
            database_identity=database_identity,
            profile_id=profile_id,
            memory_enabled=memory_enabled,
            incognito=incognito,
            request_id=request_id,
            session_id=session_id,
            active_project_id=active_project_id,
            current_time=datetime.now(UTC),
            maximum_records=flags.recall_max_records,
            maximum_characters=flags.recall_max_chars,
            mode=mode,
            lexical_available=flags.lexical_recall_enabled,
        )

    return ChatMemoryRuntime(orchestrator, context_for)


__all__ = (
    "MemoryQueryContext",
    "ChatMemoryRuntime",
    "RecallPromptOrchestrator",
    "RecallPromptSelection",
    "RecallQuery",
    "STABLE_MEMORY_POLICY",
    "build_chat_memory_runtime",
)

"""Stable compatibility boundary between legacy consumers and canonical memory."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories.memory_v2 import MemoryV2Repository, MemoryV2RepositoryError
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags, MemoryV2RolloutError
from app.services.memory_v2.prompt import (
    STABLE_MEMORY_POLICY,
    RecallPromptOrchestrator,
    repository_usage_recorder,
)
from app.services.memory_v2.queries import (
    MemoryQueryContext,
    RecallMode,
    RecallPromptSelection,
    RecallQuery,
)
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.runtime import build_phase6_recall_dependencies

_BROAD_MEMORY_QUERY = re.compile(
    r"\b(?:what do you remember|show (?:me )?(?:my )?saved memories|"
    r"summari[sz]e my current (?:goals|preferences|memories))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChatCanonicalMemoryRuntime:
    orchestrator: RecallPromptOrchestrator
    context_factory: Callable[[str], MemoryQueryContext]


def build_chat_canonical_memory_runtime(
    db: Session,
    *,
    owner_id: str,
    database_identity: str,
    profile_id: str,
    request_id: str,
    session_id: str | None = None,
    memory_enabled: bool = True,
    incognito: bool = False,
) -> ChatCanonicalMemoryRuntime | None:
    """Build request-owned Phase 5 chat wiring or fail closed without legacy fallback."""
    try:
        settings = get_settings()
        flags = MemoryV2FeatureFlags.from_settings(settings)
        if not flags.canonical_query_enabled or not flags.owner_is_enabled(owner_id):
            return None
        repository = MemoryV2Repository(
            db,
            owner_id=owner_id,
            database_identity=database_identity,
        )
    except (MemoryV2RolloutError, MemoryV2RepositoryError, ValueError):
        return None
    phase6 = build_phase6_recall_dependencies(
        db.get_bind(),
        owner_id=owner_id,
        database_identity=database_identity,
        flags=flags,
        settings=settings,
    )
    recall = CanonicalRecallService(
        repository,
        flags=flags,
        fts_index=phase6.fts_index,
        semantic_provider=phase6.semantic_provider,
        vector_index=phase6.vector_index,
        repair_scheduler=phase6.repair_scheduler,
        metric_recorder=phase6.metric_recorder,
        semantic_weight=settings.memory_v2_semantic_weight,
        semantic_cap=settings.memory_v2_semantic_cap,
        semantic_threshold=settings.semantic_similarity_threshold,
        vector_candidate_limit=settings.memory_v2_vector_candidate_limit,
        fts_candidate_limit=settings.memory_v2_fts_candidate_limit,
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
            current_time=datetime.now(UTC),
            maximum_records=flags.recall_max_records,
            maximum_characters=flags.recall_max_chars,
            mode=mode,
            lexical_available=flags.lexical_recall_enabled,
        )

    return ChatCanonicalMemoryRuntime(orchestrator, context_for)


__all__ = (
    "MemoryQueryContext",
    "ChatCanonicalMemoryRuntime",
    "RecallPromptOrchestrator",
    "RecallPromptSelection",
    "RecallQuery",
    "STABLE_MEMORY_POLICY",
    "build_chat_canonical_memory_runtime",
)

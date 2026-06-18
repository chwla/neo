from __future__ import annotations

from datetime import UTC, date, datetime
import re

from sqlalchemy import Select, case, exists, or_, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    Chat,
    ChatMessage,
    Event,
    Goal,
    GoalStatus,
    Memory,
    MemoryCandidate,
    MemoryEmbedding,
    MemoryLifecycleAudit,
    Preference,
    ProfileFact,
    Project,
    ProjectStatus,
)
from app.models.enums import CandidateStatus, MemoryType
from app.services.embeddings import (
    EmbeddingProvider,
    MemoryEmbeddingService,
    cosine_similarity,
    decode_vector,
)


QUERY_STOPWORDS = {
    "a",
    "am",
    "an",
    "and",
    "as",
    "at",
    "about",
    "are",
    "can",
    "currently",
    "do",
    "does",
    "for",
    "have",
    "i",
    "in",
    "is",
    "me",
    "my",
    "of",
    "on",
    "should",
    "the",
    "that",
    "this",
    "to",
    "use",
    "with",
    "what",
    "which",
}


class MemoryStore:
    """Repository facade over the local SQLite memory tables."""

    def __init__(
        self,
        db: Session,
        embedding_provider: EmbeddingProvider | None = None,
        semantic_enabled: bool | None = None,
        auto_embed: bool | None = None,
    ) -> None:
        self.db = db
        self._memory_fts_available: bool | None = None
        settings = get_settings()
        self.semantic_enabled = (
            semantic_enabled if semantic_enabled is not None else settings.semantic_retrieval_enabled
        )
        self.auto_embed = auto_embed if auto_embed is not None else settings.auto_embed_memories
        self.embedding_provider = embedding_provider
        self.embedding_service = (
            MemoryEmbeddingService(embedding_provider)
            if embedding_provider is not None or self.semantic_enabled or self.auto_embed
            else None
        )

    def add(self, entity):
        self.db.add(entity)
        self.db.flush()
        if isinstance(entity, Memory):
            self._sync_memory_fts(entity)
            self._sync_memory_embedding(entity)
            self.record_lifecycle_audit(
                entity,
                "created",
                previous_status=None,
                new_status=entity.status,
                reason=entity.update_reason or "Memory created.",
                source_sentence=entity.source_sentence,
            )
        return entity

    def create_chat(self, project_id: int | None = None, title: str = "New chat") -> Chat:
        return self.add(Chat(title=title, project_id=project_id, archived=False))

    def get_chat(self, chat_id: int) -> Chat | None:
        return self.db.get(Chat, chat_id)

    def get_project(self, project_id: int) -> Project | None:
        return self.db.get(Project, project_id)

    def list_chats(
        self,
        project_id: int | None = None,
        unprojected_only: bool = False,
        with_messages_only: bool = False,
        limit: int = 50,
    ) -> list[Chat]:
        stmt = (
            select(Chat)
            .where(Chat.archived.is_(False))
            .order_by(Chat.updated_at.desc(), Chat.id.desc())
            .limit(limit)
        )
        if unprojected_only:
            stmt = stmt.where(Chat.project_id.is_(None))
        elif project_id is not None:
            stmt = stmt.where(Chat.project_id == project_id)
        if with_messages_only:
            stmt = stmt.where(
                exists().where(ChatMessage.chat_id == Chat.id)
            )
        return list(self.db.scalars(stmt))

    def list_chat_messages(self, chat_id: int) -> list[ChatMessage]:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
        return list(self.db.scalars(stmt))

    def add_chat_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        duration_ms: int | None = None,
        thinking: str | None = None,
    ) -> ChatMessage:
        message = self.add(
            ChatMessage(
                chat_id=chat_id,
                role=role,
                content=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms,
                thinking=thinking,
            )
        )
        chat = self.get_chat(chat_id)
        if chat is not None:
            chat.updated_at = message.created_at
        return message

    def update_chat_message_content(self, message_id: int, content: str) -> ChatMessage | None:
        message = self.db.get(ChatMessage, message_id)
        if message is None:
            return None
        message.content = content
        self.db.flush()
        return message

    def rename_chat_from_prompt(self, chat_id: int, prompt: str) -> None:
        chat = self.get_chat(chat_id)
        if chat is None or chat.title != "New chat":
            return
        title = " ".join(prompt.strip().split())
        chat.title = title[:54] + "..." if len(title) > 57 else title or "New chat"

    def assign_chat_to_project(self, chat_id: int, project_id: int | None) -> None:
        chat = self.get_chat(chat_id)
        if chat is not None:
            chat.project_id = project_id
            self.db.flush()

    def delete_chat(self, chat_id: int) -> None:
        chat = self.get_chat(chat_id)
        if chat is not None:
            self.db.delete(chat)
            self.db.flush()

    def delete_project(self, project_id: int) -> None:
        project = self.get_project(project_id)
        if project is None:
            return
        for chat in list(project.chats):
            self.db.delete(chat)
        self.db.delete(project)
        self.db.flush()

    def create_project(
        self,
        name: str,
        description: str | None = None,
        priority: int = 5,
    ) -> Project:
        return self.add(Project(name=name, description=description, priority=priority))

    def list_profile(self, active_only: bool = True) -> list[ProfileFact]:
        stmt = select(ProfileFact).order_by(ProfileFact.key)
        if active_only:
            stmt = stmt.where(ProfileFact.is_active.is_(True))
        return list(self.db.scalars(stmt))

    def list_preferences(self, active_only: bool = True) -> list[Preference]:
        stmt = select(Preference).order_by(Preference.importance.desc(), Preference.category)
        if active_only:
            stmt = stmt.where(Preference.is_active.is_(True))
        return list(self.db.scalars(stmt))

    def list_goals(self, status: GoalStatus | None = None) -> list[Goal]:
        stmt = select(Goal).order_by(Goal.priority.desc(), Goal.updated_at.desc())
        if status is not None:
            stmt = stmt.where(Goal.status == status)
        return list(self.db.scalars(stmt))

    def list_projects(self, status: ProjectStatus | None = None) -> list[Project]:
        stmt = select(Project).order_by(Project.priority.desc(), Project.updated_at.desc())
        if status is not None:
            stmt = stmt.where(Project.status == status)
        return list(self.db.scalars(stmt))

    def list_events(self, limit: int = 50) -> list[Event]:
        stmt = (
            select(Event)
            .order_by(Event.event_date.desc().nullslast(), Event.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def list_memories(self, active_only: bool = True, limit: int = 50) -> list[Memory]:
        stmt = (
            select(Memory)
            .order_by(Memory.importance.desc(), Memory.updated_at.desc())
            .limit(limit)
        )
        if active_only:
            stmt = stmt.where(Memory.is_active.is_(True), Memory.status == "active")
        return list(self.db.scalars(stmt))

    def list_candidates(
        self,
        status: CandidateStatus | None = CandidateStatus.PENDING,
        limit: int = 100,
    ) -> list[MemoryCandidate]:
        stmt = select(MemoryCandidate).order_by(MemoryCandidate.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(MemoryCandidate.status == status)
        return list(self.db.scalars(stmt))

    def get_candidate(self, candidate_id: int) -> MemoryCandidate | None:
        return self.db.get(MemoryCandidate, candidate_id)

    def get_memory(self, memory_id: int) -> Memory | None:
        return self.db.get(Memory, memory_id)

    def get_profile_fact(self, profile_id: int) -> ProfileFact | None:
        return self.db.get(ProfileFact, profile_id)

    def get_preference(self, preference_id: int) -> Preference | None:
        return self.db.get(Preference, preference_id)

    def get_goal(self, goal_id: int) -> Goal | None:
        return self.db.get(Goal, goal_id)

    def get_event(self, event_id: int) -> Event | None:
        return self.db.get(Event, event_id)

    def update_profile_fact(self, profile_id: int, key: str, value: str) -> None:
        fact = self.get_profile_fact(profile_id)
        if fact is None:
            return
        old_text = f"{fact.key} = {fact.value}"
        fact.key = key
        fact.value = value
        self._update_matching_memories(MemoryType.IDENTITY, old_text, f"{key} = {value}")
        self.db.flush()

    def delete_profile_fact(self, profile_id: int) -> None:
        fact = self.get_profile_fact(profile_id)
        if fact is None:
            return
        fact.is_active = False
        self._deactivate_matching_memories(MemoryType.IDENTITY, f"{fact.key} = {fact.value}")
        self.db.flush()

    def update_preference(
        self,
        preference_id: int,
        category: str,
        value: str,
        importance: int,
    ) -> None:
        preference = self.get_preference(preference_id)
        if preference is None:
            return
        old_text = f"{preference.category} = {preference.value}"
        preference.category = category
        preference.value = value
        preference.importance = importance
        self._update_matching_memories(
            MemoryType.PREFERENCE,
            old_text,
            f"{category} = {value}",
            importance,
        )
        self.db.flush()

    def delete_preference(self, preference_id: int) -> None:
        preference = self.get_preference(preference_id)
        if preference is None:
            return
        preference.is_active = False
        self._deactivate_matching_memories(
            MemoryType.PREFERENCE,
            f"{preference.category} = {preference.value}",
        )
        self.db.flush()

    def update_goal(
        self,
        goal_id: int,
        goal_text: str,
        description: str | None,
        priority: int,
    ) -> None:
        goal = self.get_goal(goal_id)
        if goal is None:
            return
        old_text = goal.goal
        goal.goal = goal_text
        goal.description = description
        goal.priority = priority
        self._update_matching_memories(MemoryType.GOAL_RELATED, old_text, goal_text, priority)
        self.db.flush()

    def delete_goal(self, goal_id: int) -> None:
        goal = self.get_goal(goal_id)
        if goal is None:
            return
        goal.status = GoalStatus.ABANDONED
        self._deactivate_matching_memories(MemoryType.GOAL_RELATED, goal.goal)
        self.db.flush()

    def update_project_memory(
        self,
        project_id: int,
        name: str,
        description: str | None,
        priority: int,
    ) -> None:
        project = self.get_project(project_id)
        if project is None:
            return
        old_text = project.name
        project.name = name
        project.description = description
        project.priority = priority
        self._update_matching_memories(MemoryType.PROJECT_RELATED, old_text, name, priority)
        self.db.flush()

    def delete_project_memory(self, project_id: int) -> None:
        project = self.get_project(project_id)
        if project is None:
            return
        project.status = ProjectStatus.ARCHIVED
        self._deactivate_matching_memories(MemoryType.PROJECT_RELATED, project.name)
        self.db.flush()

    def update_event(
        self,
        event_id: int,
        event_text: str,
        description: str | None,
        event_date: date | None,
        importance: int,
    ) -> None:
        event = self.get_event(event_id)
        if event is None:
            return
        old_text = event.event
        event.event = event_text
        event.description = description
        event.event_date = event_date
        event.importance = importance
        self._update_matching_memories(MemoryType.LIFE_FACT, old_text, event_text, importance)
        self.db.flush()

    def delete_event(self, event_id: int) -> None:
        event = self.get_event(event_id)
        if event is None:
            return
        self._deactivate_matching_memories(MemoryType.LIFE_FACT, event.event)
        self.db.delete(event)
        self.db.flush()

    def update_memory(
        self,
        memory_id: int,
        memory_text: str,
        memory_type: MemoryType,
        importance: int,
    ) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            return
        if not memory.is_active or memory.status != "active":
            return
        memory.memory_text = memory_text
        memory.memory_type = memory_type
        memory.importance = importance
        memory.update_reason = "User edited active memory."
        self._sync_memory_fts(memory)
        self._mark_embedding_stale(memory)
        self._sync_memory_embedding(memory)
        self.record_lifecycle_audit(
            memory,
            "updated",
            previous_status="active",
            new_status="active",
            reason=memory.update_reason,
            source_sentence=memory.source_sentence,
        )
        self.db.flush()

    def delete_memory(self, memory_id: int) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            return
        from app.services.lifecycle import MemoryLifecycleService

        MemoryLifecycleService().delete(self, memory)

    def _update_matching_memories(
        self,
        memory_type: MemoryType,
        old_text: str,
        new_text: str,
        importance: int | None = None,
    ) -> None:
        for memory in self._matching_memories(memory_type, old_text):
            memory.memory_text = new_text
            if importance is not None:
                memory.importance = importance

    def _deactivate_matching_memories(self, memory_type: MemoryType, memory_text: str) -> None:
        for memory in self._matching_memories(memory_type, memory_text):
            self.delete_memory(memory.id)

    def _matching_memories(self, memory_type: MemoryType, memory_text: str) -> list[Memory]:
        stmt = select(Memory).where(
            Memory.memory_type == memory_type,
            Memory.memory_text == memory_text,
            Memory.is_active.is_(True),
            Memory.status == "active",
        )
        return list(self.db.scalars(stmt))

    def inactive_memory_tombstone(
        self,
        memory_type: MemoryType,
        memory_text: str,
        canonical_slot: str | None = None,
    ) -> Memory | None:
        normalized_text = " ".join(memory_text.lower().split())
        from app.services.lifecycle import tombstone_identity

        candidate_identity = tombstone_identity(memory_type, memory_text, canonical_slot)
        stmt = select(Memory).where(
            Memory.memory_type == memory_type,
            Memory.is_active.is_(False),
            Memory.status.in_(["deleted", "archived", "superseded"]),
        )
        for memory in self.db.scalars(stmt):
            memory_identity = tombstone_identity(memory.memory_type, memory.memory_text, memory.canonical_slot)
            if candidate_identity and memory_identity == candidate_identity:
                return memory
            if canonical_slot and memory.canonical_slot == canonical_slot:
                if " ".join(memory.memory_text.lower().split()) == normalized_text:
                    return memory
            elif " ".join(memory.memory_text.lower().split()) == normalized_text:
                return memory
        return None

    def record_lifecycle_audit(
        self,
        memory: Memory,
        action: str,
        previous_status: str | None,
        new_status: str | None,
        reason: str | None = None,
        related_memory_id: int | None = None,
        source_sentence: str | None = None,
    ) -> MemoryLifecycleAudit:
        return self.add(
            MemoryLifecycleAudit(
                memory_id=memory.id,
                action=action,
                previous_status=previous_status,
                new_status=new_status,
                reason=reason,
                related_memory_id=related_memory_id,
                source_sentence=source_sentence,
            ),
        )

    def list_lifecycle_audit(self, memory_id: int) -> list[MemoryLifecycleAudit]:
        stmt = (
            select(MemoryLifecycleAudit)
            .where(MemoryLifecycleAudit.memory_id == memory_id)
            .order_by(MemoryLifecycleAudit.created_at.desc(), MemoryLifecycleAudit.id.desc())
        )
        return list(self.db.scalars(stmt))

    def compress_memories(
        self,
        memories: list[Memory],
        summary_text: str,
        memory_type: MemoryType,
        canonical_slot: str | None = None,
        reason: str = "Compressed related memories into a concise active summary.",
    ) -> Memory:
        from app.services.lifecycle import MemoryLifecycleService

        return MemoryLifecycleService().compress(
            self,
            memories,
            summary_text,
            memory_type,
            canonical_slot=canonical_slot,
            reason=reason,
        )

    def age_memories(self, policy=None, now=None, dry_run: bool = False, max_actions: int | None = None):
        from app.services.lifecycle import MemoryLifecycleService

        return MemoryLifecycleService().age(
            self,
            policy=policy,
            now=now,
            dry_run=dry_run,
            max_actions=max_actions,
        )

    def search_memories(self, query: str, limit: int = 10) -> list[Memory]:
        if self.semantic_enabled:
            hybrid_results = self._search_memories_hybrid(query, limit)
            if hybrid_results is not None:
                return hybrid_results
        fts_results = self._search_memories_fts(query, limit)
        if fts_results is not None:
            return fts_results
        terms = self._query_terms(query)
        filters = self._term_filters_from_terms(terms, [Memory.memory_text])
        relevance_score = self._relevance_score(Memory.memory_text, terms)
        stmt = (
            select(Memory)
            .where(Memory.is_active.is_(True))
            .where(Memory.status == "active")
            .where(or_(*filters))
            .order_by(relevance_score.desc(), Memory.importance.desc(), Memory.updated_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def _search_memories_hybrid(self, query: str, limit: int) -> list[Memory] | None:
        fts_results = self._search_memories_fts_scored(query, max(limit, 20))
        semantic_results = self._search_memories_semantic(query, get_settings().max_semantic_candidates)
        if semantic_results is None and fts_results is None:
            return None
        slot_hint = self._canonical_slot_hint(query)
        scores: dict[int, float] = {}
        memories: dict[int, Memory] = {}
        settings = get_settings()

        for rank, (memory, fts_score) in enumerate(fts_results or [], start=1):
            memories[memory.id] = memory
            scores[memory.id] = scores.get(memory.id, 0.0) + (
                settings.hybrid_fts_weight * fts_score
            ) + (1 / rank)

        for memory, similarity in semantic_results or []:
            memories[memory.id] = memory
            scores[memory.id] = scores.get(memory.id, 0.0) + (
                settings.hybrid_semantic_weight * similarity
            )

        for memory_id, memory in list(memories.items()):
            if not memory.is_active or memory.status != "active":
                scores.pop(memory_id, None)
                memories.pop(memory_id, None)
                continue
            if slot_hint and self._memory_slot(memory) == slot_hint:
                scores[memory_id] += settings.hybrid_slot_weight
            elif slot_hint and self._strong_slot(slot_hint):
                scores[memory_id] -= settings.hybrid_slot_weight
            scores[memory_id] += memory.importance * settings.hybrid_importance_weight
            scores[memory_id] += self._recency_boost(memory.updated_at)

        ordered_ids = [
            memory_id
            for memory_id, _score in sorted(
                scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ][:limit]
        return [memories[memory_id] for memory_id in ordered_ids]

    def _search_memories_fts(self, query: str, limit: int) -> list[Memory] | None:
        scored = self._search_memories_fts_scored(query, limit)
        if scored is None:
            return None
        return [memory for memory, _score in scored]

    def _search_memories_fts_scored(self, query: str, limit: int) -> list[tuple[Memory, float]] | None:
        if not self._ensure_memory_fts():
            return None
        match_query = self._fts_match_query(query)
        if not match_query:
            return []
        try:
            rows = self.db.execute(
                text(
                    """
                    SELECT m.id, bm25(memory_fts) AS bm25_score
                    FROM memory_fts f
                    JOIN memories m ON m.id = f.rowid
                    WHERE m.is_active = 1
                      AND m.status = 'active'
                      AND memory_fts MATCH :query
                    ORDER BY bm25(memory_fts), m.importance DESC, m.updated_at DESC
                    LIMIT :limit
                    """,
                ),
                {"query": match_query, "limit": limit},
            ).all()
        except Exception:
            self._memory_fts_available = False
            return None
        ids = [row[0] for row in rows]
        if not ids:
            return []
        memories_by_id = {
            memory.id: memory
            for memory in self.db.scalars(select(Memory).where(Memory.id.in_(ids)))
        }
        scored: list[tuple[Memory, float]] = []
        for memory_id, bm25_score in rows:
            memory = memories_by_id.get(memory_id)
            if memory is None:
                continue
            scored.append((memory, 1.0 / (1.0 + abs(float(bm25_score or 0.0)))))
        return scored

    def _search_memories_semantic(self, query: str, limit: int) -> list[tuple[Memory, float]] | None:
        if self.embedding_service is None:
            return None
        try:
            query_vector = self.embedding_service.provider.embed(query)
        except Exception:
            return []
        settings = get_settings()
        stmt = (
            select(Memory, MemoryEmbedding)
            .join(MemoryEmbedding, MemoryEmbedding.memory_id == Memory.id)
            .where(Memory.is_active.is_(True))
            .where(Memory.status == "active")
            .where(MemoryEmbedding.status == "ready")
            .where(MemoryEmbedding.model == self.embedding_service.provider.model_name)
            .where(MemoryEmbedding.provider == self.embedding_service.provider.provider_name)
        )
        scored: list[tuple[Memory, float]] = []
        for memory, embedding in self.db.execute(stmt):
            similarity = cosine_similarity(query_vector, decode_vector(embedding.vector_json))
            if similarity >= settings.semantic_similarity_threshold:
                scored.append((memory, similarity))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def _ensure_memory_fts(self) -> bool:
        if self._memory_fts_available is False:
            return False
        try:
            self.db.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                    USING fts5(memory_text, canonical_slot)
                    """,
                ),
            )
            count = self.db.execute(text("SELECT count(*) FROM memory_fts")).scalar_one()
            if count == 0:
                for memory in self.db.scalars(select(Memory)):
                    self._upsert_memory_fts(memory)
            self._memory_fts_available = True
            return True
        except Exception:
            self._memory_fts_available = False
            return False

    def _sync_memory_fts(self, memory: Memory) -> None:
        if not self._ensure_memory_fts():
            return
        self._upsert_memory_fts(memory)

    def _upsert_memory_fts(self, memory: Memory) -> None:
        self.db.execute(
            text(
                """
                INSERT OR REPLACE INTO memory_fts(rowid, memory_text, canonical_slot)
                VALUES (:id, :memory_text, :canonical_slot)
                """,
            ),
            {
                "id": memory.id,
                "memory_text": memory.memory_text,
                "canonical_slot": memory.canonical_slot or "",
            },
        )

    def _delete_memory_fts(self, memory_id: int) -> None:
        if not self._ensure_memory_fts():
            return
        self.db.execute(text("DELETE FROM memory_fts WHERE rowid = :id"), {"id": memory_id})

    def _sync_memory_embedding(self, memory: Memory) -> None:
        if not self.auto_embed or self.embedding_service is None:
            return
        existing = self.db.get(MemoryEmbedding, memory.id)
        if not self.embedding_service.needs_embedding(memory, existing):
            return
        result = self.embedding_service.upsert_embedding(memory, existing)
        if existing is None:
            self.db.add(result.embedding)
        self.db.flush()

    def _mark_embedding_stale(self, memory: Memory) -> None:
        embedding = self.db.get(MemoryEmbedding, memory.id)
        if embedding is None:
            return
        if embedding.status == "ready":
            embedding.status = "stale"
            embedding.error = None
            self.db.flush()

    def upsert_memory_embedding(self, memory: Memory, dry_run: bool = False) -> str:
        if self.embedding_service is None:
            return "skipped"
        existing = self.db.get(MemoryEmbedding, memory.id)
        if not self.embedding_service.needs_embedding(memory, existing):
            return "already_embedded"
        result = self.embedding_service.upsert_embedding(memory, existing, dry_run=dry_run)
        if not dry_run and existing is None:
            self.db.add(result.embedding)
            self.db.flush()
        return result.status

    def list_embedding_backfill_targets(self, active_only: bool = True, limit: int | None = None) -> list[Memory]:
        stmt = select(Memory).order_by(Memory.id)
        if active_only:
            stmt = stmt.where(Memory.is_active.is_(True), Memory.status == "active")
        if limit is not None:
            stmt = stmt.limit(limit)
        memories = list(self.db.scalars(stmt))
        if self.embedding_service is None:
            return memories
        return [
            memory
            for memory in memories
            if self.embedding_service.needs_embedding(memory, self.db.get(MemoryEmbedding, memory.id))
        ]

    def count_memory_embeddings(self) -> dict[str, int]:
        total = self.db.execute(
            text("SELECT count(*) FROM memories WHERE is_active = 1 AND status = 'active'"),
        ).scalar_one()
        ready = self.db.execute(
            text("SELECT count(*) FROM memory_embeddings WHERE status = 'ready'"),
        ).scalar_one()
        failed = self.db.execute(
            text("SELECT count(*) FROM memory_embeddings WHERE status = 'failed'"),
        ).scalar_one()
        stale = self.db.execute(
            text("SELECT count(*) FROM memory_embeddings WHERE status = 'stale'"),
        ).scalar_one()
        return {
            "total_memories": int(total),
            "ready": int(ready),
            "failed": int(failed),
            "stale": int(stale),
            "missing": max(int(total) - int(ready) - int(failed) - int(stale), 0),
        }

    def _fts_match_query(self, query: str) -> str:
        terms = [
            term
            for term in self._query_terms(query)
            if re.match(r"^[a-z0-9]+$", term)
        ]
        if not terms:
            return ""
        return " OR ".join(dict.fromkeys(terms))

    def _canonical_slot_hint(self, query: str) -> str | None:
        lowered = query.lower()
        if re.search(r"\b(laptop|computer|machine|system|specs|gpu|graphics|ram|ssd|processor|cpu|llm|llms)\b", lowered):
            return "current_hardware"
        if re.search(r"\b(editor|ide|write code|code in|work in)\b", lowered):
            return "preference:editor"
        if re.search(r"\b(cp|competitive programming)\b", lowered):
            return "preference:competitive_programming_language"
        if re.search(r"\b(project|building|assistant|focus on|improve)\b", lowered):
            return "project_related"
        if re.search(r"\b(career|roadmap|skills|flutter|frontend|goal|target|prioritize)\b", lowered):
            return "goal_related"
        return None

    def _memory_slot(self, memory: Memory) -> str:
        if memory.canonical_slot:
            return memory.canonical_slot
        return memory.memory_type.value if hasattr(memory.memory_type, "value") else str(memory.memory_type)

    def _strong_slot(self, slot: str) -> bool:
        return slot in {
            "current_hardware",
            "preference:editor",
            "preference:competitive_programming_language",
        }

    def _recency_boost(self, updated_at: datetime | None) -> float:
        if updated_at is None:
            return 0.0
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        age_days = max((datetime.now(UTC) - updated_at).days, 0)
        return max(0.0, 0.25 - (age_days / 3650))

    def search_profile(self, query: str, limit: int = 10) -> list[ProfileFact]:
        terms = self._query_terms(query)
        filters = self._term_filters_from_terms(terms, [ProfileFact.key, ProfileFact.value])
        relevance_score = self._relevance_score(ProfileFact.key, terms) + self._relevance_score(
            ProfileFact.value,
            terms,
        )
        stmt = (
            select(ProfileFact)
            .where(ProfileFact.is_active.is_(True))
            .where(or_(*filters))
            .order_by(relevance_score.desc(), ProfileFact.updated_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def search_preferences(self, query: str, limit: int = 10) -> list[Preference]:
        terms = self._query_terms(query)
        filters = self._term_filters_from_terms(terms, [Preference.category, Preference.value])
        relevance_score = self._relevance_score(
            Preference.category,
            terms,
        ) + self._relevance_score(Preference.value, terms)
        stmt = (
            select(Preference)
            .where(Preference.is_active.is_(True))
            .where(or_(*filters))
            .order_by(relevance_score.desc(), Preference.importance.desc(), Preference.updated_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def search_events(self, query: str, limit: int = 10) -> list[Event]:
        filters = self._term_filters(query, [Event.event, Event.description])
        stmt = (
            select(Event)
            .where(or_(*filters))
            .order_by(Event.event_date.desc().nullslast(), Event.id.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt))

    def search_goals(self, query: str, limit: int = 10) -> list[Goal]:
        return self._keyword_query(
            select(Goal),
            query,
            [Goal.goal, Goal.description],
            limit,
            Goal.priority.desc(),
        )

    def search_projects(self, query: str, limit: int = 10) -> list[Project]:
        return self._keyword_query(
            select(Project),
            query,
            [Project.name, Project.description],
            limit,
            Project.priority.desc(),
        )

    def active_profile_by_key(self, key: str) -> list[ProfileFact]:
        stmt = select(ProfileFact).where(ProfileFact.key == key, ProfileFact.is_active.is_(True))
        return list(self.db.scalars(stmt))

    def active_preferences_by_category(self, category: str) -> list[Preference]:
        stmt = select(Preference).where(
            Preference.category == category,
            Preference.is_active.is_(True),
        )
        return list(self.db.scalars(stmt))

    def active_memories_by_type(self, memory_type: MemoryType) -> list[Memory]:
        stmt = select(Memory).where(
            Memory.memory_type == memory_type,
            Memory.is_active.is_(True),
            Memory.status == "active",
        )
        return list(self.db.scalars(stmt))

    def events_in_range(self, start: date, end: date) -> list[Event]:
        stmt = (
            select(Event)
            .where(Event.event_date >= start, Event.event_date <= end)
            .order_by(Event.event_date)
        )
        return list(self.db.scalars(stmt))

    def _keyword_query(
        self,
        stmt: Select,
        query: str,
        columns: list,
        limit: int,
        *order_by,
    ):
        filters = self._term_filters(query, columns)
        stmt = stmt.where(or_(*filters)).order_by(*order_by).limit(limit)
        return list(self.db.scalars(stmt))

    def _term_filters(self, query: str, columns: list):
        return self._term_filters_from_terms(self._query_terms(query), columns)

    def _term_filters_from_terms(self, terms: list[str], columns: list):
        if not terms:
            terms = [""]
        return [column.ilike(f"%{term}%") for term in terms for column in columns]

    def _query_terms(self, query: str) -> list[str]:
        terms = re.findall(r"[a-z0-9+#.]+", query.lower())
        useful_terms = [term for term in terms if len(term) > 1 and term not in QUERY_STOPWORDS]
        useful_terms = self._expand_query_terms(useful_terms)
        if useful_terms:
            return useful_terms
        return [term for term in terms if term]

    def query_terms(self, query: str) -> list[str]:
        return self._query_terms(query)

    def _expand_query_terms(self, terms: list[str]) -> list[str]:
        expansions = {
            "laptop": ["hardware", "computer"],
            "computer": ["hardware", "laptop"],
            "machine": ["hardware", "computer", "laptop"],
            "system": ["hardware", "machine", "computer"],
            "gpu": ["graphics", "hardware"],
            "graphics": ["gpu", "hardware"],
            "pc": ["hardware", "computer"],
            "specs": ["hardware", "ram", "processor"],
            "llm": ["hardware", "ram", "gpu"],
            "llms": ["hardware", "ram", "gpu"],
            "dedicated": ["gpu", "graphics"],
            "cp": ["competitive", "programming"],
            "competitive": ["programming"],
            "editor": ["ide"],
            "ide": ["editor"],
            "code": ["editor", "ide"],
            "roadmap": ["goal", "career"],
            "skills": ["goal", "career"],
            "career": ["goal", "long", "term"],
            "direction": ["goal", "career"],
            "target": ["goal", "career"],
            "flutter": ["frontend", "mobile"],
        }
        expanded: list[str] = []
        for term in terms:
            expanded.append(term)
            expanded.extend(expansions.get(term, []))
        return list(dict.fromkeys(expanded))

    def _relevance_score(self, column, terms: list[str]):
        if not terms:
            return case((column.ilike("%%"), 1), else_=0)
        score = None
        for term in terms:
            term_score = case((column.ilike(f"%{term}%"), 1), else_=0)
            score = term_score if score is None else score + term_score
        return score

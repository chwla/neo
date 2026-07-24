from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

from sqlalchemy import Select, case, exists, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    Activity,
    Chat,
    ChatMessage,
    Education,
    Event,
    Goal,
    GoalStatus,
    Memory,
    MemoryCandidate,
    MemoryEmbedding,
    MemoryLifecycleAudit,
    MemorySource,
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
from app.services.memory_fingerprints import memory_fingerprint, source_fingerprint

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
            semantic_enabled
            if semantic_enabled is not None
            else settings.semantic_retrieval_enabled
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
            stmt = stmt.where(exists().where(ChatMessage.chat_id == Chat.id))
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
        response_kind: str | None = None,
        provider_name: str | None = None,
        model_name: str | None = None,
        route_name: str | None = None,
        finish_reason: str | None = None,
        trace_id: str | None = None,
        metadata: dict | None = None,
        generation_id: str | None = None,
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
                response_kind=response_kind,
                provider_name=provider_name,
                model_name=model_name,
                route_name=route_name,
                finish_reason=finish_reason,
                trace_id=trace_id,
                metadata_json=json.dumps(metadata, sort_keys=True) if metadata else None,
                generation_id=generation_id,
            )
        )
        chat = self.get_chat(chat_id)
        if chat is not None:
            chat.updated_at = message.created_at
        return message

    def upsert_generation_assistant(
        self,
        chat_id: int,
        generation_id: str,
        content: str,
        **metadata,
    ) -> ChatMessage:
        """Persist one assistant message for a durable generation.

        The unique generation key closes the crash window between saving an
        assistant message and marking its generation complete. A recovered
        worker updates that same row instead of appending a duplicate.
        """

        existing = self.db.scalar(
            select(ChatMessage).where(ChatMessage.generation_id == generation_id)
        )
        if existing is None:
            try:
                with self.db.begin_nested():
                    existing = self.add_chat_message(
                        chat_id,
                        "assistant",
                        content,
                        generation_id=generation_id,
                        **metadata,
                    )
            except IntegrityError:
                existing = self.db.scalar(
                    select(ChatMessage).where(ChatMessage.generation_id == generation_id)
                )
        if existing is None:
            raise RuntimeError("The generation assistant message could not be persisted.")
        if existing.chat_id != chat_id or existing.role != "assistant":
            raise RuntimeError("The generation correlation belongs to a different message.")

        existing.content = content
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "duration_ms",
            "thinking",
            "response_kind",
            "provider_name",
            "model_name",
            "route_name",
            "finish_reason",
            "trace_id",
        ):
            if field in metadata:
                setattr(existing, field, metadata[field])
        if "metadata" in metadata:
            value = metadata["metadata"]
            existing.metadata_json = json.dumps(value, sort_keys=True) if value else None
        chat = self.get_chat(chat_id)
        if chat is not None:
            chat.updated_at = datetime.now(UTC)
        self.db.flush()
        return existing

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

    def list_education(self, active_only: bool = True) -> list[Education]:
        stmt = select(Education).order_by(Education.updated_at.desc(), Education.id.desc())
        if active_only:
            stmt = stmt.where(Education.is_active.is_(True))
        return list(self.db.scalars(stmt))

    def get_education(self, education_id: int) -> Education | None:
        return self.db.get(Education, education_id)

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

    def list_activities(
        self,
        active_only: bool = True,
        now: datetime | None = None,
    ) -> list[Activity]:
        now = now or datetime.now(UTC)
        if active_only:
            self.archive_expired_activities(now)
        stmt = select(Activity).order_by(Activity.updated_at.desc(), Activity.id.desc())
        if active_only:
            stmt = stmt.where(Activity.is_active.is_(True), Activity.expires_at > now)
        return list(self.db.scalars(stmt))

    def get_activity(self, activity_id: int) -> Activity | None:
        return self.db.get(Activity, activity_id)

    def archive_expired_activities(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        expired = list(
            self.db.scalars(
                select(Activity).where(
                    Activity.is_active.is_(True),
                    Activity.expires_at <= now,
                ),
            ),
        )
        for activity in expired:
            activity.is_active = False
            activity.archived_at = now
            for memory in self.active_memories_by_type(MemoryType.ACTIVITY):
                if memory.fingerprint == activity.fingerprint:
                    from app.services.lifecycle import MemoryLifecycleService

                    MemoryLifecycleService().archive(
                        self,
                        memory,
                        "Current activity expired after 30 days.",
                    )
        if expired:
            self.db.flush()
        return len(expired)

    def list_memories(self, active_only: bool = True, limit: int = 50) -> list[Memory]:
        stmt = (
            select(Memory).order_by(Memory.importance.desc(), Memory.updated_at.desc()).limit(limit)
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

    def active_memory_by_fingerprint(
        self,
        memory_type: MemoryType,
        fingerprint: str,
    ) -> Memory | None:
        return self.db.scalar(
            select(Memory)
            .where(
                Memory.memory_type == memory_type,
                Memory.fingerprint == fingerprint,
                Memory.is_active.is_(True),
                Memory.status == "active",
            )
            .order_by(Memory.updated_at.desc(), Memory.id.desc()),
        )

    def attach_memory_source(
        self,
        memory: Memory,
        source_sentence: str,
        source_conversation_id: int | None = None,
        source_message_id: int | None = None,
    ) -> MemorySource:
        fingerprint = source_fingerprint(
            source_message_id,
            source_conversation_id,
            source_sentence,
        )
        existing = self.db.scalar(
            select(MemorySource).where(
                MemorySource.memory_id == memory.id,
                MemorySource.source_fingerprint == fingerprint,
            ),
        )
        if existing is not None:
            existing.is_active = True
            existing.detachment_reason = None
            existing.source_sentence = source_sentence
            existing.source_conversation_id = source_conversation_id
            existing.source_message_id = source_message_id
            self.db.flush()
            return existing
        return self.add(
            MemorySource(
                memory_id=memory.id,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                source_sentence=source_sentence,
                source_fingerprint=fingerprint,
                is_active=True,
                detachment_reason=None,
            ),
        )

    def list_memory_sources(
        self,
        memory_id: int,
        active_only: bool = True,
    ) -> list[MemorySource]:
        stmt = select(MemorySource).where(MemorySource.memory_id == memory_id)
        if active_only:
            stmt = stmt.where(MemorySource.is_active.is_(True))
        return list(self.db.scalars(stmt.order_by(MemorySource.id)))

    def detach_memory_sources_for_message(
        self,
        source_message_id: int,
        *,
        reason: str = "deletion",
    ) -> list[int]:
        """Remove one message's support without conflating edits and deletions.

        A replacement is a temporary, source-scoped transition used while the same
        message is re-extracted. Deletion is intentional removal and creates a
        durable tombstone when the message was the final source.
        """

        if reason not in {"replacement", "deletion"}:
            raise ValueError("Memory source detachment reason must be replacement or deletion.")

        sources = list(
            self.db.scalars(
                select(MemorySource).where(
                    MemorySource.source_message_id == source_message_id,
                ),
            ),
        )
        affected_ids = sorted({source.memory_id for source in sources})
        for source in sources:
            if source.is_active or reason == "deletion":
                source.is_active = False
                source.detachment_reason = reason
        self.db.flush()
        for memory_id in affected_ids:
            if self.list_memory_sources(memory_id):
                continue
            memory = self.get_memory(memory_id)
            if memory is None:
                continue
            if memory.is_active:
                self._deactivate_typed_record_for_memory(memory)
            from app.services.lifecycle import MemoryLifecycleService

            lifecycle = MemoryLifecycleService()
            if reason == "deletion":
                lifecycle.delete(
                    self,
                    memory,
                    "Deleted because its final user-message source was removed.",
                )
            elif memory.is_active:
                lifecycle.archive(
                    self,
                    memory,
                    "Archived while its user-message source is being replaced.",
                )
        self.db.flush()
        return affected_ids

    def source_was_detached_for_replacement(
        self,
        memory_id: int,
        source_message_id: int | None,
    ) -> bool:
        if source_message_id is None:
            return False
        return (
            self.db.scalar(
                select(MemorySource.id).where(
                    MemorySource.memory_id == memory_id,
                    MemorySource.source_message_id == source_message_id,
                    MemorySource.is_active.is_(False),
                    MemorySource.detachment_reason == "replacement",
                ),
            )
            is not None
        )

    def reactivate_typed_record_for_memory(self, memory: Memory) -> None:
        """Reactivate the typed projection before re-accepting the same source fact."""

        if memory.memory_type == MemoryType.IDENTITY and "=" in memory.memory_text:
            key, value = (part.strip() for part in memory.memory_text.split("=", 1))
            fact = self.db.scalar(
                select(ProfileFact)
                .where(ProfileFact.key == key, ProfileFact.value == value)
                .order_by(ProfileFact.id.desc()),
            )
            if fact is not None:
                fact.is_active = True
        elif memory.memory_type == MemoryType.PREFERENCE:
            preference = self.db.scalar(
                select(Preference)
                .where(Preference.fingerprint == memory.fingerprint)
                .order_by(Preference.id.desc()),
            )
            if preference is not None:
                preference.is_active = True
        elif memory.memory_type == MemoryType.EDUCATION:
            education = self.db.scalar(
                select(Education)
                .where(Education.fingerprint == memory.fingerprint)
                .order_by(Education.id.desc()),
            )
            if education is not None:
                education.is_active = True
        elif memory.memory_type == MemoryType.GOAL_RELATED:
            goal = self.db.scalar(
                select(Goal).where(Goal.fingerprint == memory.fingerprint).order_by(Goal.id.desc()),
            )
            if goal is not None:
                goal.status = GoalStatus.ACTIVE
        elif memory.memory_type == MemoryType.ACTIVITY:
            activity = self.db.scalar(
                select(Activity)
                .where(Activity.fingerprint == memory.fingerprint)
                .order_by(Activity.id.desc()),
            )
            if activity is not None:
                activity.is_active = True
                activity.archived_at = None
        self.db.flush()

    def _deactivate_typed_record_for_memory(self, memory: Memory) -> None:
        if memory.memory_type == MemoryType.IDENTITY and "=" in memory.memory_text:
            key, value = (part.strip() for part in memory.memory_text.split("=", 1))
            for fact in self.active_profile_by_key(key):
                if fact.value == value:
                    fact.is_active = False
        elif memory.memory_type == MemoryType.PREFERENCE:
            for preference in self.list_preferences():
                if preference.fingerprint == memory.fingerprint:
                    preference.is_active = False
        elif memory.memory_type == MemoryType.EDUCATION:
            for education in self.list_education():
                if education.fingerprint == memory.fingerprint:
                    education.is_active = False
        elif memory.memory_type == MemoryType.GOAL_RELATED:
            for goal in self.list_goals(GoalStatus.ACTIVE):
                if goal.fingerprint == memory.fingerprint:
                    goal.status = GoalStatus.ABANDONED
        elif memory.memory_type == MemoryType.ACTIVITY:
            for activity in self.db.scalars(
                select(Activity).where(Activity.is_active.is_(True)),
            ):
                if activity.fingerprint == memory.fingerprint:
                    activity.is_active = False
                    activity.archived_at = datetime.now(UTC)
        elif memory.memory_type == MemoryType.LIFE_FACT:
            for event in self.list_events(limit=100000):
                if event.fingerprint == memory.fingerprint:
                    self.db.delete(event)

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

    def retire_invalid_profile_facts(self) -> int:
        """Retire invalid identity rows and restore a valid fact they wrongly superseded."""

        from app.services.identity_facts import is_durable_identity_fact
        from app.services.lifecycle import MemoryLifecycleService

        retired = 0
        for fact in self.list_profile(active_only=True):
            if is_durable_identity_fact(str(fact.key), str(fact.value)):
                continue
            invalid_text = f"{fact.key} = {fact.value}"
            invalid_memories = self._matching_memories(MemoryType.IDENTITY, invalid_text)
            predecessors = [
                self.db.get(Memory, memory.supersedes_id)
                for memory in invalid_memories
                if memory.supersedes_id is not None
            ]
            fact.is_active = False
            self._deactivate_matching_memories(MemoryType.IDENTITY, invalid_text)
            retired += 1
            for predecessor in predecessors:
                if predecessor is None or predecessor.memory_type != MemoryType.IDENTITY:
                    continue
                prefix, separator, value = predecessor.memory_text.partition("=")
                key = prefix.strip()
                value = value.strip()
                if not separator or key != fact.key or not is_durable_identity_fact(key, value):
                    continue
                previous_fact = self.db.scalar(
                    select(ProfileFact)
                    .where(
                        ProfileFact.key == key,
                        ProfileFact.value == value,
                        ProfileFact.is_active.is_(False),
                    )
                    .order_by(ProfileFact.updated_at.desc(), ProfileFact.id.desc())
                )
                if previous_fact is None:
                    continue
                previous_fact.is_active = True
                MemoryLifecycleService().restore(
                    self,
                    predecessor,
                    "Restored after retiring an invalid identity value that superseded it.",
                    explicit_restore=True,
                )
                break
        if retired:
            self.db.flush()
        return retired

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

    def create_manual_memory(
        self,
        memory_text: str,
        memory_type: MemoryType,
        importance: int,
    ) -> tuple[Memory, bool]:
        """Add a durable user-authored memory without creating a fake chat source.

        A repeated submission returns the existing active memory. This keeps the
        manual form idempotent while preserving the entry's lifecycle and index.
        """
        fingerprint = memory_fingerprint("manual", memory_type.value, memory_text)
        existing = self.active_memory_by_fingerprint(memory_type, fingerprint)
        if existing is not None:
            if importance > existing.importance:
                existing.importance = importance
                existing.update_reason = "User raised the importance of a manual memory."
                self._sync_memory_fts(existing)
                self._mark_embedding_stale(existing)
                self._sync_memory_embedding(existing)
                self.db.flush()
            return existing, False
        memory = self.add(
            Memory(
                memory_text=memory_text,
                memory_type=memory_type,
                importance=importance,
                confidence=1.0,
                source="manual",
                source_sentence=memory_text,
                fingerprint=fingerprint,
                update_reason="User added this memory manually.",
                status="active",
                is_active=True,
            )
        )
        return memory, True

    def update_education(
        self,
        education_id: int,
        institution: str,
        degree: str | None,
        field_of_study: str | None,
        graduation_date: date | None,
        description: str | None,
    ) -> None:
        education = self.get_education(education_id)
        if education is None or not education.is_active:
            return
        new_fingerprint = memory_fingerprint(
            "education", institution, degree, field_of_study, graduation_date
        )
        duplicate = self.db.scalar(
            select(Education).where(
                Education.id != education_id,
                Education.is_active.is_(True),
                Education.fingerprint == new_fingerprint,
            )
        )
        if duplicate is not None:
            raise ValueError("An identical active education record already exists")
        old_fingerprint = education.fingerprint
        education.institution = institution
        education.degree = degree
        education.field_of_study = field_of_study
        education.graduation_date = graduation_date
        education.description = description or self._education_summary(education)
        education.fingerprint = new_fingerprint
        for memory in self._typed_memories_by_fingerprint(MemoryType.EDUCATION, old_fingerprint):
            memory.memory_text = education.description
            memory.fingerprint = new_fingerprint
            memory.canonical_slot = f"education:{self._slot_key(institution)}"
            memory.update_reason = "User edited education memory."
            self._sync_memory_fts(memory)
            self._mark_embedding_stale(memory)
            self._sync_memory_embedding(memory)
        self.db.flush()

    def delete_education(self, education_id: int) -> None:
        education = self.get_education(education_id)
        if education is None or not education.is_active:
            return
        memories = self._typed_memories_by_fingerprint(MemoryType.EDUCATION, education.fingerprint)
        if memories:
            for memory in memories:
                self.delete_memory(memory.id)
        else:
            education.is_active = False
        self.db.flush()

    def update_activity(
        self,
        activity_id: int,
        category: str,
        activity_text: str,
        description: str | None,
        started_at: datetime,
        expires_at: datetime,
    ) -> None:
        activity = self.get_activity(activity_id)
        if activity is None or not activity.is_active:
            return
        new_fingerprint = memory_fingerprint("activity", category, activity_text)
        duplicate = self.db.scalar(
            select(Activity).where(
                Activity.id != activity_id,
                Activity.is_active.is_(True),
                Activity.fingerprint == new_fingerprint,
            )
        )
        if duplicate is not None:
            raise ValueError("An identical active activity already exists")
        old_fingerprint = activity.fingerprint
        activity.category = category
        activity.activity = activity_text
        activity.description = description
        activity.started_at = started_at
        activity.expires_at = expires_at
        activity.fingerprint = new_fingerprint
        for memory in self._typed_memories_by_fingerprint(MemoryType.ACTIVITY, old_fingerprint):
            memory.memory_text = activity_text
            memory.fingerprint = new_fingerprint
            memory.canonical_slot = f"activity:{self._slot_key(category)}"
            memory.expires_at = expires_at
            memory.update_reason = "User edited current activity memory."
            self._sync_memory_fts(memory)
            self._mark_embedding_stale(memory)
            self._sync_memory_embedding(memory)
        self.db.flush()

    def delete_activity(self, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity is None or not activity.is_active:
            return
        memories = self._typed_memories_by_fingerprint(MemoryType.ACTIVITY, activity.fingerprint)
        if memories:
            for memory in memories:
                self.delete_memory(memory.id)
        else:
            activity.is_active = False
            activity.archived_at = datetime.now(UTC)
        self.db.flush()

    def delete_memory(self, memory_id: int) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            return
        from app.services.lifecycle import MemoryLifecycleService

        if memory.is_active:
            self._deactivate_typed_record_for_memory(memory)
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

    def _typed_memories_by_fingerprint(
        self,
        memory_type: MemoryType,
        fingerprint: str,
    ) -> list[Memory]:
        return list(
            self.db.scalars(
                select(Memory).where(
                    Memory.memory_type == memory_type,
                    Memory.fingerprint == fingerprint,
                    Memory.is_active.is_(True),
                    Memory.status == "active",
                ),
            )
        )

    @staticmethod
    def _slot_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold()) or "general"

    @staticmethod
    def _education_summary(education: Education) -> str:
        qualification = education.degree or "Education"
        if education.field_of_study:
            qualification = f"{qualification} in {education.field_of_study}"
        return f"{qualification} at {education.institution}"

    def inactive_memory_tombstone(
        self,
        memory_type: MemoryType,
        memory_text: str,
        canonical_slot: str | None = None,
        replacement_source_message_id: int | None = None,
    ) -> Memory | None:
        normalized_text = " ".join(memory_text.lower().split())
        from app.services.lifecycle import tombstone_identity

        candidate_identity = tombstone_identity(memory_type, memory_text, canonical_slot)
        stmt = select(Memory).where(
            Memory.memory_type == memory_type,
            Memory.is_active.is_(False),
            Memory.status.in_(["deleted", "archived", "superseded"]),
        )
        matches: list[Memory] = []
        for memory in self.db.scalars(stmt):
            memory_identity = tombstone_identity(
                memory.memory_type, memory.memory_text, memory.canonical_slot
            )
            if candidate_identity and memory_identity == candidate_identity:
                matches.append(memory)
                continue
            if canonical_slot and memory.canonical_slot == canonical_slot:
                if " ".join(memory.memory_text.lower().split()) == normalized_text:
                    matches.append(memory)
            elif " ".join(memory.memory_text.lower().split()) == normalized_text:
                matches.append(memory)
        if not matches:
            return None

        def priority(memory: Memory) -> tuple[int, int]:
            if memory.status == "deleted":
                return (0, -memory.id)
            if memory.status == "superseded":
                return (1, -memory.id)
            if self.source_was_detached_for_replacement(
                memory.id,
                replacement_source_message_id,
            ):
                return (2, -memory.id)
            return (3, -memory.id)

        return min(matches, key=priority)

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

    def age_memories(
        self, policy=None, now=None, dry_run: bool = False, max_actions: int | None = None
    ):
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
        semantic_results = self._search_memories_semantic(
            query, get_settings().max_semantic_candidates
        )
        if semantic_results is None and fts_results is None:
            return None
        slot_hint = self._canonical_slot_hint(query)
        scores: dict[int, float] = {}
        memories: dict[int, Memory] = {}
        settings = get_settings()

        for rank, (memory, fts_score) in enumerate(fts_results or [], start=1):
            memories[memory.id] = memory
            scores[memory.id] = (
                scores.get(memory.id, 0.0) + (settings.hybrid_fts_weight * fts_score) + (1 / rank)
            )

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

    def _search_memories_fts_scored(
        self, query: str, limit: int
    ) -> list[tuple[Memory, float]] | None:
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

    def _search_memories_semantic(
        self, query: str, limit: int
    ) -> list[tuple[Memory, float]] | None:
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

    def list_embedding_backfill_targets(
        self, active_only: bool = True, limit: int | None = None
    ) -> list[Memory]:
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
            if self.embedding_service.needs_embedding(
                memory, self.db.get(MemoryEmbedding, memory.id)
            )
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
        terms = [term for term in self._query_terms(query) if re.match(r"^[a-z0-9]+$", term)]
        if not terms:
            return ""
        return " OR ".join(dict.fromkeys(terms))

    def _canonical_slot_hint(self, query: str) -> str | None:
        lowered = query.lower()
        if re.search(
            r"\b(laptop|computer|machine|system|specs|gpu|graphics|ram|ssd|processor|cpu|llm|llms)\b",
            lowered,
        ):
            return "current_hardware"
        if re.search(r"\b(editor|ide|write code|code in|work in)\b", lowered):
            return "preference:editor"
        if re.search(r"\b(cp|competitive programming)\b", lowered):
            return "preference:competitive_programming_language"
        if re.search(r"\b(project|building|assistant|focus on|improve)\b", lowered):
            return "project_related"
        if re.search(
            r"\b(career|roadmap|skills|flutter|frontend|goal|target|prioritize)\b", lowered
        ):
            return "goal_related"
        return None

    def _memory_slot(self, memory: Memory) -> str:
        if memory.canonical_slot:
            return memory.canonical_slot
        return (
            memory.memory_type.value
            if hasattr(memory.memory_type, "value")
            else str(memory.memory_type)
        )

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
            .order_by(
                relevance_score.desc(), Preference.importance.desc(), Preference.updated_at.desc()
            )
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

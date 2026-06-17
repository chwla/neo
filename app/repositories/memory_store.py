from __future__ import annotations

from datetime import date

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Chat,
    ChatMessage,
    Event,
    Goal,
    GoalStatus,
    Memory,
    MemoryCandidate,
    Preference,
    ProfileFact,
    Project,
    ProjectStatus,
)
from app.models.enums import CandidateStatus, MemoryType


class MemoryStore:
    """Repository facade over the local SQLite memory tables."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, entity):
        self.db.add(entity)
        self.db.flush()
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
            stmt = stmt.where(Memory.is_active.is_(True))
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
        memory.memory_text = memory_text
        memory.memory_type = memory_type
        memory.importance = importance
        self.db.flush()

    def delete_memory(self, memory_id: int) -> None:
        memory = self.get_memory(memory_id)
        if memory is None:
            return
        memory.is_active = False
        self.db.flush()

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
            memory.is_active = False

    def _matching_memories(self, memory_type: MemoryType, memory_text: str) -> list[Memory]:
        stmt = select(Memory).where(
            Memory.memory_type == memory_type,
            Memory.memory_text == memory_text,
            Memory.is_active.is_(True),
        )
        return list(self.db.scalars(stmt))

    def search_memories(self, query: str, limit: int = 10) -> list[Memory]:
        filters = self._term_filters(query, [Memory.memory_text])
        stmt = (
            select(Memory)
            .where(Memory.is_active.is_(True))
            .where(or_(*filters))
            .order_by(Memory.importance.desc(), Memory.updated_at.desc())
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
        stmt = select(Memory).where(Memory.memory_type == memory_type, Memory.is_active.is_(True))
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
        terms = [term for term in query.split() if term]
        if not terms:
            terms = [query]
        return [column.ilike(f"%{term}%") for term in terms for column in columns]

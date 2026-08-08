from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.chat import Chat, ChatMessage
from app.models.enums import ProjectStatus
from app.models.project import Project


class AppStore:
    """Persistence for chats and chat projects only.

    Personal memory has its own owner-bound repository and never passes through
    this application store.
    """

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
        statement = (
            select(Chat)
            .where(Chat.archived.is_(False))
            .order_by(Chat.updated_at.desc(), Chat.id.desc())
            .limit(limit)
        )
        if unprojected_only:
            statement = statement.where(Chat.project_id.is_(None))
        elif project_id is not None:
            statement = statement.where(Chat.project_id == project_id)
        if with_messages_only:
            statement = statement.where(exists().where(ChatMessage.chat_id == Chat.id))
        return list(self.db.scalars(statement))

    def list_chat_messages(self, chat_id: int) -> list[ChatMessage]:
        return list(
            self.db.scalars(
                select(ChatMessage)
                .where(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        )

    def add_chat_message(self, chat_id: int, role: str, content: str, **fields) -> ChatMessage:
        metadata = fields.pop("metadata", None)
        message = self.add(
            ChatMessage(
                chat_id=chat_id,
                role=role,
                content=content,
                metadata_json=json.dumps(metadata, sort_keys=True) if metadata else None,
                **fields,
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
        existing = self.db.scalar(
            select(ChatMessage).where(ChatMessage.generation_id == generation_id)
        )
        if existing is None:
            try:
                with self.db.begin_nested():
                    existing = self.add_chat_message(
                        chat_id, "assistant", content, generation_id=generation_id, **metadata
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
        if message is not None:
            message.content = content
            self.db.flush()
        return message

    def rename_chat_from_prompt(self, chat_id: int, prompt: str) -> None:
        chat = self.get_chat(chat_id)
        if chat is None or chat.title != "New chat":
            return
        title = " ".join(prompt.strip().split())
        chat.title = title[:54] + "..." if len(title) > 57 else title or "New chat"

    def delete_chat(self, chat_id: int) -> None:
        chat = self.get_chat(chat_id)
        if chat is not None:
            self.db.delete(chat)
            self.db.flush()

    def create_project(
        self, name: str, description: str | None = None, priority: int = 5
    ) -> Project:
        return self.add(Project(name=name, description=description, priority=priority))

    def list_projects(self, status: ProjectStatus | None = None) -> list[Project]:
        statement = select(Project).order_by(Project.priority.desc(), Project.updated_at.desc())
        if status is not None:
            statement = statement.where(Project.status == status)
        return list(self.db.scalars(statement))

    def update_project(
        self, project_id: int, name: str, description: str | None, priority: int
    ) -> Project:
        project = self.get_project(project_id)
        if project is None:
            raise ValueError("Project not found")
        project.name = name
        project.description = description
        project.priority = priority
        self.db.flush()
        return project

    def delete_project(self, project_id: int) -> None:
        project = self.get_project(project_id)
        if project is None:
            return
        for chat in list(project.chats):
            self.db.delete(chat)
        self.db.delete(project)
        self.db.flush()

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.chat import Chat, ChatGeneration, ChatMessage
from app.models.enums import ProjectStatus
from app.models.project import Project

#: Everything ``ChatUpdateRequest`` can change on a chat that has no messages
#: yet, minus ``pinned`` -- a pinned chat is never offered for reuse, so its pin
#: must survive rather than be reset out from under the user.
REUSABLE_CHAT_FIELDS = (
    "title",
    "repo_id",
    "agent_mode",
    "effort",
    "agent_definition_id",
    "disabled_tools",
)


def _column_default(name: str):
    """The value a fresh INSERT would give this column of ``chats``.

    Read off the column rather than restated here: a default written down in two
    places is a default that will disagree with itself after the next edit to the
    model, and the whole point of resetting a recycled chat is that the caller
    cannot tell it apart from one that was just inserted.
    """

    default = Chat.__table__.c[name].default
    if default is None:
        return None
    return default.arg(None) if default.is_callable else default.arg


def _like_pattern(value: str) -> str:
    """Escape LIKE wildcards so a query containing % or _ matches literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(content: str, query: str, window: int = 70) -> str:
    """A short excerpt centred on the match, so the reason for the hit is visible."""
    flat = " ".join(content.split())
    position = flat.lower().find(query.lower())
    if position == -1:
        return flat[: window * 2] + ("..." if len(flat) > window * 2 else "")
    start = max(0, position - window // 2)
    end = min(len(flat), position + len(query) + window)
    return ("..." if start > 0 else "") + flat[start:end] + ("..." if end < len(flat) else "")


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

    def find_empty_chat(self, project_id: int | None = None) -> Chat | None:
        """The scratch thread a fresh "New chat" should land on, if one is already there.

        Deliberately narrower than "has no messages": a pinned or archived empty
        chat is one the user put somewhere on purpose, and a chat carrying a
        generation is mid-turn even in the moment before its rows land.

        The scope test is exact rather than convenient. A chat's project is fixed
        at creation -- nothing reassigns it -- so ``IS NULL`` against ``= :id``
        partitions the chats completely, and an empty chat inside a project can
        never be handed back as the unprojected one.
        """

        statement = (
            select(Chat)
            .where(
                Chat.archived.is_(False),
                Chat.pinned.is_(False),
                ~exists().where(ChatMessage.chat_id == Chat.id),
                ~exists().where(ChatGeneration.chat_id == Chat.id),
            )
            .order_by(Chat.updated_at.desc(), Chat.id.desc())
            .limit(1)
        )
        if project_id is None:
            statement = statement.where(Chat.project_id.is_(None))
        else:
            statement = statement.where(Chat.project_id == project_id)
        return self.db.scalar(statement)

    def reset_chat_for_reuse(self, chat: Chat) -> Chat:
        """Give a recycled thread back in the state a brand-new one would be in.

        ``project_id`` is untouched: it is the scope this chat was found under,
        not a setting. So are ``archived`` and ``pinned`` -- a chat carrying
        either is never offered for reuse in the first place.
        """

        for name in REUSABLE_CHAT_FIELDS:
            setattr(chat, name, _column_default(name))
        return chat

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
            # Pinned first, then most recent: a pin is a promise the thread stays
            # at the top no matter how long since it was last touched.
            .order_by(Chat.pinned.desc(), Chat.updated_at.desc(), Chat.id.desc())
            .limit(limit)
        )
        if unprojected_only:
            statement = statement.where(Chat.project_id.is_(None))
        elif project_id is not None:
            statement = statement.where(Chat.project_id == project_id)
        if with_messages_only:
            statement = statement.where(exists().where(ChatMessage.chat_id == Chat.id))
        return list(self.db.scalars(statement))

    def search_chats(self, query: str, limit: int = 30) -> list[dict]:
        """Find chats by title or by the text of any message they contain.

        Returns one entry per chat with the first matching message, so a hit found deep
        in a conversation can show why it matched instead of only a title.
        """
        cleaned = " ".join(query.split())
        if not cleaned:
            return []
        pattern = f"%{_like_pattern(cleaned)}%"
        title_match = Chat.title.ilike(pattern, escape="\\")
        message_match = exists().where(
            and_(ChatMessage.chat_id == Chat.id, ChatMessage.content.ilike(pattern, escape="\\"))
        )
        chats = list(
            self.db.scalars(
                select(Chat)
                .where(Chat.archived.is_(False), or_(title_match, message_match))
                .order_by(Chat.updated_at.desc(), Chat.id.desc())
                .limit(limit)
            )
        )
        if not chats:
            return []

        # One query for every hit rather than one per chat, then keep the earliest
        # matching message for each.
        matches: dict[int, ChatMessage] = {}
        for message in self.db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.chat_id.in_([chat.id for chat in chats]),
                ChatMessage.content.ilike(pattern, escape="\\"),
            )
            .order_by(ChatMessage.chat_id, ChatMessage.created_at, ChatMessage.id)
        ):
            matches.setdefault(message.chat_id, message)

        results = []
        for chat in chats:
            message = matches.get(chat.id)
            results.append(
                {
                    "chat": chat,
                    "snippet": _snippet(message.content, cleaned) if message else None,
                    "matched_title": cleaned.lower() in chat.title.lower(),
                }
            )
        return results

    def get_chat_message(self, message_id: int) -> ChatMessage | None:
        return self.db.get(ChatMessage, message_id)

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

    def resolve_calendar_proposal(
        self,
        message_id: int,
        *,
        status: str,
        event_id: str | None = None,
        note: str | None = None,
    ) -> ChatMessage | None:
        """Stamp the outcome of a card click onto the proposal message itself.

        This is the durable record of "the user decided" -- the thing whose
        absence let an approved card come back offering Approve again after a
        view switch. It lives on the proposal's own metadata rather than in a
        following message, so it is position-independent and survives a
        reload.

        Refuses a message that is not a proposal, and refuses one already
        carrying a ``status``. That second refusal is what makes approving
        idempotent: a double-click or a stale tab gets ``None`` here, and the
        route turns it into a 409 rather than a second write to the calendar.
        """
        message = self.db.get(ChatMessage, message_id)
        if message is None or message.response_kind != "calendar_proposal":
            return None
        try:
            metadata = json.loads(message.metadata_json) if message.metadata_json else {}
            proposal = metadata["calendar_proposal"]
        except (TypeError, KeyError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(proposal, dict) or proposal.get("status"):
            return None
        proposal["status"] = status
        proposal["resolved_at"] = datetime.now(UTC).isoformat()
        if event_id is not None:
            proposal["resolved_event_id"] = event_id
        if note is not None:
            proposal["resolution_note"] = note
        metadata["calendar_proposal"] = proposal
        message.metadata_json = json.dumps(metadata, sort_keys=True)
        self.db.flush()
        return message

    def rename_chat_from_prompt(self, chat_id: int, prompt: str) -> None:
        chat = self.get_chat(chat_id)
        if chat is None or chat.title != "New chat":
            return
        title = " ".join(prompt.strip().split())
        chat.title = title[:54] + "..." if len(title) > 57 else title or "New chat"

    def set_chat_pinned(self, chat_id: int, pinned: bool) -> Chat | None:
        """Pin or unpin a chat.

        Pinning is not an edit of the thread, so `updated_at` is left alone: a
        pin must not reorder the unpinned list when it is undone.
        """
        chat = self.get_chat(chat_id)
        if chat is None:
            return None
        chat.pinned = pinned
        self.db.flush()
        return chat

    def rename_chat(self, chat_id: int, title: str) -> Chat | None:
        """Apply a title the user chose.

        Unlike the automatic titling above this overwrites whatever is there, including
        a title derived from the first prompt: an explicit rename is the whole point.
        """
        chat = self.get_chat(chat_id)
        if chat is None:
            return None
        chat.title = " ".join(title.split())
        self.db.flush()
        return chat

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

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Chat(TimestampMixin, Base):
    """Persisted chat thread shown in the Streamlit sidebar."""

    __tablename__ = "chats"
    __table_args__ = (
        Index("ix_chats_project_updated", "project_id", "updated_at"),
        Index("ix_chats_archived_updated", "archived", "updated_at"),
        #: Pinned chats head the sidebar, so the list is read pin-first.
        Index("ix_chats_pinned_updated", "pinned", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="New chat")
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    archived: Mapped[bool] = mapped_column(default=False, nullable=False)
    pinned: Mapped[bool] = mapped_column(default=False, nullable=False)

    #: Agent turns run against the conversation's folder, not one picked per
    #: message, so the workspace and the permission mode belong to the thread.
    #: A chat with no repository still runs agent turns -- the registry simply
    #: withholds the tools that need one.
    repo_id: Mapped[str | None] = mapped_column(String(64))
    agent_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")

    #: How much work a turn in this chat is allowed to spend before answering.
    #: ``high`` is the full pipeline: a model decides the route, and the reply is
    #: re-examined when it sounds unsure. ``low`` keeps only the deterministic
    #: parts of that, which is roughly half the model calls. It belongs to the
    #: thread, like the agent settings beside it, and is chosen per message so a
    #: single question can be answered carefully without changing the thread.
    #: ``server_default`` is load-bearing here in a way it is not for the columns
    #: around it: the raw-sqlite3 INSERT in ``create_chat_for_session`` names
    #: every one of those but not this, so a NOT NULL with only a Python-side
    #: default rejects the row outright. Note the value is unquoted -- SQLAlchemy
    #: quotes it, and writing ``"'high'"`` here renders a default with the quotes,
    #: which stores the quote characters as part of the value.
    effort: Mapped[str] = mapped_column(
        String(16), nullable=False, default="high", server_default="high"
    )
    agent_definition_id: Mapped[str | None] = mapped_column(String(64))
    #: Tool names withheld from every agent turn in this chat. Applies to both
    #: built-in tools and bridged connector tools -- see agent_core.tools.registry.
    #: ``server_default`` matters here, not just ``default``: agent_core writes
    #: chat rows through its own raw-sqlite3 INSERT (``create_chat_for_session``)
    #: that never goes through the ORM, so the NOT NULL fallback has to live in
    #: the column's own DDL rather than in Python-side insert defaults.
    disabled_tools: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="'[]'"
    )

    project = relationship("Project", back_populates="chats")
    messages = relationship(
        "ChatMessage",
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    """Single persisted user, assistant, or system message in a chat."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_chat_created", "chat_id", "created_at"),
        Index("ix_chat_messages_generation", "generation_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    thinking: Mapped[str | None] = mapped_column(Text)
    response_kind: Mapped[str | None] = mapped_column(String(40))
    provider_name: Mapped[str | None] = mapped_column(String(120))
    model_name: Mapped[str | None] = mapped_column(String(240))
    route_name: Mapped[str | None] = mapped_column(String(120))
    finish_reason: Mapped[str | None] = mapped_column(String(40))
    trace_id: Mapped[str | None] = mapped_column(String(80))
    metadata_json: Mapped[str | None] = mapped_column(Text)
    generation_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    chat = relationship("Chat", back_populates="messages")


class ChatGeneration(Base):
    """Durable, refresh-safe execution state for one chat response."""

    __tablename__ = "chat_generations"
    __table_args__ = (
        Index("ix_chat_generations_chat_created", "chat_id", "created_at"),
        Index("ix_chat_generations_chat_status", "chat_id", "status"),
        Index("ix_chat_generations_client_request", "client_request_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    llm_id: Mapped[str | None] = mapped_column(String(80))
    client_request_id: Mapped[str | None] = mapped_column(String(80))
    user_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    status_detail: Mapped[str | None] = mapped_column(String(120))
    partial_response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thinking: Mapped[str | None] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    assistant_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    error: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(String(80))
    locale: Mapped[str | None] = mapped_column(String(40))
    response_kind: Mapped[str | None] = mapped_column(String(40))
    provider_name: Mapped[str | None] = mapped_column(String(120))
    model_name: Mapped[str | None] = mapped_column(String(240))
    route_name: Mapped[str | None] = mapped_column(String(120))
    finish_reason: Mapped[str | None] = mapped_column(String(40))
    trace_id: Mapped[str | None] = mapped_column(String(80))
    metadata_json: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(36))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatEvent(Base):
    """Append-only live log for one chat, tailed as NDJSON.

    Both kinds of turn write here: the chat generation worker appends coalesced
    response deltas, and ``agent_core.store.append_event`` mirrors a run's events
    whenever its session belongs to a chat.  That gives the browser a single
    monotonic cursor for a thread that mixes both, so one reconnecting reader
    replaces the old 250ms generation poll and the per-session event tail.

    The agent's own ``workspace_agent_events`` log is unchanged and still backs
    ``GET /agent-sessions/{id}/events``; this is a second, chat-scoped sequence.
    """

    __tablename__ = "chat_events"
    __table_args__ = (Index("ix_chat_events_chat_seq", "chat_id", "seq"),)

    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    #: Exactly one of these is set, naming which turn produced the event.
    generation_id: Mapped[str | None] = mapped_column(String(36))
    agent_session_id: Mapped[str | None] = mapped_column(String(36))
    #: The anchor ``chat_messages`` row the event belongs to, so a reader can
    #: route an event to a turn without resolving the session first.
    message_id: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.associations import memory_project_links
from app.models.enums import MemoryType, enum_values
from app.models.mixins import TimestampMixin


class Memory(TimestampMixin, Base):
    """Durable accepted memory. Conversation transcripts do not belong here."""

    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memories_confidence"),
        CheckConstraint("importance >= 1 AND importance <= 10", name="ck_memories_importance"),
        Index("ix_memories_type_active", "memory_type", "is_active"),
        Index("ix_memories_importance", "importance"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    memory_text: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(MemoryType, native_enum=False, values_callable=enum_values),
        nullable=False,
    )
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source: Mapped[str | None] = mapped_column(String(255))
    source_sentence: Mapped[str | None] = mapped_column(Text)
    source_conversation_id: Mapped[int | None] = mapped_column(Integer)
    canonical_slot: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("memories.id"))
    update_reason: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("memories.id"))

    supersedes = relationship("Memory", remote_side=[id], foreign_keys=[supersedes_id])
    superseded_by = relationship("Memory", remote_side=[id], foreign_keys=[superseded_by_id])
    embedding = relationship(
        "MemoryEmbedding",
        back_populates="memory",
        cascade="all, delete-orphan",
        uselist=False,
    )
    projects = relationship(
        "Project",
        secondary=memory_project_links,
        back_populates="memories",
    )

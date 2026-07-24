from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class MemorySource(TimestampMixin, Base):
    """One user-authored source supporting an accepted memory."""

    __tablename__ = "memory_sources"
    __table_args__ = (
        UniqueConstraint(
            "memory_id",
            "source_fingerprint",
            name="uq_memory_sources_memory_fingerprint",
        ),
        Index("ix_memory_sources_message_active", "source_message_id", "is_active"),
        Index("ix_memory_sources_conversation_active", "source_conversation_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    memory_id: Mapped[int] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_conversation_id: Mapped[int | None] = mapped_column(Integer)
    source_message_id: Mapped[int | None] = mapped_column(Integer)
    source_sentence: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detachment_reason: Mapped[str | None] = mapped_column(String(32))

    memory = relationship("Memory", back_populates="sources")

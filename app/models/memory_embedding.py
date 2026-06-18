from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class MemoryEmbedding(TimestampMixin, Base):
    """Best-effort vector representation for a durable memory."""

    __tablename__ = "memory_embeddings"
    __table_args__ = (
        Index("ix_memory_embeddings_status", "status"),
        Index("ix_memory_embeddings_model", "model"),
    )

    memory_id: Mapped[int] = mapped_column(ForeignKey("memories.id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False, default="ollama")
    dimensions: Mapped[int | None] = mapped_column(Integer)
    vector_json: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="missing")
    error: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memory = relationship("Memory", back_populates="embedding")

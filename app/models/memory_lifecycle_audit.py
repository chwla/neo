from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryLifecycleAudit(Base):
    """Append-only lifecycle event for durable memory status changes."""

    __tablename__ = "memory_lifecycle_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    memory_id: Mapped[int] = mapped_column(ForeignKey("memories.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    related_memory_id: Mapped[int | None] = mapped_column(ForeignKey("memories.id"))
    source_sentence: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

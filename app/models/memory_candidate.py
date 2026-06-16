from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CandidateStatus, CandidateType, enum_values


class MemoryCandidate(Base):
    """Extracted candidate awaiting review before promotion to durable memory."""

    __tablename__ = "memory_candidates"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_candidates_confidence"),
        CheckConstraint("importance >= 1 AND importance <= 10", name="ck_candidates_importance"),
        Index("ix_candidates_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_text: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_type: Mapped[CandidateType] = mapped_column(
        Enum(CandidateType, native_enum=False, values_callable=enum_values),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    reasoning: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CandidateStatus] = mapped_column(
        Enum(CandidateStatus, native_enum=False, values_callable=enum_values),
        nullable=False,
        default=CandidateStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_memory_id: Mapped[int | None] = mapped_column(ForeignKey("memories.id"))

    accepted_memory = relationship("Memory")

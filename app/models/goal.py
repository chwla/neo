from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import GoalStatus, enum_values
from app.models.mixins import TimestampMixin


class Goal(TimestampMixin, Base):
    """Active or historical user goal."""

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("priority >= 1 AND priority <= 10", name="ck_goals_priority"),
        Index("ix_goals_status_priority", "status", "priority"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    goal: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    status: Mapped[GoalStatus] = mapped_column(
        Enum(GoalStatus, native_enum=False, values_callable=enum_values),
        nullable=False,
        default=GoalStatus.ACTIVE,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

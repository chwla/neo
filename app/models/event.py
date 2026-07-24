from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.associations import event_project_links


class Event(Base):
    """Timeline event. Events are queried separately from durable memories."""

    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("importance >= 1 AND importance <= 10", name="ck_events_importance"),
        Index("ix_events_event_date", "event_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    event_date: Mapped[date | None] = mapped_column(Date)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    projects = relationship(
        "Project",
        secondary=event_project_links,
        back_populates="events",
    )

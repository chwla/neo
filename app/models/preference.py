from sqlalchemy import CheckConstraint, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class Preference(TimestampMixin, Base):
    """User preference that may evolve over time."""

    __tablename__ = "preferences"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_preferences_confidence"),
        CheckConstraint("importance >= 1 AND importance <= 10", name="ck_preferences_importance"),
        Index("ix_preferences_category_active", "category", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_slot: Mapped[str | None] = mapped_column(String(160), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

from sqlalchemy import CheckConstraint, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ProfileFact(TimestampMixin, Base):
    """Durable identity facts about the user."""

    __tablename__ = "profile"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_profile_confidence"),
        Index("ix_profile_key_active", "key", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


from sqlalchemy import CheckConstraint, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.associations import event_project_links, memory_project_links
from app.models.enums import ProjectStatus, enum_values
from app.models.mixins import TimestampMixin


class Project(TimestampMixin, Base):
    """Active or historical project tracked by Neo."""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("priority >= 1 AND priority <= 10", name="ck_projects_priority"),
        Index("ix_projects_status_priority", "status", "priority"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, native_enum=False, values_callable=enum_values),
        nullable=False,
        default=ProjectStatus.ACTIVE,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    memories = relationship(
        "Memory",
        secondary=memory_project_links,
        back_populates="projects",
    )
    events = relationship(
        "Event",
        secondary=event_project_links,
        back_populates="projects",
    )
    chats = relationship("Chat", back_populates="project")

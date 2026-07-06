from sqlalchemy import Column, ForeignKey, Table

from app.db.base import Base

memory_project_links = Table(
    "memory_project_links",
    Base.metadata,
    Column("memory_id", ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
)

event_project_links = Table(
    "event_project_links",
    Base.metadata,
    Column("event_id", ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
)

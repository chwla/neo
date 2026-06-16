"""Initial Neo memory schema.

Revision ID: 20260615_0001
Revises:
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260615_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("importance >= 1 AND importance <= 10", name="ck_events_importance"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_event_date", "events", ["event_date"])

    op.create_table(
        "goals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("goal", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "completed", "paused", "abandoned", name="goalstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("priority >= 1 AND priority <= 10", name="ck_goals_priority"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_goals_status_priority", "goals", ["status", "priority"])

    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("memory_text", sa.Text(), nullable=False),
        sa.Column(
                "memory_type",
                sa.Enum(
                "identity",
                "preference",
                "goal_related",
                "project_related",
                "knowledge",
                "relationship",
                "life_fact",
                name="memorytype",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memories_confidence"),
        sa.CheckConstraint("importance >= 1 AND importance <= 10", name="ck_memories_importance"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["memories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memories_importance", "memories", ["importance"])
    op.create_index("ix_memories_type_active", "memories", ["memory_type", "is_active"])

    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_preferences_confidence"),
        sa.CheckConstraint("importance >= 1 AND importance <= 10", name="ck_preferences_importance"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_preferences_category_active", "preferences", ["category", "is_active"])

    op.create_table(
        "profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_profile_confidence"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_profile_key_active", "profile", ["key", "is_active"])

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
                "status",
            sa.Enum(
                "active",
                "completed",
                "paused",
                "abandoned",
                "archived",
                name="projectstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("priority >= 1 AND priority <= 10", name="ck_projects_priority"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_projects_status_priority", "projects", ["status", "priority"])

    op.create_table(
        "reflections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reflection", sa.Text(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("importance >= 1 AND importance <= 10", name="ck_reflections_importance"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "event_project_links",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id", "project_id"),
    )

    op.create_table(
        "memory_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_text", sa.Text(), nullable=False),
        sa.Column(
            "candidate_type",
            sa.Enum("identity", "preference", "goal", "project", "event", "memory", "none", name="candidatetype", native_enum=False),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "accepted", "rejected", "merged", name="candidatestatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_memory_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_candidates_confidence"),
        sa.CheckConstraint("importance >= 1 AND importance <= 10", name="ck_candidates_importance"),
        sa.ForeignKeyConstraint(["accepted_memory_id"], ["memories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_candidates_status_created", "memory_candidates", ["status", "created_at"])

    op.create_table(
        "memory_project_links",
        sa.Column("memory_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("memory_id", "project_id"),
    )


def downgrade() -> None:
    op.drop_table("memory_project_links")
    op.drop_index("ix_candidates_status_created", table_name="memory_candidates")
    op.drop_table("memory_candidates")
    op.drop_table("event_project_links")
    op.drop_table("reflections")
    op.drop_index("ix_projects_status_priority", table_name="projects")
    op.drop_table("projects")
    op.drop_index("ix_profile_key_active", table_name="profile")
    op.drop_table("profile")
    op.drop_index("ix_preferences_category_active", table_name="preferences")
    op.drop_table("preferences")
    op.drop_index("ix_memories_type_active", table_name="memories")
    op.drop_index("ix_memories_importance", table_name="memories")
    op.drop_table("memories")
    op.drop_index("ix_goals_status_priority", table_name="goals")
    op.drop_table("goals")
    op.drop_index("ix_events_event_date", table_name="events")
    op.drop_table("events")

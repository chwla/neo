from enum import Enum, StrEnum


def enum_values(enum_cls: type[Enum]) -> list[str]:
    """Persist enum values, not Python member names, in the database."""

    return [item.value for item in enum_cls]


class GoalStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    ABANDONED = "abandoned"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    ABANDONED = "abandoned"
    ARCHIVED = "archived"


class MemoryType(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    GOAL_RELATED = "goal_related"
    PROJECT_RELATED = "project_related"
    KNOWLEDGE = "knowledge"
    RELATIONSHIP = "relationship"
    LIFE_FACT = "life_fact"


class CandidateType(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    GOAL = "goal"
    PROJECT = "project"
    EVENT = "event"
    MEMORY = "memory"
    NONE = "none"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MERGED = "merged"

from datetime import date, datetime

from pydantic import Field

from app.models.enums import CandidateStatus, CandidateType, GoalStatus, MemoryType, ProjectStatus
from app.schemas.common import Confidence, Importance, OrmSchema


class ProfileFactRead(OrmSchema):
    id: int
    key: str
    value: str
    confidence: float
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PreferenceRead(OrmSchema):
    id: int
    category: str
    value: str
    confidence: float
    importance: int
    canonical_slot: str | None = None
    fingerprint: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class GoalRead(OrmSchema):
    id: int
    goal: str
    description: str | None
    priority: int
    status: GoalStatus
    target_date: date | None = None
    horizon_months: int | None = None
    fingerprint: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ProjectRead(OrmSchema):
    id: int
    name: str
    description: str | None
    status: ProjectStatus
    priority: int
    created_at: datetime
    updated_at: datetime


class EventRead(OrmSchema):
    id: int
    event: str
    description: str | None
    event_date: date | None
    importance: int
    fingerprint: str | None = None


class EducationRead(OrmSchema):
    id: int
    institution: str
    degree: str | None
    field_of_study: str | None
    graduation_date: date | None
    description: str | None
    fingerprint: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ActivityRead(OrmSchema):
    id: int
    category: str
    activity: str
    description: str | None
    fingerprint: str
    started_at: datetime
    expires_at: datetime
    archived_at: datetime | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MemorySourceRead(OrmSchema):
    id: int
    source_conversation_id: int | None
    source_message_id: int | None
    source_sentence: str
    source_fingerprint: str
    is_active: bool
    detachment_reason: str | None
    created_at: datetime
    updated_at: datetime


class MemoryRead(OrmSchema):
    id: int
    memory_text: str
    memory_type: MemoryType
    importance: int
    confidence: float
    source: str | None
    source_sentence: str | None
    source_conversation_id: int | None
    canonical_slot: str | None
    fingerprint: str | None = None
    expires_at: datetime | None = None
    status: str
    supersedes_id: int | None
    update_reason: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    superseded_by_id: int | None
    sources: list[MemorySourceRead] = Field(default_factory=list)


class MemoryCandidateCreate(OrmSchema):
    candidate_text: str = Field(min_length=1)
    candidate_type: CandidateType
    confidence: Confidence = 1.0
    importance: Importance = 5
    reasoning: str | None = None


class MemoryCandidateRead(OrmSchema):
    id: int
    candidate_text: str
    candidate_type: CandidateType
    confidence: float
    importance: int
    reasoning: str | None
    status: CandidateStatus
    created_at: datetime
    reviewed_at: datetime | None
    accepted_memory_id: int | None


class ReflectionRead(OrmSchema):
    id: int
    reflection: str
    importance: int
    created_at: datetime
    updated_at: datetime

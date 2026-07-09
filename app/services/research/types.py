from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class DepthMode(str, enum.Enum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


DEPTH_CONFIG: dict[DepthMode, dict[str, int]] = {
    DepthMode.QUICK: {"min_queries": 3, "max_queries": 5, "max_sources": 5, "max_rounds": 1},
    DepthMode.STANDARD: {"min_queries": 5, "max_queries": 8, "max_sources": 10, "max_rounds": 2},
    DepthMode.DEEP: {"min_queries": 8, "max_queries": 12, "max_sources": 20, "max_rounds": 3},
}


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    SEARCHING = "searching"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResearchSource(BaseModel):
    id: int = 0
    url: str
    title: str = ""
    domain: str = ""
    published_date: str | None = None
    fetched: bool = False
    fetch_status: str = "pending"
    fetch_error: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    text: str = ""
    extracted_text_length: int = 0
    evidence_count: int = 0
    quality_score: float = 0.0
    relevance_score: float = 0.0
    error: str | None = None


class ResearchEvidenceChunk(BaseModel):
    source_id: int
    source_url: str
    source_title: str
    text: str
    relevance_score: float = 0.0
    quality_score: float = 0.0
    claim_type: str = "general"
    evidence_category: str = "general"
    supports_subquestion: str | None = None
    extracted_at: str = ""


class ResearchPlan(BaseModel):
    objective: str = ""
    subquestions: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    freshness_required: bool = False
    source_preferences: list[str] = Field(default_factory=list)
    expected_output: str = "comparison"
    topic_intent: str | None = None
    normalized_entities: dict[str, str] = Field(default_factory=dict)
    comparison_tools: list[str] = Field(default_factory=list)
    original_query: str | None = None
    normalized_query: str | None = None
    normalization_reason: str | None = None
    domain_hint: str | None = None
    qualifiers: list[str] = Field(default_factory=list)
    ai_workload_focus: bool = False
    product_pair: str | None = None
    comparison_query: bool = True


class ProgressEvent(BaseModel):
    status: str
    progress_percent: int = 0
    current_step: str = ""
    message: str = ""
    queries_done: int = 0
    sources_found: int = 0
    sources_fetched: int = 0
    evidence_chunks: int = 0
    timestamp: str = ""


class ResearchJob(BaseModel):
    id: str
    user_query: str
    depth: DepthMode = DepthMode.STANDARD
    max_sources: int = 10
    max_rounds: int = 2
    status: JobStatus = JobStatus.QUEUED
    created_at: str = ""
    updated_at: str = ""
    progress_percent: int = 0
    current_step: str = ""
    plan: ResearchPlan | None = None
    generated_queries: list[str] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    evidence_chunks: list[ResearchEvidenceChunk] = Field(default_factory=list)
    report: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    progress_log: list[ProgressEvent] = Field(default_factory=list)

    def current_progress(self) -> ProgressEvent:
        return ProgressEvent(
            status=self.status.value,
            progress_percent=self.progress_percent,
            current_step=self.current_step,
            message=self.progress_log[-1].message if self.progress_log else "",
            queries_done=len(self.generated_queries),
            sources_found=len(self.sources),
            sources_fetched=sum(1 for s in self.sources if s.fetched),
            evidence_chunks=len(self.evidence_chunks),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


class StartResearchRequest(BaseModel):
    query: str
    depth: DepthMode = DepthMode.STANDARD
    max_sources: int | None = None
    max_rounds: int | None = None
    project_id: str | None = None
    task_id: str | None = None
    repo_id: str | None = None

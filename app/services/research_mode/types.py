"""Typed models for the evidence-grounded Enterprise Research Mode."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ResearchIntent = Literal["technical", "business", "market", "academic", "coding", "general"]
ResearchDepth = Literal["quick", "standard", "deep"]


class ResearchRunRequest(BaseModel):
    question: str = Field(min_length=3, max_length=10_000)
    mode: ResearchIntent = "general"
    freshness_required: bool = True
    depth: ResearchDepth = "standard"
    max_search_runs: int = Field(default=3, ge=1, le=4)
    max_sources: int = Field(default=12, ge=1, le=20)
    include_memory: bool = True
    include_conflict_analysis: bool = True
    created_by: str = "user"

    @field_validator("question")
    @classmethod
    def clean_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Research question is required.")
        return value


class ResearchPlanRequest(BaseModel):
    question: str = Field(min_length=3, max_length=10_000)
    mode: ResearchIntent = "general"
    freshness_required: bool = True
    depth: ResearchDepth = "standard"


class ResearchPlan(BaseModel):
    question: str
    intent: ResearchIntent = "general"
    freshness_required: bool = True
    objective: str = ""
    assumptions: list[str] = Field(default_factory=list)
    subquestions: list[str] = Field(default_factory=list)
    required_sources: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    memory_queries: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    expected_conflicts: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)


class ResearchEvidence(BaseModel):
    id: str | None = None
    source_type: str
    source_id: str | None = None
    citation_label: str | None = None
    evidence_text: str
    extracted_claim: str | None = None
    confidence: float = 0.0
    quality_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchClaim(BaseModel):
    id: str | None = None
    claim: str
    claim_type: str = "finding"
    confidence: float = 0.0
    citation_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["supported", "conflicted", "uncertain", "unsupported"] = "uncertain"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchConflict(BaseModel):
    topic: str
    conflict_type: str
    claims: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "low"
    recommended_resolution: str

from __future__ import annotations

from pydantic import BaseModel, Field


class WebSearchRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    mode: str = "research"
    max_queries: int = Field(default=6, ge=1, le=8)
    max_sources: int = Field(default=10, ge=1, le=12)
    freshness_required: bool = False
    fetch_sources: bool = True
    include_conflict_detection: bool = True


class WebSearchPlanRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    mode: str = "research"
    freshness_required: bool = False

"""Adapters for existing bounded web and memory systems."""

from __future__ import annotations

from app.services.memory_retrieval import MemoryRetrievalService
from app.services.memory_retrieval.types import MemoryRetrieveRequest
from app.services.web_search.service import ReliableWebSearchService
from app.services.web_search.types import WebSearchRunRequest


def collect_memory(question: str) -> dict:
    return MemoryRetrievalService().retrieve(
        MemoryRetrieveRequest(
            query=question, limit=8, include_score_breakdown=True, created_by="research_mode"
        )
    )


def collect_web(
    query: str, mode: str, freshness_required: bool, max_sources: int, include_conflicts: bool
) -> dict:
    return ReliableWebSearchService().run(
        WebSearchRunRequest(
            query=query,
            mode=mode,
            freshness_required=freshness_required,
            max_sources=max_sources,
            fetch_sources=True,
            include_conflict_detection=include_conflicts,
        )
    )

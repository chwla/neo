from __future__ import annotations

from typing import Any

from app.services.memory_retrieval import store
from app.services.memory_retrieval.indexer import MemoryIndexer
from app.services.memory_retrieval.pruning import MemoryPruner
from app.services.memory_retrieval.redaction import redact_memory
from app.services.memory_retrieval.retriever import MemoryRetriever
from app.services.memory_retrieval.types import (
    MemoryIndexRequest,
    MemoryItemCreate,
    MemoryItemUpdate,
    MemoryRetrieveRequest,
)


class MemoryRetrievalService:
    def __init__(self) -> None:
        store.initialize_memory_retrieval_tables()
        self.indexer, self.retriever, self.pruner = (
            MemoryIndexer(),
            MemoryRetriever(),
            MemoryPruner(),
        )

    def create(self, request: MemoryItemCreate) -> dict:
        safe, summary = redact_memory(
            {
                "title": request.title,
                "content_text": request.content_text,
                "content_json": request.content_json,
                "tags": request.tags,
            }
        )
        return store.upsert_item({**request.model_dump(), **safe, "redaction_summary": summary})

    def update(self, item_id: str, request: MemoryItemUpdate) -> dict:
        current = store.get_item(item_id)
        if not current:
            raise LookupError("Memory item not found.")
        fields = {key: value for key, value in request.model_dump().items() if value is not None}
        merged = {**current, **fields}
        safe, summary = redact_memory(
            {
                "title": merged["title"],
                "content_text": merged["content_text"],
                "content_json": merged.get("content_json", {}),
                "tags": merged.get("tags", []),
            }
        )
        return store.update_item(item_id, {**safe, "redaction_summary": summary}) or current

    def delete(self, item_id: str) -> bool:
        return store.delete_item(item_id)

    def retrieve(self, request: MemoryRetrieveRequest) -> dict:
        return self.retriever.retrieve(request)

    def retrieve_for_agent(
        self, objective: str, *, scope_type: str | None, scope_id: str | None, source: str
    ) -> dict:
        return self.retrieve(
            MemoryRetrieveRequest(
                query=objective,
                scope_type=scope_type,
                scope_id=scope_id,
                memory_types=["constraint", "decision", "failure", "fix", "safety_note", "summary"],
                limit=8,
                created_by=source,
            )
        )

    def index(self, request: MemoryIndexRequest) -> dict:
        requested = getattr(request, "source_types", []) or (
            [request.source_type] if request.source_type else []
        )
        source_types = requested or [
            "context_summary", "agentic_run", "coding_run", *self.indexer.SOURCE_TYPES
        ]
        items: list[dict] = []
        counts: dict[str, int] = {}
        for source_type in source_types:
            before = len(items)
            if source_type == "context_summary":
                items.extend(
                    self.indexer.index_context_summaries(request.scope_type, request.scope_id)
                )
            elif source_type in {"agentic_run", "coding_run"}:
                if request.source_id:
                    items.extend(self.indexer.index_run(source_type, request.source_id))
                else:
                    items.extend(
                        self.indexer.index_source_type(
                            source_type, request.scope_type, request.scope_id
                        )
                    )
            else:
                items.extend(
                    self.indexer.index_source_type(
                        source_type, request.scope_type, request.scope_id
                    )
                )
            counts[source_type] = len(items) - before
        return {"indexed": len(items), "counts": counts, "items": items}

    def refresh_agentic_run(self, run: dict[str, Any]) -> list[dict]:
        state = run.get("state") or {}
        scope_type, scope_id = (
            ("task", state.get("task_id"))
            if state.get("task_id")
            else ("project", state.get("project_id") or run["id"])
        )
        items = [
            self.indexer.index_record(
                scope_type=scope_type,
                scope_id=scope_id,
                source_type="agentic_run",
                source_id=run["id"],
                title=f"Agentic run: {run['objective']}",
                content=run.get("final_report") or run["objective"],
                tags=["agentic_run", "summary"],
            )
        ]
        for index, failure in enumerate(state.get("failures") or state.get("blockers") or []):
            items.append(
                self.indexer.index_record(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    source_type="agentic_run",
                    source_id=f"{run['id']}:failure:{index}",
                    title=f"Agentic blocker: {run['objective'][:100]}",
                    content=failure,
                    memory_type="failure",
                    importance=4,
                    tags=["agentic_run", "failure"],
                )
            )
        return items

    def list_items(self, **filters: Any) -> list[dict]:
        return store.list_items(
            filters.get("scope_type"), filters.get("scope_id"), filters.get("limit", 100)
        )

    def item(self, item_id: str) -> dict | None:
        return store.get_item(item_id)

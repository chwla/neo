from __future__ import annotations

from app.services.memory_retrieval import store
from app.services.memory_retrieval.scorer import score


class MemoryRetriever:
    def retrieve(self, request) -> dict:
        candidates = store.candidates(request.query)
        results = []
        for item in candidates:
            if (
                request.scope_type
                and item["scope_type"] != request.scope_type
                and item["scope_id"] != request.scope_id
            ):
                continue
            if (
                request.scope_id
                and item["scope_id"] != request.scope_id
                and item["scope_type"] != request.scope_type
            ):
                continue
            if request.memory_types and item["memory_type"] not in request.memory_types:
                continue
            if request.source_types and item["source_type"] not in request.source_types:
                continue
            if request.tags and not (set(request.tags) & set(item.get("tags", []))):
                continue
            ranking = score(
                item,
                request.query,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                tags=request.tags,
            )
            snippet = item["content_text"][:360]
            results.append(
                {
                    "memory_id": item["id"],
                    "title": item["title"],
                    "snippet": snippet,
                    "source_type": item["source_type"],
                    "source_id": item.get("source_id"),
                    "memory_type": item["memory_type"],
                    **ranking,
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        results = results[: request.limit]
        store.mark_accessed([item["memory_id"] for item in results])
        audit = store.save_retrieval(
            {
                "query_text": request.query,
                "scope_type": request.scope_type,
                "scope_id": request.scope_id,
                "filters": {
                    "memory_types": request.memory_types,
                    "source_types": request.source_types,
                    "tags": request.tags,
                },
                "results": results,
                "scorer": {"version": "hybrid-fts-v1"},
                "created_by": request.created_by,
            }
        )
        return {
            "results": results
            if request.include_score_breakdown
            else [
                {key: value for key, value in item.items() if key != "score_breakdown"}
                for item in results
            ],
            "retrieval": audit,
        }

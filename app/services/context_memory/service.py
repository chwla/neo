from __future__ import annotations

from app.services.context_memory import extractor, store
from app.services.context_memory.redaction import redact
from app.services.context_memory.summarizer import summarize
from app.services.context_memory.token_budget import compression
from app.services.context_memory.types import CompactRequest, ContextEventCreate

VALID_SCOPES = {"chat", "agent_run", "coding_run", "task", "project", "repo_workspace"}


class ContextMemoryService:
    def _validate(self, scope_type: str, scope_id: str) -> None:
        if scope_type not in VALID_SCOPES:
            raise ValueError("Unsupported context-memory scope.")
        if not scope_id.strip():
            raise ValueError("A scope id is required.")

    def event(self, scope_type: str, scope_id: str, request: ContextEventCreate) -> dict:
        self._validate(scope_type, scope_id)
        return store.add_event(
            scope_type,
            scope_id,
            request.event_type,
            redact(request.content),
            request.event_ref_id,
            request.importance,
        )

    def events(self, scope_type: str, scope_id: str) -> list[dict]:
        self._validate(scope_type, scope_id)
        return store.list_events(scope_type, scope_id)

    def preview(self, request: CompactRequest) -> dict:
        self._validate(request.scope_type, request.scope_id)
        result = summarize(
            extractor.extract(request.scope_type, request.scope_id, request.include_events),
            request.max_summary_tokens,
        )
        result.update(
            {
                "scope_type": request.scope_type,
                "scope_id": request.scope_id,
                "source_type": request.scope_type,
                "source_id": request.scope_id,
                "status": "degraded" if request.mode == "llm" else "deterministic",
                "reason": "No provider summary was requested; deterministic metadata summary used."
                if request.mode != "llm"
                else "No provider available; deterministic summary used.",
                "compression_ratio": compression(
                    result["token_estimate_before"], result["token_estimate_after"]
                ),
            }
        )
        return result

    def compact(self, request: CompactRequest) -> dict:
        preview = self.preview(request)
        saved = store.save_summary(preview)
        return {
            **saved,
            "status": preview["status"],
            "reason": preview["reason"],
            "compression_ratio": preview["compression_ratio"],
        }

    def summaries(self, scope_type: str | None = None, scope_id: str | None = None) -> list[dict]:
        if scope_type:
            self._validate(scope_type, scope_id or "filter")
        return store.list_summaries(scope_type, scope_id)

    def summary(self, summary_id: str) -> dict | None:
        return store.get_summary(summary_id)

    def scope(self, scope_type: str, scope_id: str) -> dict:
        self._validate(scope_type, scope_id)
        summaries = store.list_summaries(scope_type, scope_id, 1)
        if summaries:
            item = summaries[0]
            return {
                "used": True,
                "summary_id": item["id"],
                "summary_text": item["summary_text"],
                "token_estimate_saved": max(
                    0, item["token_estimate_before"] - item["token_estimate_after"]
                ),
            }
        return {
            "used": False,
            "summary_id": None,
            "summary_text": "",
            "token_estimate_saved": 0,
            "reason": "No context summary exists; source data remains authoritative.",
        }

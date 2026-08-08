from __future__ import annotations

import sqlite3
from dataclasses import replace

from app.services.memory.adapters import ChatMemoryAdapter
from app.services.memory.extraction_contracts import ExtractionMode, ExtractionRequest
from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator
from app.services.memory.extraction_diagnostics import InMemoryExtractionDiagnostics
from tests.memory.mutation_helpers import memory_harness


def extraction_harness(tmp_path, *, model=None, guest=False):
    harness = memory_harness(tmp_path, guest=guest)
    harness.coordinator.flags = replace(
        harness.coordinator.flags,
        extraction_enabled=True,
        foreground_extraction_enabled=True,
        post_turn_extraction_enabled=True,
    )
    diagnostics = InMemoryExtractionDiagnostics()
    extraction = MemoryExtractionCoordinator(
        ChatMemoryAdapter(harness.coordinator),
        model=model,
        diagnostics=diagnostics,
    )
    return harness, extraction, diagnostics


def extraction_input(
    harness,
    text: str,
    *,
    message_id: str,
    mode: ExtractionMode = ExtractionMode.FOREGROUND_DETERMINISTIC,
    explicit: bool = False,
    supporting_window=(),
    maximum_candidates: int = 4,
    incognito: bool = False,
    memory_enabled: bool = True,
):
    context = replace(
        harness.context,
        message_id=message_id,
        conversation_id="phase4-conversation",
        session_id="phase4-session",
        execution=replace(
            harness.context.execution,
            is_incognito=incognito,
            memory_enabled=memory_enabled,
        ),
    )
    request = ExtractionRequest(
        request_id=f"request-{message_id}",
        owner_id=context.execution.owner_id,
        conversation_id=context.conversation_id,
        session_id=context.session_id,
        message_id=message_id,
        user_message=text,
        supporting_window=supporting_window,
        explicit_memory_intent=explicit,
        incognito=incognito,
        memory_enabled=memory_enabled,
        mode=mode,
        maximum_candidates=maximum_candidates,
        source_content_hash=ExtractionRequest.content_hash(text),
    )
    return request, context


def run_text(extraction, harness, text: str, *, message_id: str, **kwargs):
    request, context = extraction_input(
        harness,
        text,
        message_id=message_id,
        **kwargs,
    )
    return extraction.process(request, context)


def sql_state(path):
    if not path.exists():
        return {
            "records": [],
            "candidates": [],
            "sources": [],
            "relations": [],
            "operations": [],
            "outbox": [],
        }
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        queries = {
            "records": (
                "SELECT id, memory_type, domain_key, slot_key, canonical_payload, display_text, "
                "status, revision FROM memory_records ORDER BY created_at, id"
            ),
            "candidates": (
                "SELECT id, memory_type, domain_key, slot_key, canonical_payload, display_text, "
                "sensitivity, state, decision_outcome, applied_operation_id, extractor_name, "
                "raw_output_hash, source_spans_json, grounding_evidence_json FROM "
                "memory_candidates ORDER BY created_at, id"
            ),
            "sources": (
                "SELECT id, memory_id, assertion_role, message_id, source_span_json, is_active "
                "FROM memory_sources ORDER BY created_at, id"
            ),
            "relations": (
                "SELECT relation_type, from_memory_id, to_memory_id FROM memory_relations "
                "ORDER BY created_at, id"
            ),
            "operations": (
                "SELECT id, operation_kind, outcome FROM memory_operations ORDER BY created_at, id"
            ),
            "outbox": (
                "SELECT event_kind, memory_id, canonical_revision FROM memory_outbox "
                "ORDER BY created_at, id"
            ),
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "memory_records" not in tables:
            return {name: [] for name in queries}
        return {
            name: [dict(row) for row in connection.execute(query).fetchall()]
            for name, query in queries.items()
        }
    finally:
        connection.close()

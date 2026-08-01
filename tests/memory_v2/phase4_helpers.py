from __future__ import annotations

import sqlite3
from dataclasses import replace

from app.services.memory_v2.adapters import ChatMemoryV2Adapter
from app.services.memory_v2.extraction_contracts import ExtractionMode, ExtractionRequest
from app.services.memory_v2.extraction_coordinator import MemoryV2ExtractionCoordinator
from app.services.memory_v2.extraction_diagnostics import InMemoryExtractionDiagnostics
from tests.memory_v2.phase3_helpers import phase3_harness


def phase4_harness(tmp_path, *, model=None, guest=False):
    harness = phase3_harness(tmp_path, guest=guest)
    harness.coordinator.flags = replace(
        harness.coordinator.flags,
        extraction_enabled=True,
        foreground_commands_enabled=True,
        post_turn_extraction_enabled=True,
    )
    diagnostics = InMemoryExtractionDiagnostics()
    extraction = MemoryV2ExtractionCoordinator(
        ChatMemoryV2Adapter(harness.coordinator),
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
                "status, revision FROM memory_records_v2 ORDER BY created_at, id"
            ),
            "candidates": (
                "SELECT id, memory_type, domain_key, slot_key, canonical_payload, display_text, "
                "sensitivity, state, decision_outcome, applied_operation_id, extractor_name, "
                "raw_output_hash, source_spans_json, grounding_evidence_json FROM "
                "memory_candidates_v2 ORDER BY created_at, id"
            ),
            "sources": (
                "SELECT id, memory_id, assertion_role, message_id, source_span_json, is_active "
                "FROM memory_sources_v2 ORDER BY created_at, id"
            ),
            "relations": (
                "SELECT relation_type, from_memory_id, to_memory_id FROM memory_relations_v2 "
                "ORDER BY created_at, id"
            ),
            "operations": (
                "SELECT id, operation_kind, outcome FROM memory_operations_v2 "
                "ORDER BY created_at, id"
            ),
            "outbox": (
                "SELECT event_kind, memory_id, canonical_revision FROM memory_outbox_v2 "
                "ORDER BY created_at, id"
            ),
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "memory_records_v2" not in tables:
            return {name: [] for name in queries}
        return {
            name: [dict(row) for row in connection.execute(query).fetchall()]
            for name, query in queries.items()
        }
    finally:
        connection.close()

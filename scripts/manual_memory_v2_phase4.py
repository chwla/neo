#!/usr/bin/env python3
"""Manual Phase 4 extraction validation against disposable profile databases only."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from app.services.memory_v2.adapters import (
    CandidateReviewV2Adapter,
    ChatMemoryV2Adapter,
    MemoryV2AdapterContext,
)
from app.services.memory_v2.contracts import ActorKind, SourceKind
from app.services.memory_v2.coordinator import (
    MemoryV2ExecutionContext,
    MemoryV2MutationCoordinator,
)
from app.services.memory_v2.disposable_crypto import DisposableMemoryCrypto
from app.services.memory_v2.extraction import (
    ExtractionModelTimeout,
    FixtureExtractionModel,
    OllamaProbeResult,
    OllamaRequestMode,
    build_extraction_model_provider,
    probe_ollama_provider,
)
from app.services.memory_v2.extraction_contracts import (
    ConversationRole,
    ExtractionMode,
    ExtractionRequest,
    TrustedConversationMessage,
)
from app.services.memory_v2.extraction_coordinator import MemoryV2ExtractionCoordinator
from app.services.memory_v2.extraction_diagnostics import InMemoryExtractionDiagnostics
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.policy import classify_sensitivity

OWNER = "00000000-0000-4000-8000-000000000001"
OLD_GOAL = "create long-form cinematic YouTube videos"
NEW_GOAL = "create short Instagram reels clearly"
CRITICAL = (
    "I no longer want to make long-form cinematic YouTube videos. "
    "I want to create short Instagram reels clearly."
)
SENSITIVE_SENTINEL = "-".join(("P4", "SENSITIVE", "C71A9E4D0B62"))
PROHIBITED_SENTINEL = "-".join(("P4", "PROHIBITED", "D84F2A710C95"))
SENSITIVE_TEXT = f"Remember that my diagnosis is {SENSITIVE_SENTINEL}."
PROHIBITED_TEXT = f"Remember that my password is {PROHIBITED_SENTINEL}."


@dataclass(frozen=True)
class FixtureAudit:
    database_path: Path
    sensitive_payload_encrypted: bool
    category_reconfirm_suppressed_ids: tuple[str, ...]
    ambiguous_conflict_suppression_authorized: bool


@dataclass(frozen=True)
class LiveValidation:
    database_path: Path
    passed: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Retain disposable artifacts.")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--probe-live-model", action="store_true")
    parser.add_argument(
        "--provider",
        choices=("direct_json", "ollama"),
        default=os.environ.get("NEO_MEMORY_V2_EXTRACTION_PROVIDER", ""),
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("NEO_MEMORY_V2_EXTRACTION_ENDPOINT", "")
    )
    parser.add_argument("--model", default=os.environ.get("NEO_MEMORY_V2_EXTRACTION_MODEL", ""))
    parser.add_argument("--connect-timeout-seconds", type=int, default=5)
    parser.add_argument("--model-timeout-seconds", type=int, default=120)
    parser.add_argument("--warmup-timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--ollama-request-mode",
        choices=tuple(item.value for item in OllamaRequestMode),
        default=OllamaRequestMode.AUTO.value,
    )
    parser.add_argument("--token-env", default="NEO_MEMORY_V2_EXTRACTION_TOKEN")
    parser.add_argument("--confirm-disposable-live-model", action="store_true")
    return parser


def _harness(
    root: Path,
    profile_id: str,
    *,
    live_endpoint: str = "",
    live_model: str = "",
    live_provider: str = "",
    connect_timeout_seconds: int = 5,
    response_timeout_seconds: int = 120,
    warmup_timeout_seconds: int = 300,
    ollama_request_mode: str = OllamaRequestMode.AUTO.value,
):
    database_path = root / profile_id / "neo.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    crypto = DisposableMemoryCrypto(seed=b"phase4-manual-disposable-seed-material")
    flags = MemoryV2FeatureFlags(
        schema_enabled=True,
        canonical_writes=True,
        enabled_owner_ids=frozenset({OWNER}),
        disposable_database_root=str(root),
        extraction_enabled=True,
        foreground_commands_enabled=True,
        post_turn_extraction_enabled=True,
        live_extraction_model_enabled=bool(live_endpoint and live_model),
        extraction_provider=live_provider,
        extraction_endpoint=live_endpoint,
        extraction_model=live_model,
        extraction_connect_timeout_seconds=connect_timeout_seconds,
        extraction_response_timeout_seconds=response_timeout_seconds,
        extraction_warmup_timeout_seconds=warmup_timeout_seconds,
        ollama_request_mode=ollama_request_mode,
    )
    coordinator = MemoryV2MutationCoordinator(
        flags=flags,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
    )
    context = MemoryV2AdapterContext(
        execution=MemoryV2ExecutionContext(
            owner_id=OWNER,
            database_identity=f"account-profile:{profile_id}",
            database_url=f"sqlite:///{database_path}",
            profile_id=profile_id,
            disposable=True,
        ),
        actor_kind=ActorKind.USER,
        actor_id="manual-phase4-user",
        source_kind=SourceKind.AUTOMATIC_EXTRACTION,
        source_id="manual-phase4",
        session_id="manual-phase4-session",
        conversation_id="manual-phase4-conversation",
    )
    return database_path, coordinator, context


def _print_probe(probe: OllamaProbeResult) -> None:
    print(f"provider_reachable={str(probe.provider_reachable).lower()}")
    print(f"model_available={str(probe.model_available).lower()}")
    print(f"warmup_success={str(probe.warmup_success).lower()}")
    print(f"warmup_latency_ms={probe.warmup_latency_ms}")
    print(f"ollama_version={probe.ollama_version or 'null'}")
    print(f"schema_format_supported={str(probe.capabilities.schema_format_supported).lower()}")
    print(f"json_format_supported={str(probe.capabilities.json_format_supported).lower()}")
    print(f"think_field_supported={str(probe.capabilities.think_field_supported).lower()}")
    print(f"seed_option_supported={str(probe.capabilities.seed_option_supported).lower()}")
    print(
        "num_predict_option_supported="
        f"{str(probe.capabilities.num_predict_option_supported).lower()}"
    )
    print(f"keep_alive_supported={str(probe.capabilities.keep_alive_supported).lower()}")
    print(
        "selected_request_mode="
        + (probe.selected_request_mode.value if probe.selected_request_mode else "null")
    )
    print(f"sanitized_failure_code={probe.sanitized_failure_code or 'null'}")
    print(f"sanitized_failure_message={probe.sanitized_error_message or 'null'}")


def _probe_live_model(args) -> OllamaProbeResult:
    if args.provider != "ollama":
        raise RuntimeError("provider_probe_requires_ollama")
    if not args.endpoint.strip() or not args.model.strip():
        raise RuntimeError("live_model_endpoint_and_model_required")
    token = os.environ.get(args.token_env) or None
    probe = probe_ollama_provider(
        args.endpoint,
        model=args.model,
        connect_timeout_seconds=getattr(args, "connect_timeout_seconds", 5),
        response_timeout_seconds=getattr(args, "model_timeout_seconds", 120),
        warmup_timeout_seconds=getattr(args, "warmup_timeout_seconds", 300),
        requested_mode=getattr(args, "ollama_request_mode", OllamaRequestMode.AUTO.value),
        bearer_token=token,
    )
    _print_probe(probe)
    return probe


def _span(text: str, value: str, message_id: str) -> dict[str, object]:
    start = text.index(value)
    return {
        "message_id": message_id,
        "start": start,
        "end": start + len(value),
        "quoted_text": value,
    }


def _assertion(
    text: str,
    value: str,
    message_id: str,
    *,
    proposal_id: str,
    memory_type: str = "knowledge",
    domain: str = "software_development",
    slot: str | None = None,
    sensitivity: str = "normal",
) -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "source_spans": [_span(text, value, message_id)],
        "subject_hint": "user",
        "memory_type_hint": memory_type,
        "domain_hint": domain,
        "slot_hint": slot,
        "typed_value": value,
        "display_hint": value,
        "durability": "durable",
        "confidence": 0.96,
        "sensitivity_hint": sensitivity,
    }


def _response(*assertions, retractions=(), exclusions=()) -> dict[str, object]:
    return {
        "schema_version": 1,
        "assertions": list(assertions),
        "retractions": list(retractions),
        "exclusions": list(exclusions),
    }


def _fixtures() -> dict[str, object]:
    stable = "I use Python for work."
    assistant_user = "Okay."
    assistant_source = "You prefer Python."
    sensitive = SENSITIVE_TEXT
    cap_facts = [f"I use Python tool {index}" for index in range(6)]
    cap_text = "; ".join(cap_facts)
    ambiguous = "Now I want to create travel videos."
    sync_text = "I use Rust for work."
    category = "That is a goal, not a preference."
    critical_value_start = CRITICAL.index(NEW_GOAL)
    category_fixture = _assertion(
        CRITICAL,
        NEW_GOAL,
        "critical-correction",
        proposal_id="category-goal",
        memory_type="goal",
        domain="video_creation",
        slot="current_primary_goal",
    )
    category_fixture["source_spans"].append(
        {
            "message_id": "category-correction",
            "start": 0,
            "end": len(category),
            "quoted_text": category,
        }
    )
    category_fixture["source_spans"][0] = {
        "message_id": "critical-correction",
        "start": critical_value_start,
        "end": critical_value_start + len(NEW_GOAL),
        "quoted_text": NEW_GOAL,
    }
    return {
        stable: _response(
            _assertion(stable, "I use Python", "stable-fact", proposal_id="stable-python")
        ),
        category: _response(category_fixture),
        assistant_user: _response(
            {
                **_assertion(
                    assistant_source,
                    "prefer Python",
                    "assistant-source",
                    proposal_id="assistant-claim",
                    memory_type="preference",
                    domain="software_development",
                    slot="language",
                )
            }
        ),
        "Malformed fixture output.": "not-json",
        "Timeout fixture output.": ExtractionModelTimeout("model_timeout"),
        sensitive: _response(
            _assertion(
                sensitive,
                f"my diagnosis is {SENSITIVE_SENTINEL}",
                "sensitive-fact",
                proposal_id="sensitive-fact",
                domain="health_fitness",
            )
        ),
        cap_text: _response(
            *(
                _assertion(
                    cap_text,
                    fact,
                    "candidate-cap",
                    proposal_id=f"cap-{index}",
                )
                for index, fact in enumerate(cap_facts)
            )
        ),
        ambiguous: {
            "schema_version": 1,
            "assertions": [],
            "unexpected": "invalid schema for grounded review fallback",
        },
        sync_text: _response(
            _assertion(sync_text, "I use Rust", "sync-stream", proposal_id="sync-rust")
        ),
    }


def _request(context, text: str, message_id: str, *, mode, explicit=False, window=(), limit=4):
    effective = replace(context, message_id=message_id)
    request = ExtractionRequest(
        request_id=f"manual-{message_id}",
        owner_id=OWNER,
        conversation_id=effective.conversation_id,
        session_id=effective.session_id,
        message_id=message_id,
        user_message=text,
        supporting_window=window,
        explicit_memory_intent=explicit,
        mode=mode,
        maximum_candidates=limit,
        source_content_hash=ExtractionRequest.content_hash(text),
    )
    return request, effective


def _inspect(path: Path) -> dict[str, list[dict[str, object]]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        queries = {
            "records": (
                "SELECT id, memory_type, domain_key, slot_key, canonical_payload, display_text, "
                "status, revision FROM memory_records_v2 ORDER BY created_at, id"
            ),
            "candidates": (
                "SELECT id, memory_type, domain_key, slot_key, state, decision_outcome, "
                "applied_operation_id, extractor_name, raw_output_hash FROM memory_candidates_v2 "
                "ORDER BY created_at, id"
            ),
            "relations": "SELECT * FROM memory_relations_v2 ORDER BY created_at, id",
            "sources": (
                "SELECT id, memory_id, assertion_role, message_id, source_span_json, is_active "
                "FROM memory_sources_v2 ORDER BY created_at, id"
            ),
            "operations": (
                "SELECT id, operation_kind, outcome, status FROM memory_operations_v2 "
                "ORDER BY created_at, id"
            ),
            "outbox": (
                "SELECT event_kind, memory_id, canonical_revision, state FROM memory_outbox_v2 "
                "ORDER BY created_at, id"
            ),
        }
        return {
            name: [dict(row) for row in connection.execute(query).fetchall()]
            for name, query in queries.items()
        }
    finally:
        connection.close()


def _sensitive_payload_is_encrypted(
    path: Path,
    *,
    memory_id: str,
    candidate_id: str,
    operation_id: str,
) -> bool:
    with sqlite3.connect(path) as connection:
        record = connection.execute(
            "SELECT canonical_payload, display_text, encrypted_canonical_payload, "
            "encrypted_display_payload FROM memory_records_v2 WHERE id = ?",
            (memory_id,),
        ).fetchone()
        candidate = connection.execute(
            "SELECT canonical_payload, display_text, encrypted_canonical_payload, "
            "encrypted_display_payload, target_hints_json, raw_output_hash "
            "FROM memory_candidates_v2 WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        operation = connection.execute(
            "SELECT normalized_command_json, encrypted_command_payload "
            "FROM memory_operations_v2 WHERE id = ?",
            (operation_id,),
        ).fetchone()
        sources = connection.execute(
            "SELECT redacted_excerpt, encrypted_excerpt FROM memory_sources_v2 WHERE memory_id = ?",
            (memory_id,),
        ).fetchall()
    return bool(
        record
        and record[0] is None
        and record[1] is None
        and record[2]
        and record[3]
        and candidate
        and candidate[0] is None
        and candidate[1] is None
        and candidate[2]
        and candidate[3]
        and SENSITIVE_SENTINEL not in str(candidate[4])
        and candidate[5] is None
        and operation
        and operation[0] is None
        and operation[1]
        and sources
        and all(redacted is None and encrypted for redacted, encrypted in sources)
    )


def _artifact_plaintext_count(root: Path, sentinel: str) -> int:
    needle = sentinel.encode("utf-8")
    count = 0
    for path in root.rglob("*"):
        if path.is_file():
            count += path.read_bytes().count(needle)
    return count


def _print_scenario(label: str, text: str, result, *, redact_source: bool = False) -> None:
    payload = result.model_dump(mode="json")
    print(f"scenario={label}")
    redact_source = redact_source or classify_sensitivity(text).value != "normal"
    printable_source = "<redacted sensitive input>" if redact_source else text
    print(f"user_source={json.dumps(printable_source, ensure_ascii=False)}")
    for key in (
        "preparse",
        "model_summary",
        "grounding",
        "decisions",
        "current_turn_override",
        "diagnostic",
    ):
        print(f"{key}={json.dumps(payload[key], sort_keys=True, ensure_ascii=False)}")


def _run_fixture(root: Path) -> FixtureAudit:
    path, coordinator, context = _harness(root, "fixture")
    model = FixtureExtractionModel(_fixtures())
    diagnostics = InMemoryExtractionDiagnostics()
    review_adapter = CandidateReviewV2Adapter(coordinator)
    extraction = MemoryV2ExtractionCoordinator(
        ChatMemoryV2Adapter(coordinator),
        model=model,
        diagnostics=diagnostics,
    )

    def run(
        label,
        text,
        message_id,
        *,
        mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
        redact_source=False,
        **kwargs,
    ):
        request, effective = _request(context, text, message_id, mode=mode, **kwargs)
        result = extraction.process(request, effective)
        _print_scenario(label, text, result, redact_source=redact_source)
        return result

    stable = run(
        "stable_fact_creation",
        "I use Python for work.",
        "stable-fact",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert stable.status.value == "applied"
    run("initial_video_goal", f"I want to {OLD_GOAL}.", "initial-video")
    critical = run("critical_implicit_video_correction", CRITICAL, "critical-correction")
    assert critical.decisions[0].action.value == "replace"
    category = run(
        "category_correction",
        "That is a goal, not a preference.",
        "category-correction",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        window=(
            TrustedConversationMessage(
                message_id="critical-correction",
                role=ConversationRole.USER,
                content=CRITICAL,
            ),
        ),
    )
    assert category.decisions[0].outcome == "reconfirmed"
    assert category.current_turn_override.suppressed_memory_ids == ()
    run(
        "domain_specific_preference",
        "For video-editing advice, give me quick 15-minute drills.",
        "scoped-style",
    )
    run("global_response_style", "Always answer me concisely.", "global-style")

    location = run("current_location_creation", "I live in Pune.", "location-create")
    assert location.decisions[0].outcome == "created"
    retraction = run("pure_retraction", "I no longer live in Pune.", "pure-retraction")
    assert retraction.decisions[0].outcome == "archived"
    run(
        "additive_language",
        "I still want edit YouTube videos, and I also want create Instagram reels.",
        "additive",
    )
    run(
        "not_only_boundary",
        "I prefer project-based learning, not only video courses.",
        "not-only",
    )
    temporary = run(
        "temporary_rejection",
        "I am drinking coffee right now and I have a headache.",
        "temporary",
    )
    assert temporary.status.value == "no_action"
    third_party = run("third_party_rejection", "My friend prefers Rust.", "third-party")
    assert third_party.status.value == "no_action"
    assistant = run(
        "assistant_statement_rejection",
        "Okay.",
        "assistant-user",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        window=(
            TrustedConversationMessage(
                message_id="assistant-source",
                role=ConversationRole.ASSISTANT,
                content="You prefer Python.",
            ),
        ),
    )
    assert assistant.status.value == "rejected"
    malformed = run(
        "malformed_output",
        "Malformed fixture output.",
        "malformed",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert malformed.status.value == "failed"
    timeout = run(
        "model_timeout",
        "Timeout fixture output.",
        "timeout",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert timeout.status.value == "failed"
    sensitive = run(
        "sensitive_explicit_request",
        SENSITIVE_TEXT,
        "sensitive-fact",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
        redact_source=True,
    )
    assert sensitive.status.value == "applied"
    assert sensitive.current_turn_override.positive_current_assertion is None
    assert sensitive.current_turn_override.redacted_current_assertion == "[sensitive memory]"
    before_prohibited = _inspect(path)
    before_model_calls = model.call_count
    prohibited = run(
        "prohibited_content",
        PROHIBITED_TEXT,
        "prohibited",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
        redact_source=True,
    )
    assert prohibited.status.value == "rejected"
    assert model.call_count == before_model_calls
    assert _inspect(path) == before_prohibited
    assert prohibited.current_turn_override.positive_current_assertion is None
    assert prohibited.current_turn_override.contradicted_memory_ids == ()
    assert prohibited.current_turn_override.contradicted_slot_keys == ()
    assert not prohibited.current_turn_override.contradiction_deterministic
    cap_facts = [f"I use Python tool {index}" for index in range(6)]
    cap_text = "; ".join(cap_facts)
    capped = run(
        "automatic_candidate_cap",
        cap_text,
        "candidate-cap",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert capped.model_summary.capped_count == 2
    batch_lines = [f"I use Python library {index}" for index in range(6)]
    batch_text = "Remember these 6 facts:\n" + "\n".join(f"- {item}" for item in batch_lines)
    batch = run(
        "explicit_batch",
        batch_text,
        "explicit-batch",
        mode=ExtractionMode.EXPLICIT_BATCH,
        explicit=True,
        limit=10,
    )
    assert len(batch.decisions) == 6
    ambiguous = run(
        "ambiguous_correction_review",
        "Now I want to create travel videos.",
        "ambiguous-correction",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert ambiguous.status.value == "needs_review"
    assert ambiguous.current_turn_override.suppressed_memory_ids == ()
    assert ambiguous.current_turn_override.candidate_target_memory_ids == ()
    assert not ambiguous.current_turn_override.contradiction_deterministic
    review_decision = ambiguous.decisions[0]
    assert review_decision.proposed_memory_type == "goal"
    assert review_decision.proposed_domain_hint is None
    assert review_decision.proposed_slot_hint is None
    assert review_decision.domain_unresolved
    assert review_decision.slot_unresolved
    assert review_decision.model_failure_reason == "invalid_model_schema"
    assert review_decision.candidate_id is not None
    review_status = review_adapter.candidate_status(context, review_decision.candidate_id)
    assert review_status is not None and review_status.state.value == "needs_review"
    sync_request, sync_context = _request(
        context,
        "I use Rust for work.",
        "sync-stream",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    sync = extraction.process(sync_request, sync_context, transport="sync")
    stream = extraction.process(sync_request, sync_context, transport="stream")
    _print_scenario("sync_extraction", "I use Rust for work.", sync)
    _print_scenario("stream_idempotent_replay", "I use Rust for work.", stream)
    assert stream.decisions[0].operation_id == sync.decisions[0].operation_id
    incognito_request, incognito_context = _request(
        replace(context, execution=replace(context.execution, is_incognito=True)),
        "I use Go for work.",
        "incognito",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    incognito_request = incognito_request.model_copy(update={"incognito": True})
    before_calls = model.call_count
    incognito = extraction.process(incognito_request, incognito_context)
    _print_scenario("incognito_zero_call", "I use Go for work.", incognito)
    assert incognito.status.value == "disabled" and model.call_count == before_calls

    state = _inspect(path)
    active_video = [
        row
        for row in state["records"]
        if row["status"] == "active"
        and row["slot_key"] == "goal:video_creation:current_primary_goal"
    ]
    assert len(active_video) == 1
    assert active_video[0]["canonical_payload"] == f'"{NEW_GOAL}"'
    assert active_video[0]["domain_key"] == "video_creation"
    assert any(row["state"] == "needs_review" for row in state["candidates"])
    sensitive_decision = sensitive.decisions[0]
    assert sensitive_decision.candidate_id is not None
    assert sensitive_decision.operation_id is not None
    assert sensitive_decision.memory_ids
    sensitive_payload_encrypted = _sensitive_payload_is_encrypted(
        path,
        memory_id=str(sensitive_decision.memory_ids[0]),
        candidate_id=str(sensitive_decision.candidate_id),
        operation_id=str(sensitive_decision.operation_id),
    )
    assert sensitive_payload_encrypted
    for name, rows in state.items():
        print(f"persisted_{name}={json.dumps(rows, sort_keys=True, default=str)}")
    print(
        "extraction_diagnostics="
        + json.dumps(
            [item.model_dump(mode="json") for item in diagnostics.snapshot()],
            sort_keys=True,
        )
    )
    print("phase4_fixture_validation=PASS")
    return FixtureAudit(
        database_path=path,
        sensitive_payload_encrypted=sensitive_payload_encrypted,
        category_reconfirm_suppressed_ids=tuple(
            str(item) for item in category.current_turn_override.suppressed_memory_ids
        ),
        ambiguous_conflict_suppression_authorized=bool(
            ambiguous.current_turn_override.suppressed_memory_ids
        ),
    )


def _run_interactive(root: Path, args) -> LiveValidation:
    if not args.live_model:
        raise RuntimeError("interactive_mode_requires_live_model")
    if not args.confirm_disposable_live_model:
        raise RuntimeError("live_model_requires_explicit_disposable_confirmation")
    if not args.endpoint.strip() or not args.model.strip():
        raise RuntimeError("live_model_endpoint_and_model_required")
    if not args.provider:
        raise RuntimeError("live_model_requires_explicit_provider")
    token = os.environ.get(args.token_env) or None
    probe = _probe_live_model(args) if args.provider == "ollama" else None
    if probe is not None and not probe.successful:
        raise RuntimeError(
            f"live_model_probe_failed:{probe.sanitized_failure_code or 'unknown_provider_failure'}"
        )
    path, coordinator, context = _harness(
        root,
        "interactive",
        live_endpoint=args.endpoint,
        live_model=args.model,
        live_provider=args.provider,
        connect_timeout_seconds=getattr(args, "connect_timeout_seconds", 5),
        response_timeout_seconds=getattr(args, "model_timeout_seconds", 120),
        warmup_timeout_seconds=getattr(args, "warmup_timeout_seconds", 300),
        ollama_request_mode=(
            probe.selected_request_mode.value
            if probe is not None and probe.selected_request_mode is not None
            else getattr(args, "ollama_request_mode", OllamaRequestMode.AUTO.value)
        ),
    )
    model = build_extraction_model_provider(
        args.provider,
        args.endpoint,
        model=args.model,
        connect_timeout_seconds=coordinator.flags.extraction_connect_timeout_seconds,
        response_timeout_seconds=coordinator.flags.extraction_response_timeout_seconds,
        bearer_token=token,
        ollama_request_mode=(
            probe.selected_request_mode if probe is not None else OllamaRequestMode.SCHEMA
        ),
        ollama_capabilities=(probe.capabilities if probe is not None else None),
    )
    diagnostics = InMemoryExtractionDiagnostics()
    extraction = MemoryV2ExtractionCoordinator(
        ChatMemoryV2Adapter(coordinator), model=model, diagnostics=diagnostics
    )
    results = []

    def run(label, text, message_id, *, model_override=None, **kwargs):
        request, effective = _request(
            context,
            text,
            message_id,
            mode=ExtractionMode.POST_TURN_AUTOMATIC,
            **kwargs,
        )
        runner = extraction
        if model_override is not None:
            runner = MemoryV2ExtractionCoordinator(
                ChatMemoryV2Adapter(coordinator),
                model=model_override,
                diagnostics=diagnostics,
            )
        result = runner.process(request, effective)
        results.append(result)
        _print_scenario(label, text, result)
        if result.diagnostic.provider_kind == args.provider:
            safe_provider_message = getattr(model, "last_sanitized_error_message", None)
            if safe_provider_message:
                print(f"sanitized_provider_error_message={safe_provider_message}")
        return result

    captured = io.StringIO()
    with redirect_stdout(captured):
        stable = run(
            "live_model_required_stable_fact",
            "I use Python for work.",
            "live-stable-fact",
        )
        prior = f"I want to {NEW_GOAL}."
        created_goal = run("live_category_prior_goal", prior, "live-category-prior")
        category = run(
            "live_contextual_category_correction",
            "What I said is a goal, not a preference.",
            "live-category-current",
            window=(
                TrustedConversationMessage(
                    message_id="live-category-prior",
                    role=ConversationRole.USER,
                    content=prior,
                ),
            ),
        )
        location = run(
            "live_current_location_creation",
            "I live in Pune.",
            "live-location-create",
        )
        retraction = run(
            "live_current_location_retraction",
            "I no longer live in Pune.",
            "live-location-retract",
        )
        ambiguous_text = "Now I want to create travel videos."
        ambiguous = run(
            "live_ambiguous_goal_review",
            ambiguous_text,
            "live-ambiguous-goal",
            model_override=FixtureExtractionModel(
                {
                    ambiguous_text: {
                        "schema_version": 1,
                        "assertions": [],
                        "unexpected": True,
                    }
                }
            ),
        )
        invalid_text = "Invalid schema safety case."
        invalid = run(
            "live_invalid_schema_safety",
            invalid_text,
            "live-invalid-schema",
            model_override=FixtureExtractionModel(
                {
                    invalid_text: {
                        "schema_version": 1,
                        "assertions": [],
                        "unexpected": True,
                    }
                }
            ),
        )
        sensitive = run(
            "live_sensitive_leak_safety",
            f"My diagnosis is {SENSITIVE_SENTINEL}.",
            "live-sensitive",
            explicit=True,
        )
        calls_before_prohibited = model.call_count
        prohibited = run(
            "live_prohibited_zero_call",
            f"My password is {PROHIBITED_SENTINEL}.",
            "live-prohibited",
            explicit=True,
        )
    rendered = captured.getvalue()
    sensitive_leaks = rendered.count(SENSITIVE_SENTINEL) + _artifact_plaintext_count(
        root, SENSITIVE_SENTINEL
    )
    prohibited_leaks = rendered.count(PROHIBITED_SENTINEL) + _artifact_plaintext_count(
        root, PROHIBITED_SENTINEL
    )
    if sensitive_leaks or prohibited_leaks:
        raise RuntimeError("live_model_sensitive_or_prohibited_plaintext_leak")
    print(rendered, end="")

    state = _inspect(path)
    category_target = created_goal.decisions[0].memory_ids[0]
    location_target = location.decisions[0].memory_ids[0]
    review_ids = {row["id"] for row in state["candidates"] if row["state"] == "needs_review"}
    mandatory_passed = all(
        (
            stable.status.value == "applied",
            stable.diagnostic.schema_validation_result == "valid",
            category.decisions
            and category.decisions[0].action.value == "reconfirm"
            and category.decisions[0].outcome == "reconfirmed",
            category.current_turn_override.candidate_target_memory_ids == (category_target,),
            category.current_turn_override.suppressed_memory_ids == (),
            location.status.value == "applied",
            retraction.decisions and retraction.decisions[0].outcome == "archived",
            retraction.current_turn_override.suppressed_memory_ids == (location_target,),
            ambiguous.status.value == "needs_review",
            ambiguous.decisions
            and ambiguous.decisions[0].candidate_id is not None
            and str(ambiguous.decisions[0].candidate_id) in review_ids,
            ambiguous.current_turn_override.suppressed_memory_ids == (),
            invalid.status.value == "failed",
            not invalid.decisions,
            sensitive.current_turn_override.positive_current_assertion is None,
            prohibited.status.value == "rejected",
            model.call_count == calls_before_prohibited,
            sensitive_leaks == 0,
            prohibited_leaks == 0,
        )
    )
    print(f"interactive_disposable_database={path}")
    if mandatory_passed:
        print("Mandatory live scenarios passed. Enter a user message or :quit.")
    else:
        print("Mandatory live scenarios failed; interactive continuation is disabled.")
    while True:
        if not mandatory_passed:
            break
        try:
            text = input("phase4> ").strip()
        except EOFError:
            break
        if not text:
            continue
        if text == ":quit":
            break
        if text == ":reset":
            path, coordinator, context = _harness(
                root,
                f"interactive-{uuid4()}",
                live_endpoint=args.endpoint,
                live_model=args.model,
                live_provider=args.provider,
                connect_timeout_seconds=getattr(args, "connect_timeout_seconds", 5),
                response_timeout_seconds=getattr(args, "model_timeout_seconds", 120),
                warmup_timeout_seconds=getattr(args, "warmup_timeout_seconds", 300),
                ollama_request_mode=(
                    probe.selected_request_mode.value
                    if probe is not None and probe.selected_request_mode is not None
                    else getattr(args, "ollama_request_mode", OllamaRequestMode.AUTO.value)
                ),
            )
            extraction = MemoryV2ExtractionCoordinator(
                ChatMemoryV2Adapter(coordinator), model=model
            )
            print(f"interactive_disposable_database={path}")
            continue
        message_id = f"interactive-{uuid4()}"
        request, effective = _request(
            context,
            text,
            message_id,
            mode=ExtractionMode.POST_TURN_AUTOMATIC,
        )
        result = extraction.process(request, effective)
        results.append(result)
        _print_scenario("interactive", text, result)
        for name, rows in _inspect(path).items():
            print(f"persisted_{name}={json.dumps(rows, sort_keys=True, default=str)}")
    live_diagnostics = tuple(
        item for item in diagnostics.snapshot() if item.provider_kind == args.provider
    )
    live_results = tuple(item for item in results if item.diagnostic.provider_kind == args.provider)
    transport_successes = sum(
        item.http_status is not None
        and 200 <= item.http_status < 300
        and item.content_present is True
        and item.response_envelope_shape in {"ollama_chat_v1", "direct_schema_body_v1"}
        for item in live_diagnostics
    )
    transport_failures = len(live_diagnostics) - transport_successes
    valid_schema_count = sum(item.schema_validation_result == "valid" for item in live_diagnostics)
    invalid_schema_count = sum(
        item.schema_validation_result == "invalid" for item in live_diagnostics
    )
    deterministic_applied_count = sum(
        item.status.value == "applied" and not item.model_summary.called for item in results
    )
    live_applied_count = sum(
        item.status.value == "applied" and item.diagnostic.schema_validation_result == "valid"
        for item in live_results
    )
    live_review_count = sum(
        item.status.value == "needs_review" and item.diagnostic.schema_validation_result == "valid"
        for item in live_results
    )
    mandatory_passed = bool(
        mandatory_passed
        and model.call_count > 0
        and transport_successes > 0
        and valid_schema_count > 0
        and (live_applied_count > 0 or live_review_count > 0)
    )
    print(f"deterministic_applied_count={deterministic_applied_count}")
    print(f"live_model_call_count={model.call_count}")
    print(f"live_model_transport_success_count={transport_successes}")
    print("live_model_valid_schema_count=" + str(valid_schema_count))
    print("live_model_invalid_schema_count=" + str(invalid_schema_count))
    print(f"live_model_transport_failure_count={transport_failures}")
    print(f"live_model_applied_count={live_applied_count}")
    print(f"live_model_review_count={live_review_count}")
    print(
        "live_model_no_action_count="
        + str(sum(item.status.value == "no_action" for item in live_results))
    )
    print("phase4_live_model_validation=" + ("PASS" if mandatory_passed else "FAIL"))
    return LiveValidation(database_path=path, passed=mandatory_passed)


def main() -> int:
    args = _parser().parse_args()
    if args.probe_live_model:
        try:
            probe = _probe_live_model(args)
        except Exception as exc:
            print(f"phase4_provider_probe=FAIL:{type(exc).__name__}:{exc}")
            return 1
        print("phase4_provider_probe=" + ("PASS" if probe.successful else "FAIL"))
        return 0 if probe.successful else 1
    root = Path(tempfile.mkdtemp(prefix="neo-memory-v2-phase4-"))
    print(f"disposable_root={root}")
    retained_path = None
    try:
        if args.interactive or args.live_model:
            live_validation = _run_interactive(root, args)
            retained_path = live_validation.database_path
            if not live_validation.passed:
                print(f"validated_database={retained_path}")
                print(f"artifacts_retained={root}")
                return 1
        else:
            captured = io.StringIO()
            with redirect_stdout(captured):
                audit = _run_fixture(root)
            rendered = captured.getvalue()
            prohibited_plaintext_leak_count = rendered.count(
                PROHIBITED_SENTINEL
            ) + _artifact_plaintext_count(root, PROHIBITED_SENTINEL)
            sensitive_plaintext_log_leak_count = rendered.count(SENSITIVE_SENTINEL)
            sensitive_plaintext_artifact_leak_count = _artifact_plaintext_count(
                root, SENSITIVE_SENTINEL
            )
            if prohibited_plaintext_leak_count:
                raise RuntimeError("prohibited_plaintext_leak_detected")
            if sensitive_plaintext_log_leak_count:
                raise RuntimeError("sensitive_plaintext_log_leak_detected")
            if sensitive_plaintext_artifact_leak_count:
                raise RuntimeError("sensitive_plaintext_artifact_leak_detected")
            if not audit.sensitive_payload_encrypted:
                raise RuntimeError("sensitive_payload_not_encrypted")
            if audit.category_reconfirm_suppressed_ids:
                raise RuntimeError("category_reconfirm_authorized_suppression")
            if audit.ambiguous_conflict_suppression_authorized:
                raise RuntimeError("ambiguous_conflict_authorized_suppression")
            print(rendered, end="")
            print(f"prohibited_plaintext_leak_count={prohibited_plaintext_leak_count}")
            print(f"sensitive_plaintext_log_leak_count={sensitive_plaintext_log_leak_count}")
            print(
                f"sensitive_plaintext_artifact_leak_count={sensitive_plaintext_artifact_leak_count}"
            )
            print(f"sensitive_payload_encrypted={str(audit.sensitive_payload_encrypted).lower()}")
            print(
                "category_reconfirm_suppressed_ids="
                + json.dumps(audit.category_reconfirm_suppressed_ids)
            )
            print(
                "ambiguous_conflict_suppression_authorized="
                f"{str(audit.ambiguous_conflict_suppression_authorized).lower()}"
            )
            retained_path = audit.database_path
    except Exception as exc:
        print(f"phase4_manual_validation=FAIL:{type(exc).__name__}:{exc}")
        print(f"artifacts_retained={root}")
        return 1
    print(f"validated_database={retained_path}")
    if args.keep:
        print(f"artifacts_retained={root}")
        print(f"cleanup_command=rm -rf -- {root}")
    else:
        shutil.rmtree(root)
        print("artifacts_cleaned=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

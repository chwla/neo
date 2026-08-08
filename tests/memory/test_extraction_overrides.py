from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.memory.adapters import (
    ChatMemoryAdapter,
    GenericMemoryAdapter,
    StructuredMemoryInput,
)
from app.services.memory.contracts import (
    CanonicalMemorySnapshot,
    MemoryLifecycleState,
    Sensitivity,
)
from app.services.memory.correction_resolver import CorrectionResolutionKind
from app.services.memory.extraction import (
    ExtractionModelTimeout,
    FixtureExtractionModel,
)
from app.services.memory.extraction_contracts import (
    CandidateAction,
    ConversationRole,
    ExtractionCandidateDecision,
    ExtractionMode,
    ExtractionStatus,
    TrustedConversationMessage,
)
from app.services.memory.extraction_coordinator import (
    CurrentTurnOverrideBuilder,
    MemoryExtractionCoordinator,
)
from app.services.memory.idempotency import MemoryIdempotency
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.extraction_helpers import (
    extraction_harness,
    extraction_input,
    run_text,
    sql_state,
)


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
    correction_group: str | None = None,
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
        "confidence": 0.98,
        "sensitivity_hint": "normal",
        "correction_group": correction_group,
    }


def _response(*assertions, retractions=()) -> dict[str, object]:
    return {
        "schema_version": 1,
        "assertions": list(assertions),
        "retractions": list(retractions),
        "exclusions": [],
    }


def _artifact_count(root: Path, sentinel: str) -> int:
    needle = sentinel.encode()
    return sum(path.read_bytes().count(needle) for path in root.rglob("*") if path.is_file())


def _assert_empty_override(result) -> None:
    override = result.current_turn_override
    assert override.positive_current_assertion is None
    assert override.redacted_current_assertion is None
    assert override.suppressed_memory_ids == ()
    assert override.suppressed_slot_keys == ()
    assert override.contradicted_memory_ids == ()
    assert override.contradicted_slot_keys == ()
    assert override.candidate_target_memory_ids == ()
    assert override.unresolved_conflict_slot_keys == ()
    assert not override.contradiction_deterministic
    assert not override.review_required


@pytest.mark.parametrize("kind", ["password", "token", "api_key"])
def test_prohibited_input_leaves_no_plaintext_or_durable_trace(
    tmp_path, capsys, caplog, kind
) -> None:
    sentinel = "-".join(("P4", kind.upper(), "8C13E0A6F74B"))
    source = {
        "password": f"Remember that my password is {sentinel}.",
        "token": f"Remember that my access token: {sentinel}.",
        "api_key": f"Remember that my API key is {sentinel}.",
    }[kind]
    model = FixtureExtractionModel({source: _response()})
    harness, extraction, diagnostics = extraction_harness(tmp_path / kind, model=model)
    caplog.set_level(logging.DEBUG)

    result = run_text(
        extraction,
        harness,
        source,
        message_id=f"prohibited-{kind}",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
    )

    assert result.status is ExtractionStatus.REJECTED
    assert result.preparse.sensitive_content_redacted
    assert result.preparse.assertions == result.preparse.retractions == ()
    assert result.diagnostic.reason_codes == ("prohibited_source_rejected_before_model",)
    assert result.current_turn_override.sensitivity is Sensitivity.PROHIBITED
    _assert_empty_override(result)
    assert model.call_count == 0
    assert all(not rows for rows in sql_state(harness.database_path).values())

    captured = capsys.readouterr()
    exposed = "\n".join(
        (
            captured.out,
            captured.err,
            caplog.text,
            result.model_dump_json(),
            str([item.model_dump(mode="json") for item in diagnostics.snapshot()]),
        )
    )
    assert sentinel not in exposed
    assert _artifact_count(harness.root, sentinel) == 0


def test_approved_sensitive_input_is_encrypted_and_result_safe(tmp_path, capsys, caplog) -> None:
    sentinel = "-".join(("P4", "SENSITIVE", "0D7A52B1C9E4"))
    source = f"Remember that my diagnosis is {sentinel}."
    value = f"my diagnosis is {sentinel}"
    model = FixtureExtractionModel(
        {source: _response(_assertion(source, value, "sensitive", proposal_id="sensitive"))}
    )
    harness, extraction, diagnostics = extraction_harness(tmp_path, model=model)
    caplog.set_level(logging.DEBUG)

    result = run_text(
        extraction,
        harness,
        source,
        message_id="sensitive",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
    )

    assert result.status is ExtractionStatus.APPLIED
    assert result.preparse.sensitive_content_redacted
    assert result.preparse.assertions == result.preparse.retractions == ()
    assert result.model_summary.raw_output_hash is None
    assert all(item.spans == () for item in result.grounding)
    override = result.current_turn_override
    assert override.sensitivity is Sensitivity.SENSITIVE
    assert override.positive_current_assertion is None
    assert override.redacted_current_assertion == "[sensitive memory]"
    assert override.suppressed_memory_ids == ()

    with sqlite3.connect(harness.database_path) as connection:
        record = connection.execute(
            "SELECT canonical_payload, display_text, encrypted_canonical_payload, "
            "encrypted_display_payload FROM memory_records"
        ).fetchone()
        candidate = connection.execute(
            "SELECT canonical_payload, display_text, encrypted_canonical_payload, "
            "encrypted_display_payload, target_hints_json, raw_output_hash "
            "FROM memory_candidates"
        ).fetchone()
        operation = connection.execute(
            "SELECT normalized_command_json, encrypted_command_payload FROM memory_operations"
        ).fetchone()
        sources = connection.execute(
            "SELECT redacted_excerpt, encrypted_excerpt FROM memory_sources"
        ).fetchall()
        outbox = connection.execute("SELECT event_payload_json FROM memory_outbox").fetchall()
    assert record[0] is record[1] is None and record[2] and record[3]
    assert candidate[0] is candidate[1] is None and candidate[2] and candidate[3]
    assert sentinel not in str(candidate[4]) and candidate[5] is None
    assert operation[0] is None and operation[1]
    assert sources and all(item[0] is None and item[1] for item in sources)
    assert sentinel not in str(outbox)

    decrypted = ChatMemoryAdapter(harness.coordinator).list_active_memories(harness.context)
    assert decrypted[0].canonical_value == value
    captured = capsys.readouterr()
    exposed = "\n".join(
        (
            captured.out,
            captured.err,
            caplog.text,
            result.model_dump_json(),
            str([item.model_dump(mode="json") for item in diagnostics.snapshot()]),
        )
    )
    assert sentinel not in exposed
    assert _artifact_count(harness.root, sentinel) == 0


def test_sensitive_correction_suppresses_only_encrypted_predecessor(tmp_path) -> None:
    old = "-".join(("P4", "MEDICAL", "OLD", "17C80F6A"))
    new = "-".join(("P4", "MEDICAL", "NEW", "B926D41E"))
    old_source = f"My diagnosis is {old}, and I explicitly want this remembered."
    new_source = f"My diagnosis changed from {old} to {new}."
    old_assertion = _assertion(
        old_source,
        old,
        "medical-old",
        proposal_id="medical-old",
        memory_type="identity",
        domain="global",
        slot="health_condition",
    )
    new_assertion = _assertion(
        new_source,
        new,
        "medical-new",
        proposal_id="medical-new",
        memory_type="identity",
        domain="global",
        slot="health_condition",
        correction_group="medical-change",
    )
    retraction = {
        "proposal_id": "medical-retraction",
        "source_spans": [_span(new_source, old, "medical-new")],
        "subject_hint": "user",
        "old_value_hint": old,
        "memory_type_hint": "identity",
        "domain_hint": "global",
        "slot_hint": "health_condition",
        "confidence": 0.99,
        "correction_group": "medical-change",
    }
    model = FixtureExtractionModel(
        {
            old_source: _response(old_assertion),
            new_source: _response(new_assertion, retractions=(retraction,)),
        }
    )
    harness, extraction, _diagnostics = extraction_harness(tmp_path, model=model)
    created = run_text(
        extraction,
        harness,
        old_source,
        message_id="medical-old",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
    )
    old_id = created.decisions[0].memory_ids[0]
    corrected = run_text(
        extraction,
        harness,
        new_source,
        message_id="medical-new",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
    )

    assert corrected.decisions[0].action is CandidateAction.REPLACE
    override = corrected.current_turn_override
    assert override.sensitivity is Sensitivity.SENSITIVE
    assert override.positive_current_assertion is None
    assert override.redacted_current_assertion == "[sensitive memory]"
    assert override.suppressed_memory_ids == (old_id,)
    assert override.candidate_target_memory_ids == (old_id,)
    assert override.contradiction_deterministic
    assert old not in corrected.model_dump_json()
    assert new not in corrected.model_dump_json()
    assert _artifact_count(harness.root, old) == 0
    assert _artifact_count(harness.root, new) == 0
    active = ChatMemoryAdapter(harness.coordinator).list_active_memories(harness.context)
    assert len(active) == 1 and active[0].canonical_value == new


def test_category_and_exact_duplicate_reconfirmations_suppress_nothing(tmp_path) -> None:
    value = "create short Instagram reels clearly"
    initial = f"I want to {value}."
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    created = run_text(extraction, harness, initial, message_id="reconfirm-initial")
    duplicate = run_text(extraction, harness, initial, message_id="reconfirm-duplicate")
    memory_id = created.decisions[0].memory_ids[0]
    assert duplicate.decisions[0].outcome == "reconfirmed"
    assert duplicate.current_turn_override.suppressed_memory_ids == ()
    assert duplicate.current_turn_override.candidate_target_memory_ids == (memory_id,)
    assert not duplicate.current_turn_override.contradiction_deterministic

    category = "That is a goal, not a preference."
    start = initial.index(value)
    fixture = _assertion(
        initial,
        value,
        "category-prior",
        proposal_id="category-reconfirm",
        memory_type="goal",
        domain="video_creation",
        slot="current_primary_goal",
    )
    fixture["source_spans"].append(
        {
            "message_id": "category-current",
            "start": 0,
            "end": len(category),
            "quoted_text": category,
        }
    )
    fixture["source_spans"][0] = {
        "message_id": "category-prior",
        "start": start,
        "end": start + len(value),
        "quoted_text": value,
    }
    model = FixtureExtractionModel({category: _response(fixture)})
    category_extraction = MemoryExtractionCoordinator(
        ChatMemoryAdapter(harness.coordinator), model=model
    )
    request, context = extraction_input(
        harness,
        category,
        message_id="category-current",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        supporting_window=(
            TrustedConversationMessage(
                message_id="category-prior",
                role=ConversationRole.USER,
                content=initial,
            ),
        ),
    )
    category_result = category_extraction.process(request, context)
    assert category_result.decisions[0].outcome == "reconfirmed"
    assert category_result.current_turn_override.suppressed_memory_ids == ()
    assert category_result.current_turn_override.candidate_target_memory_ids == (memory_id,)
    assert not category_result.current_turn_override.contradiction_deterministic


def test_compatible_refinement_target_is_not_suppressed() -> None:
    target = CanonicalMemorySnapshot(
        memory_id=uuid4(),
        owner_id="00000000-0000-4000-8000-000000000001",
        subject_key="user",
        memory_type=MemoryType.KNOWLEDGE,
        domain_key="software_development",
        slot_key=f"knowledge:software_development:{uuid4()}",
        cardinality=Cardinality.ADDITIVE,
        canonical_value={"language": "Python"},
        display_text="Uses Python",
        sensitivity=Sensitivity.NORMAL,
        status=MemoryLifecycleState.ACTIVE,
        revision=1,
    )
    builder = CurrentTurnOverrideBuilder(
        owner_id=target.owner_id,
        source_message_id="compatible-refinement",
    )
    builder.record_final_outcome(
        CorrectionResolutionKind.REFINE,
        (target,),
        ExtractionCandidateDecision(
            action=CandidateAction.REFINE,
            reason="compatible_refinement",
            memory_ids=(target.memory_id,),
            outcome="refined",
        ),
    )
    override = builder.build(
        status=ExtractionStatus.APPLIED,
        sensitivity=Sensitivity.NORMAL,
        positive_current_assertion={"language": "Python", "version": "3.14"},
        confidence=0.99,
    )
    assert override.candidate_target_memory_ids == (target.memory_id,)
    assert override.suppressed_memory_ids == ()
    assert not override.contradiction_deterministic


def test_replacement_retraction_and_ambiguity_have_distinct_suppression(tmp_path) -> None:
    old = "create long-form cinematic YouTube videos"
    critical = (
        "I no longer want to make long-form cinematic YouTube videos. "
        "I want to create short Instagram reels clearly."
    )
    harness, extraction, _diagnostics = extraction_harness(tmp_path / "replace")
    created = run_text(extraction, harness, f"I want to {old}.", message_id="replace-old")
    replaced = run_text(extraction, harness, critical, message_id="replace-new")
    predecessor_id = created.decisions[0].memory_ids[0]
    assert replaced.current_turn_override.suppressed_memory_ids == (predecessor_id,)
    assert replaced.current_turn_override.candidate_target_memory_ids == (predecessor_id,)
    assert replaced.current_turn_override.contradiction_deterministic

    harness, extraction, _diagnostics = extraction_harness(tmp_path / "retract")
    seeded = GenericMemoryAdapter(harness.coordinator).create(
        harness.context,
        StructuredMemoryInput(
            memory_type=MemoryType.IDENTITY,
            domain_key="global",
            slot_key="identity:global:current_location",
            cardinality=Cardinality.EXCLUSIVE,
            canonical_value="Pune",
            display_text="Pune",
        ),
        idempotency_key=MemoryIdempotency.manual(
            harness.context.execution.owner_id, "seed-location"
        ),
    )
    location_id = seeded.mutation.affected_memory_ids[0]
    retracted = run_text(
        extraction,
        harness,
        "I no longer live in Pune.",
        message_id="retract-location",
    )
    assert retracted.current_turn_override.positive_current_assertion is None
    assert str(location_id) in {
        str(item) for item in retracted.current_turn_override.suppressed_memory_ids
    }
    assert retracted.current_turn_override.contradiction_deterministic

    harness, base, _diagnostics = extraction_harness(tmp_path / "ambiguous")
    original = run_text(
        base,
        harness,
        f"I want to {old}.",
        message_id="ambiguous-old",
    )
    text = "Now I want to create travel videos."
    proposal = _assertion(
        text,
        "create travel videos",
        "ambiguous-new",
        proposal_id="ambiguous-new",
        memory_type="goal",
        domain="video_creation",
        slot="current_primary_goal",
    )
    model = FixtureExtractionModel({text: _response(proposal)})
    ambiguous_extraction = MemoryExtractionCoordinator(
        ChatMemoryAdapter(harness.coordinator), model=model
    )
    ambiguous = run_text(
        ambiguous_extraction,
        harness,
        text,
        message_id="ambiguous-new",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    active_id = original.decisions[0].memory_ids[0]
    assert ambiguous.status is ExtractionStatus.NEEDS_REVIEW
    assert ambiguous.current_turn_override.suppressed_memory_ids == ()
    assert ambiguous.current_turn_override.candidate_target_memory_ids == (active_id,)
    assert ambiguous.current_turn_override.unresolved_conflict_slot_keys == (
        "goal:video_creation:current_primary_goal",
    )
    assert ambiguous.current_turn_override.review_required
    assert not ambiguous.current_turn_override.contradiction_deterministic


def test_failure_disabled_and_ignored_paths_publish_empty_overrides(tmp_path) -> None:
    malformed_text = "I use Python for work."
    malformed_model = FixtureExtractionModel({malformed_text: "not-json"})
    harness, extraction, _diagnostics = extraction_harness(
        tmp_path / "malformed", model=malformed_model
    )
    malformed = run_text(
        extraction,
        harness,
        malformed_text,
        message_id="malformed",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    _assert_empty_override(malformed)

    timeout_text = "I use Rust for work."
    timeout_model = FixtureExtractionModel({timeout_text: ExtractionModelTimeout("model_timeout")})
    harness, extraction, _diagnostics = extraction_harness(
        tmp_path / "timeout", model=timeout_model
    )
    timeout = run_text(
        extraction,
        harness,
        timeout_text,
        message_id="timeout",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    _assert_empty_override(timeout)

    for index, text in enumerate(
        (
            "I am drinking coffee right now and I have a headache.",
            "My friend prefers Rust.",
        )
    ):
        harness, extraction, _diagnostics = extraction_harness(tmp_path / f"ignored-{index}")
        _assert_empty_override(run_text(extraction, harness, text, message_id=f"ignored-{index}"))

    harness, extraction, _diagnostics = extraction_harness(tmp_path / "disabled")
    incognito = run_text(
        extraction,
        harness,
        "I use Go for work.",
        message_id="incognito",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        incognito=True,
    )
    memory_disabled = run_text(
        extraction,
        harness,
        "I use Go for work.",
        message_id="memory-disabled",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        memory_enabled=False,
    )
    _assert_empty_override(incognito)
    _assert_empty_override(memory_disabled)

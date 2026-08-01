from __future__ import annotations

from app.services.memory_v2.extraction import (
    ExtractionModelTimeout,
    FixtureExtractionModel,
)
from app.services.memory_v2.extraction_contracts import (
    ConversationRole,
    ExtractionMode,
    ExtractionStatus,
    TrustedConversationMessage,
)
from app.services.memory_v2.model_schema import parse_model_output
from tests.memory_v2.phase3_helpers import OWNER_B
from tests.memory_v2.phase4_helpers import (
    extraction_input,
    phase4_harness,
    run_text,
    sql_state,
)


def _span(text: str, value: str, *, message_id: str):
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
    *,
    message_id: str,
    proposal_id: str = "p1",
    memory_type: str = "knowledge",
    domain: str = "software_development",
    slot: str | None = None,
    subject: str = "user",
    durability: str = "durable",
    sensitivity: str = "normal",
    confidence: float = 0.95,
):
    return {
        "proposal_id": proposal_id,
        "source_spans": [_span(text, value, message_id=message_id)],
        "subject_hint": subject,
        "memory_type_hint": memory_type,
        "domain_hint": domain,
        "slot_hint": slot,
        "typed_value": value,
        "display_hint": value,
        "durability": durability,
        "confidence": confidence,
        "sensitivity_hint": sensitivity,
    }


def _response(*assertions, retractions=(), exclusions=()):
    return {
        "schema_version": 1,
        "assertions": list(assertions),
        "retractions": list(retractions),
        "exclusions": list(exclusions),
    }


def test_strict_model_schema_rejects_owner_and_canonical_target_ids() -> None:
    payload = _response()
    payload["owner_id"] = "00000000-0000-4000-8000-000000000001"
    try:
        parse_model_output(payload)
    except ValueError as exc:
        assert str(exc) == "invalid_model_schema"
    else:
        raise AssertionError("model owner field unexpectedly accepted")

    assertion = _assertion("I use Python", "I use Python", message_id="m")
    assertion["canonical_memory_id"] = "00000000-0000-4000-8000-000000000099"
    try:
        parse_model_output(_response(assertion))
    except ValueError as exc:
        assert str(exc) == "invalid_model_schema"
    else:
        raise AssertionError("model target ID unexpectedly accepted")


def test_malformed_json_has_bounded_repair_and_no_generic_fallback(tmp_path) -> None:
    text = "I am a Python developer."
    model = FixtureExtractionModel({text: "not-json"})
    harness, extraction, diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="malformed",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.FAILED
    assert model.call_count == 2
    assert sql_state(harness.database_path)["records"] == []
    assert diagnostics.snapshot()[-1].reason_codes == ("malformed_model_json",)


def test_timeout_does_not_retry_or_mutate(tmp_path) -> None:
    text = "I use Python for work."
    model = FixtureExtractionModel({text: ExtractionModelTimeout("model_timeout")})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="timeout",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.FAILED
    assert model.call_count == 1
    assert sql_state(harness.database_path)["operations"] == []


def test_schema_repair_retry_can_recover_once(tmp_path) -> None:
    text = "I use Python for work."
    valid = _response(_assertion(text, "I use Python", message_id="repair"))
    model = FixtureExtractionModel({text: ["bad-json", valid]})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="repair",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.APPLIED
    assert model.call_count == 2
    assert len(sql_state(harness.database_path)["records"]) == 1


def test_hallucinated_value_and_assistant_span_are_rejected(tmp_path) -> None:
    text = "I use Python for work."
    hallucinated_proposal = _assertion(text, "I use Python", message_id="hallucination")
    hallucinated_proposal["typed_value"] = "I secretly prefer Haskell"
    hallucinated_proposal["display_hint"] = "I secretly prefer Haskell"
    hallucinated = _response(hallucinated_proposal)
    model = FixtureExtractionModel({text: hallucinated})
    harness, extraction, _diagnostics = phase4_harness(tmp_path / "hallucination", model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="hallucination",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.REJECTED
    assert result.grounding[0].reason == "asserted_value_not_in_source"
    assert sql_state(harness.database_path)["records"] == []

    assistant_text = "You prefer Python."
    user_text = "Okay."
    proposal = _assertion(
        assistant_text,
        "prefer Python",
        message_id="assistant-message",
        memory_type="preference",
        domain="software_development",
        slot="language",
    )
    model = FixtureExtractionModel({user_text: _response(proposal)})
    harness, extraction, _diagnostics = phase4_harness(tmp_path / "assistant", model=model)
    result = run_text(
        extraction,
        harness,
        user_text,
        message_id="user-message",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        supporting_window=(
            TrustedConversationMessage(
                message_id="assistant-message",
                role=ConversationRole.ASSISTANT,
                content=assistant_text,
            ),
        ),
    )
    assert result.grounding[0].reason == "source_message_not_user_authorized"
    assert sql_state(harness.database_path)["records"] == []


def test_temporary_text_is_rejected_before_model(tmp_path) -> None:
    text = "I am drinking coffee right now and I have a headache."
    model = FixtureExtractionModel({text: _response()})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(extraction, harness, text, message_id="temporary")
    assert result.status is ExtractionStatus.NO_ACTION
    assert model.call_count == 0
    assert sql_state(harness.database_path)["records"] == []


def test_durable_fact_can_be_extracted_from_mixed_temporary_request(tmp_path) -> None:
    text = "I am a Python developer, and remind me to drink water right now."
    value = "I am a Python developer"
    proposal = _assertion(
        text,
        value,
        message_id="mixed",
        memory_type="identity",
        domain="global",
        slot="developer_role",
    )
    model = FixtureExtractionModel({text: _response(proposal)})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="mixed",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.APPLIED
    records = sql_state(harness.database_path)["records"]
    assert len(records) == 1
    assert records[0]["memory_type"] == "identity"
    assert "drink water" not in records[0]["display_text"]


def test_typed_expiry_proposal_is_reviewed_instead_of_persisted_forever(tmp_path) -> None:
    text = "I use Python until August 31, 2026."
    proposal = _assertion(text, text[:-1], message_id="expiring")
    proposal["expires_at"] = "2026-08-31T23:59:59+00:00"
    model = FixtureExtractionModel({text: _response(proposal)})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="expiring",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.NEEDS_REVIEW
    assert result.decisions[0].reason == "typed_expiry_requires_review"
    state = sql_state(harness.database_path)
    assert state["records"] == []
    assert len(state["candidates"]) == 1
    assert state["candidates"][0]["state"] == "needs_review"
    assert "2026-08-31T23:59:59+00:00" in state["candidates"][0]["grounding_evidence_json"]


def test_automatic_candidate_cap_is_four_and_explicit_batch_can_exceed_four(tmp_path) -> None:
    facts = [f"I use Python tool {index}" for index in range(6)]
    text = "; ".join(facts)
    proposals = [
        _assertion(text, fact, message_id="cap", proposal_id=f"p{index}")
        for index, fact in enumerate(facts)
    ]
    model = FixtureExtractionModel({text: _response(*proposals)})
    harness, extraction, _diagnostics = phase4_harness(tmp_path / "cap", model=model)
    capped = run_text(
        extraction,
        harness,
        text,
        message_id="cap",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert capped.model_summary.capped_count == 2
    assert len(sql_state(harness.database_path)["records"]) == 4

    lines = [f"I use Python library {index}" for index in range(6)]
    batch_text = "Remember these 6 facts:\n" + "\n".join(f"- {line}" for line in lines)
    harness, extraction, _diagnostics = phase4_harness(tmp_path / "batch")
    batch = run_text(
        extraction,
        harness,
        batch_text,
        message_id="batch",
        mode=ExtractionMode.EXPLICIT_BATCH,
        maximum_candidates=10,
        explicit=True,
    )
    assert batch.status is ExtractionStatus.APPLIED
    assert len(batch.decisions) == 6
    assert len(sql_state(harness.database_path)["records"]) == 6


def test_ambiguous_correction_persists_review_candidate_without_mutation(tmp_path) -> None:
    harness, base_extraction, _diagnostics = phase4_harness(tmp_path / "base")
    run_text(
        base_extraction,
        harness,
        "I want to create long-form cinematic YouTube videos.",
        message_id="ambiguous-old",
    )
    text = "Now I want to create short Instagram reels clearly."
    value = "create short Instagram reels clearly"
    proposal = _assertion(
        text,
        value,
        message_id="ambiguous-new",
        memory_type="goal",
        domain="video_creation",
        slot="current_primary_goal",
    )
    model = FixtureExtractionModel({text: _response(proposal)})
    from app.services.memory_v2.adapters import ChatMemoryV2Adapter
    from app.services.memory_v2.extraction_coordinator import MemoryV2ExtractionCoordinator

    extraction = MemoryV2ExtractionCoordinator(
        ChatMemoryV2Adapter(harness.coordinator), model=model
    )
    result = run_text(
        extraction,
        harness,
        text,
        message_id="ambiguous-new",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.NEEDS_REVIEW
    before_review = sql_state(harness.database_path)
    assert len(before_review["records"]) == 1
    assert [row["operation_kind"] for row in before_review["operations"]] == ["create"]
    review = [row for row in before_review["candidates"] if row["state"] == "needs_review"]
    assert len(review) == 1
    assert review[0]["applied_operation_id"] is None


def test_prohibited_source_never_reaches_model_or_durable_diagnostics(tmp_path) -> None:
    sensitive_fragment = "password is " + "do-not-store-value"
    text = f"Remember that my {sensitive_fragment}."
    model = FixtureExtractionModel({text: _response()})
    harness, extraction, diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="prohibited",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
    )
    assert result.status is ExtractionStatus.REJECTED
    assert model.call_count == 0
    assert sql_state(harness.database_path)["candidates"] == []
    diagnostic_text = str(diagnostics.snapshot()[-1].model_dump(mode="json"))
    assert sensitive_fragment not in diagnostic_text


def test_model_global_category_is_corrected_to_grounded_video_domain(tmp_path) -> None:
    text = "For video-editing advice, give me quick 15-minute drills."
    value = "give me quick 15-minute drills"
    proposal = _assertion(
        text,
        value,
        message_id="wrong-global",
        memory_type="preference",
        domain="global",
        slot="practice_advice_format",
    )
    model = FixtureExtractionModel({text: _response(proposal)})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="wrong-global",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.APPLIED
    record = sql_state(harness.database_path)["records"][0]
    assert record["domain_key"] == "video_creation"


def test_incognito_memory_disabled_and_owner_mismatch_are_zero_call(tmp_path) -> None:
    text = "I use Python for work."
    model = FixtureExtractionModel({text: _response()})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    incognito = run_text(
        extraction,
        harness,
        text,
        message_id="incognito",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        incognito=True,
    )
    disabled = run_text(
        extraction,
        harness,
        text,
        message_id="disabled",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        memory_enabled=False,
    )
    request, context = extraction_input(
        harness,
        text,
        message_id="owner-mismatch",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    mismatch = extraction.process(request.model_copy(update={"owner_id": OWNER_B}), context)
    assert {incognito.status, disabled.status} == {ExtractionStatus.DISABLED}
    assert mismatch.status is ExtractionStatus.REJECTED
    assert model.call_count == 0
    assert sql_state(harness.database_path)["records"] == []


def test_sensitive_explicit_request_uses_encrypted_candidate_and_record(tmp_path) -> None:
    text = "Remember that my diagnosis is asthma."
    value = "my diagnosis is asthma"
    proposal = _assertion(
        text,
        value,
        message_id="sensitive",
        memory_type="knowledge",
        domain="health_fitness",
        sensitivity="normal",
    )
    model = FixtureExtractionModel({text: _response(proposal)})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="sensitive",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        explicit=True,
    )
    assert result.status is ExtractionStatus.APPLIED
    state = sql_state(harness.database_path)
    assert state["records"][0]["canonical_payload"] is None
    assert state["candidates"][0]["canonical_payload"] is None
    assert state["candidates"][0]["sensitivity"] == "sensitive"

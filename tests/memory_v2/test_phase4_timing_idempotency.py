from __future__ import annotations

from app.services.memory_v2.extraction import FixtureExtractionModel
from app.services.memory_v2.extraction_contracts import (
    CandidateAction,
    ExtractionMode,
    ExtractionStatus,
)
from tests.memory_v2.phase4_helpers import (
    extraction_input,
    phase4_harness,
    run_text,
    sql_state,
)


def _span(text: str, value: str, message_id: str):
    start = text.index(value)
    return {
        "message_id": message_id,
        "start": start,
        "end": start + len(value),
        "quoted_text": value,
    }


def _knowledge_response(text: str, value: str, message_id: str, *, proposal_id="p1"):
    return {
        "schema_version": 1,
        "assertions": [
            {
                "proposal_id": proposal_id,
                "source_spans": [_span(text, value, message_id)],
                "subject_hint": "user",
                "memory_type_hint": "knowledge",
                "domain_hint": "software_development",
                "typed_value": value,
                "display_hint": value,
                "durability": "durable",
                "confidence": 0.96,
                "sensitivity_hint": "normal",
            }
        ],
        "retractions": [],
        "exclusions": [],
    }


def test_repeated_extraction_is_candidate_and_operation_idempotent(tmp_path) -> None:
    harness, extraction, _diagnostics = phase4_harness(tmp_path)
    text = "I want to create long-form cinematic YouTube videos."
    first = run_text(extraction, harness, text, message_id="same-message")
    before = sql_state(harness.database_path)
    second = run_text(extraction, harness, text, message_id="same-message")
    after = sql_state(harness.database_path)
    assert first.decisions[0].action is CandidateAction.CREATE
    assert second.decisions[0].action is CandidateAction.IDEMPOTENT_REPLAY
    assert second.decisions[0].operation_id == first.decisions[0].operation_id
    assert after == before


def test_sync_and_stream_use_same_extraction_candidate_and_operation(tmp_path) -> None:
    text = "I use Python for work."
    model = FixtureExtractionModel({text: _knowledge_response(text, "I use Python", "cross")})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    first = run_text(
        extraction,
        harness,
        text,
        message_id="cross",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    request, context = extraction_input(
        harness,
        text,
        message_id="cross",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    second = extraction.process(request, context, transport="stream")
    assert second.decisions[0].action is CandidateAction.IDEMPOTENT_REPLAY
    assert second.decisions[0].operation_id == first.decisions[0].operation_id
    assert len(sql_state(harness.database_path)["operations"]) == 1


def test_foreground_correction_is_before_response_and_model_free(tmp_path) -> None:
    model = FixtureExtractionModel({})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    run_text(
        extraction,
        harness,
        "I want to create long-form cinematic YouTube videos.",
        message_id="timing-old",
    )
    result = run_text(
        extraction,
        harness,
        "I no longer want to make long-form cinematic YouTube videos. "
        "I want to create short Instagram reels clearly.",
        message_id="timing-new",
    )
    assert result.timing == "before_response"
    assert result.model_summary.called is False
    assert model.call_count == 0


def test_post_turn_model_failure_is_after_turn_and_returns_data_not_exception(tmp_path) -> None:
    text = "I use Python for work."
    model = FixtureExtractionModel({text: "{"})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="post-failure",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.FAILED
    assert result.timing == "after_turn"
    assert sql_state(harness.database_path)["operations"] == []


def test_ambiguous_foreground_override_carries_positive_current_assertion(tmp_path) -> None:
    harness, extraction, _diagnostics = phase4_harness(tmp_path)
    result = run_text(
        extraction,
        harness,
        "Now I want to learn Japanese.",
        message_id="override-ambiguous",
    )
    assert result.status is ExtractionStatus.NEEDS_REVIEW
    assert result.current_turn_override.positive_current_assertion == "learn Japanese"
    assert not result.current_turn_override.contradiction_deterministic


def test_unicode_nfkc_span_grounding_is_exact_and_value_supported(tmp_path) -> None:
    text = "I use Ｐｙｔｈｏｎ for work."
    quoted = "Ｐｙｔｈｏｎ"
    response = _knowledge_response(text, quoted, "unicode")
    response["assertions"][0]["typed_value"] = "Python"
    response["assertions"][0]["display_hint"] = "Python"
    model = FixtureExtractionModel({text: response})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="unicode",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.APPLIED
    assert result.grounding[0].accepted
    assert sql_state(harness.database_path)["records"][0]["display_text"] == "Python"


def test_impossible_offsets_do_not_create_candidate_or_memory(tmp_path) -> None:
    text = "I use Python for work."
    response = _knowledge_response(text, "I use Python", "offset")
    response["assertions"][0]["source_spans"][0]["end"] = len(text) + 20
    model = FixtureExtractionModel({text: response})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="offset",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.REJECTED
    state = sql_state(harness.database_path)
    assert state["records"] == state["candidates"] == []


def test_candidate_transitions_validated_to_applied_with_bounded_metadata(tmp_path) -> None:
    text = "I use Python for work."
    model = FixtureExtractionModel({text: _knowledge_response(text, "I use Python", "lifecycle")})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="lifecycle",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.APPLIED
    candidate = sql_state(harness.database_path)["candidates"][0]
    assert candidate["state"] == "applied"
    assert candidate["decision_outcome"] == "created"
    assert candidate["applied_operation_id"]
    assert candidate["extractor_name"] == "phase4-model"
    assert len(candidate["raw_output_hash"]) == 64
    assert "reasoning" not in candidate["grounding_evidence_json"]


def test_explicit_archive_and_restore_route_through_phase3_commands(tmp_path) -> None:
    harness, extraction, _diagnostics = phase4_harness(tmp_path)
    value = "create long-form cinematic YouTube videos"
    run_text(extraction, harness, f"I want to {value}.", message_id="life-create")
    archived = run_text(
        extraction,
        harness,
        f"archive {value}",
        message_id="life-archive",
    )
    assert archived.decisions[0].outcome == "archived"
    restored = run_text(
        extraction,
        harness,
        f"restore {value}",
        message_id="life-restore",
    )
    assert restored.decisions[0].outcome == "restored"
    assert [row["operation_kind"] for row in sql_state(harness.database_path)["operations"]] == [
        "create",
        "archive",
        "restore",
    ]


def test_explicit_batch_item_failure_does_not_rollback_valid_items(tmp_path) -> None:
    text = "Batch facts: I use Python; unrelated text."
    good = _knowledge_response(text, "I use Python", "batch-model", proposal_id="good")[
        "assertions"
    ][0]
    bad = _knowledge_response(text, "unrelated text", "batch-model", proposal_id="bad")[
        "assertions"
    ][0]
    bad["typed_value"] = "invented fact"
    bad["display_hint"] = "invented fact"
    response = {
        "schema_version": 1,
        "assertions": [good, bad],
        "retractions": [],
        "exclusions": [],
    }
    model = FixtureExtractionModel({text: response})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="batch-model",
        mode=ExtractionMode.EXPLICIT_BATCH,
        maximum_candidates=10,
    )
    assert {item.action for item in result.decisions} == {
        CandidateAction.CREATE,
        CandidateAction.REJECT,
    }
    assert len(sql_state(harness.database_path)["records"]) == 1


def test_guest_extraction_is_bound_to_guest_disposable_database(tmp_path) -> None:
    harness, extraction, _diagnostics = phase4_harness(tmp_path, guest=True)
    result = run_text(
        extraction,
        harness,
        "I want to create long-form cinematic YouTube videos.",
        message_id="guest-memory",
    )
    assert result.status is ExtractionStatus.APPLIED
    assert harness.context.execution.database_identity.startswith("guest-profile:")
    assert len(sql_state(harness.database_path)["records"]) == 1

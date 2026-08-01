from __future__ import annotations

import json
from uuid import UUID

import pytest

from app.services.memory_v2.extraction import FixtureExtractionModel
from app.services.memory_v2.extraction_contracts import (
    CandidateAction,
    ConversationRole,
    ExtractionMode,
    ExtractionStatus,
    TrustedConversationMessage,
)
from tests.memory_v2.phase4_helpers import phase4_harness, run_text, sql_state

ACTIVE_GOAL = "create short Instagram reels clearly"
PRIOR_GOAL_MESSAGE = f"I want to {ACTIVE_GOAL}."


@pytest.mark.parametrize(
    "correction",
    [
        "That is a goal, not a preference.",
        "What I said is a goal, not a preference.",
    ],
)
def test_bounded_category_correction_reconfirms_active_goal_without_suppression(
    tmp_path, correction
) -> None:
    model = FixtureExtractionModel({correction: "provider-should-not-be-called"})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    created = run_text(
        extraction,
        harness,
        PRIOR_GOAL_MESSAGE,
        message_id="category-prior",
    )
    active_id = created.decisions[0].memory_ids[0]
    result = run_text(
        extraction,
        harness,
        correction,
        message_id="category-current",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        supporting_window=(
            TrustedConversationMessage(
                message_id="category-prior",
                role=ConversationRole.USER,
                content=PRIOR_GOAL_MESSAGE,
            ),
        ),
    )
    assert model.call_count == 0
    assert result.status is ExtractionStatus.APPLIED
    assert result.decisions[0].action is CandidateAction.RECONFIRM
    assert result.decisions[0].outcome == "reconfirmed"
    assert result.current_turn_override.candidate_target_memory_ids == (active_id,)
    assert result.current_turn_override.suppressed_memory_ids == ()
    assert result.current_turn_override.suppressed_slot_keys == ()
    assert not result.current_turn_override.review_required
    grounded_messages = {
        span.message_id for decision in result.grounding for span in decision.spans
    }
    assert grounded_messages == {"category-prior", "category-current"}
    state = sql_state(harness.database_path)
    assert [row["status"] for row in state["records"]] == ["active"]
    assert state["records"][0]["id"] == str(active_id)


@pytest.mark.parametrize(
    ("text", "location"),
    [
        ("I live in Pune.", "Pune"),
        ("I currently live in Delhi.", "Delhi"),
        ("My current city is Mumbai.", "Mumbai"),
    ],
)
def test_clear_first_person_residence_forms_create_grounded_current_location(
    tmp_path, text, location
) -> None:
    model = FixtureExtractionModel({text: "provider-should-not-be-called"})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(extraction, harness, text, message_id="current-location")
    assert model.call_count == 0
    assert result.status is ExtractionStatus.APPLIED
    assert result.decisions[0].action is CandidateAction.CREATE
    accepted = next(item for item in result.grounding if item.accepted)
    assert len(accepted.spans) == 1
    span = accepted.spans[0]
    assert (span.start, span.end) == (
        text.index(location),
        text.index(location) + len(location),
    )
    state = sql_state(harness.database_path)
    assert len(state["records"]) == 1
    record = state["records"][0]
    assert record["memory_type"] == "identity"
    assert record["domain_key"] == "global"
    assert record["slot_key"] == "identity:global:current_location"
    assert json.loads(record["canonical_payload"]) == location
    assert record["status"] == "active"


@pytest.mark.parametrize("text", ["I am visiting Pune.", "I am in Pune right now."])
def test_transient_location_forms_do_not_create_permanent_residence(tmp_path, text) -> None:
    model = FixtureExtractionModel({text: "provider-should-not-be-called"})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(extraction, harness, text, message_id="transient-location")
    assert model.call_count == 0
    assert result.status is ExtractionStatus.NO_ACTION
    assert result.preparse.reason in {"transient_location_not_residence", "temporary_state"}
    state = sql_state(harness.database_path)
    assert state["records"] == []
    assert state["candidates"] == []


def test_location_retraction_archives_exact_active_location_and_suppresses_it(tmp_path) -> None:
    harness, extraction, _diagnostics = phase4_harness(tmp_path)
    created = run_text(
        extraction,
        harness,
        "I live in Pune.",
        message_id="location-create",
    )
    memory_id = created.decisions[0].memory_ids[0]
    result = run_text(
        extraction,
        harness,
        "I no longer live in Pune.",
        message_id="location-retract",
    )
    assert result.status is ExtractionStatus.APPLIED
    assert result.decisions[0].action is CandidateAction.RETRACT
    assert result.decisions[0].outcome == "archived"
    assert result.current_turn_override.positive_current_assertion is None
    assert result.current_turn_override.suppressed_memory_ids == (memory_id,)
    assert result.current_turn_override.suppressed_slot_keys == (
        "identity:global:current_location",
    )
    assert result.current_turn_override.contradiction_deterministic
    state = sql_state(harness.database_path)
    assert len(state["records"]) == 1
    assert state["records"][0]["status"] == "archived"
    assert json.loads(state["records"][0]["canonical_payload"]) == "Pune"
    assert "not Pune" not in harness.database_path.read_text(errors="ignore")


def test_invalid_model_schema_persists_grounded_ambiguous_goal_review(tmp_path) -> None:
    text = "Now I want to create travel videos."
    invalid = {"schema_version": 1, "assertions": [], "unexpected": "field"}
    model = FixtureExtractionModel({text: invalid})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    existing = run_text(
        extraction,
        harness,
        PRIOR_GOAL_MESSAGE,
        message_id="existing-video-goal",
    )
    existing_id = existing.decisions[0].memory_ids[0]
    before = sql_state(harness.database_path)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="ambiguous-travel-goal",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert model.call_count == 2
    assert result.status is ExtractionStatus.NEEDS_REVIEW
    assert result.decisions[0].action is CandidateAction.REVIEW
    assert result.decisions[0].candidate_id is not None
    assert result.decisions[0].review_required
    assert result.decisions[0].proposed_memory_type == "goal"
    assert result.decisions[0].proposed_domain_hint is None
    assert result.decisions[0].proposed_slot_hint is None
    assert result.decisions[0].domain_unresolved
    assert result.decisions[0].slot_unresolved
    assert result.decisions[0].model_failure_reason == "invalid_model_schema"
    assert result.current_turn_override.review_required
    assert result.current_turn_override.suppressed_memory_ids == ()
    assert result.current_turn_override.suppressed_slot_keys == ()
    assert not result.current_turn_override.contradiction_deterministic
    assert result.current_turn_override.candidate_target_memory_ids == ()
    assert result.diagnostic.review_count == 1
    assert result.diagnostic.reason_codes == (
        "invalid_model_schema",
        "grounded_preparse_review_persisted",
    )
    assert result.model_summary.raw_output_hash is not None
    state = sql_state(harness.database_path)
    assert len(state["records"]) == len(before["records"]) == 1
    assert state["records"][0]["id"] == str(existing_id)
    assert state["records"][0]["status"] == "active"
    assert len(state["operations"]) == len(before["operations"]) == 1
    reviews = [item for item in state["candidates"] if item["state"] == "needs_review"]
    assert len(reviews) == 1
    review = reviews[0]
    assert review["memory_type"] == "goal"
    assert json.loads(review["canonical_payload"]) == "create travel videos"
    assert review["applied_operation_id"] is None
    assert review["raw_output_hash"] == result.model_summary.raw_output_hash
    spans = json.loads(review["source_spans_json"])
    assert len(spans) == 1
    assert spans[0]["message_id"] == "ambiguous-travel-goal"
    assert (spans[0]["start"], spans[0]["end"]) == (
        text.index("create travel videos"),
        text.index("create travel videos") + len("create travel videos"),
    )
    evidence = json.loads(review["grounding_evidence_json"])
    assert evidence["preparse_review_fallback"] is True
    assert evidence["model_failure_reason"] == "invalid_model_schema"
    assert evidence["domain_unresolved"] is True
    assert evidence["slot_unresolved"] is True


def test_unresolved_category_reference_persists_review_instead_of_no_action(tmp_path) -> None:
    text = "What I said is a goal, not a preference."
    model = FixtureExtractionModel({text: "not-json"})
    harness, extraction, _diagnostics = phase4_harness(tmp_path, model=model)
    result = run_text(
        extraction,
        harness,
        text,
        message_id="unresolved-category",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
    )
    assert result.status is ExtractionStatus.NEEDS_REVIEW
    assert result.decisions[0].action is CandidateAction.REVIEW
    assert result.current_turn_override.suppressed_memory_ids == ()
    state = sql_state(harness.database_path)
    assert state["records"] == []
    assert len(state["candidates"]) == 1
    assert state["candidates"][0]["state"] == "needs_review"
    assert UUID(state["candidates"][0]["id"]) == result.decisions[0].candidate_id

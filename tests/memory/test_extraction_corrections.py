from __future__ import annotations

from uuid import UUID, uuid4

from app.services.memory.contracts import (
    CanonicalMemorySnapshot,
    MemoryLifecycleState,
    Sensitivity,
)
from app.services.memory.correction_resolver import (
    CorrectionResolutionKind,
    DeterministicCorrectionResolver,
)
from app.services.memory.extraction_contracts import CandidateAction, ExtractionStatus
from app.services.memory.queries import RecallMode, RecallQuery
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.extraction_helpers import extraction_harness, run_text, sql_state
from tests.memory.recall_helpers import query_context, recall_services

OLD_GOAL = "create long-form cinematic YouTube videos"
NEW_GOAL = "create short Instagram reels clearly"
CRITICAL_CORRECTION = (
    "I no longer want to make long-form cinematic YouTube videos. "
    "I want to create short Instagram reels clearly."
)


def test_one_turn_replaces_independent_goal_and_preference_without_cross_contamination(
    tmp_path,
) -> None:
    """Consecutive corrections are separate lifecycle operations, never one text blob."""

    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    old_goal = "create long-form cinematic YouTube videos"
    old_preference = "answer me with detailed explanations"
    new_goal = "create short Instagram reels clearly"
    new_preference = "concise explanations"
    initial = run_text(
        extraction,
        harness,
        f"I want to {old_goal}. Always {old_preference}.",
        message_id="paired-initial",
    )
    assert initial.status is ExtractionStatus.APPLIED
    assert [decision.action for decision in initial.decisions] == [
        CandidateAction.CREATE,
        CandidateAction.CREATE,
    ]

    corrected = run_text(
        extraction,
        harness,
        f"I no longer want to {old_goal}. I want to {new_goal}. "
        f"I don't prefer detailed explanations anymore. I prefer {new_preference}.",
        message_id="paired-correction",
    )
    assert corrected.status is ExtractionStatus.APPLIED
    assert corrected.model_summary.called is False
    assert [decision.action for decision in corrected.decisions] == [
        CandidateAction.REPLACE,
        CandidateAction.REPLACE,
    ]

    state = sql_state(harness.database_path)
    active = [row for row in state["records"] if row["status"] == "active"]
    superseded = [row for row in state["records"] if row["status"] == "superseded"]
    assert {(row["memory_type"], row["display_text"]) for row in active} == {
        ("goal", new_goal),
        ("preference", new_preference),
    }
    assert {(row["memory_type"], row["display_text"]) for row in superseded} == {
        ("goal", old_goal),
        ("preference", old_preference),
    }
    assert len(state["relations"]) == 2
    assert {row["relation_type"] for row in state["relations"]} == {"supersedes"}
    assert all("no longer" not in row["display_text"].casefold() for row in active)
    assert all("don't prefer" not in row["display_text"].casefold() for row in active)

    services = recall_services(harness)
    try:
        recalled = services.recall.recall(
            RecallQuery(
                context=query_context(services, mode=RecallMode.BROAD),
                text="What do you remember about me?",
            )
        )
        assert {item.memory.display_text for item in recalled.items} == {new_goal, new_preference}
        assert all(old_goal not in item.memory.display_text for item in recalled.items)
        assert all(old_preference not in item.memory.display_text for item in recalled.items)
    finally:
        services.close()


def test_critical_implicit_video_correction_is_one_grounded_replace(tmp_path) -> None:
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    created = run_text(
        extraction,
        harness,
        f"I want to {OLD_GOAL}.",
        message_id="video-old",
    )
    assert created.status is ExtractionStatus.APPLIED

    corrected = run_text(
        extraction,
        harness,
        CRITICAL_CORRECTION,
        message_id="video-correction",
    )
    assert corrected.status is ExtractionStatus.APPLIED
    assert len(corrected.decisions) == 1
    assert corrected.decisions[0].action is CandidateAction.REPLACE
    assert corrected.decisions[0].outcome == "replaced"
    assert corrected.current_turn_override.contradiction_deterministic
    assert len(corrected.current_turn_override.contradicted_memory_ids) == 1

    state = sql_state(harness.database_path)
    active = [row for row in state["records"] if row["status"] == "active"]
    superseded = [row for row in state["records"] if row["status"] == "superseded"]
    assert len(active) == len(superseded) == 1
    assert active[0]["memory_type"] == "goal"
    assert active[0]["domain_key"] == "video_creation"
    assert active[0]["slot_key"] == "goal:video_creation:current_primary_goal"
    assert active[0]["canonical_payload"] == f'"{NEW_GOAL}"'
    assert active[0]["display_text"] == NEW_GOAL
    assert "no longer" not in active[0]["display_text"].casefold()
    assert superseded[0]["canonical_payload"] == f'"{OLD_GOAL}"'
    assert [row["relation_type"] for row in state["relations"]] == ["supersedes"]
    assert [row["operation_kind"] for row in state["operations"]] == ["create", "replace"]
    correction_sources = [
        row for row in state["sources"] if row["message_id"] == "video-correction"
    ]
    assert {row["assertion_role"] for row in correction_sources} == {
        "supports",
        "retracts_predecessor",
    }


def test_direct_replace_grammar_needs_no_model_call(tmp_path) -> None:
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    run_text(extraction, harness, f"I want to {OLD_GOAL}.", message_id="direct-old")
    result = run_text(
        extraction,
        harness,
        f"Correction: replace {OLD_GOAL} with {NEW_GOAL}.",
        message_id="direct-replace",
    )
    assert result.model_summary.called is False
    assert result.decisions[0].action is CandidateAction.REPLACE
    assert result.decisions[0].outcome == "replaced"


def test_clearly_remains_value_text_not_domain_or_global_style(tmp_path) -> None:
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    run_text(extraction, harness, f"I want to {OLD_GOAL}.", message_id="clearly-old")
    run_text(extraction, harness, CRITICAL_CORRECTION, message_id="clearly-new")
    active = [
        row for row in sql_state(harness.database_path)["records"] if row["status"] == "active"
    ]
    assert active[0]["domain_key"] == "video_creation"
    assert active[0]["memory_type"] == "goal"
    assert active[0]["canonical_payload"] == f'"{NEW_GOAL}"'
    assert active[0]["domain_key"] != "clearly"


def test_domain_specific_and_global_preferences_use_distinct_slots(tmp_path) -> None:
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    scoped = run_text(
        extraction,
        harness,
        "For video-editing advice, give me quick 15-minute drills.",
        message_id="scoped-preference",
    )
    global_style = run_text(
        extraction,
        harness,
        "Always answer me concisely.",
        message_id="global-preference",
    )
    assert scoped.status is global_style.status is ExtractionStatus.APPLIED
    preferences = sql_state(harness.database_path)["records"]
    identities = {(row["domain_key"], row["slot_key"]) for row in preferences}
    assert ("video_creation", "preference:video_creation:practice_advice_format") in identities
    assert ("global", "preference:global:verbosity") in identities


def test_preference_correction_replaces_old_global_style(tmp_path) -> None:
    from app.services.memory.adapters import GenericMemoryAdapter, StructuredMemoryInput
    from app.services.memory.idempotency import MemoryIdempotency

    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    GenericMemoryAdapter(harness.coordinator).create(
        harness.context,
        StructuredMemoryInput(
            memory_type=MemoryType.PREFERENCE,
            domain_key="global",
            slot_key="preference:global:verbosity",
            cardinality=Cardinality.EXCLUSIVE,
            canonical_value="verbose explanations",
            display_text="verbose explanations",
        ),
        idempotency_key=MemoryIdempotency.manual(
            harness.context.execution.owner_id, "verbose-style"
        ),
    )
    result = run_text(
        extraction,
        harness,
        "I don't prefer verbose explanations anymore. I prefer concise answers.",
        message_id="preference-correction",
    )
    assert result.decisions[0].action is CandidateAction.REPLACE
    records = sql_state(harness.database_path)["records"]
    active = next(row for row in records if row["status"] == "active")
    assert active["canonical_payload"] == '"concise answers"'


def test_category_correction_uses_authorized_prior_user_span(tmp_path) -> None:
    from app.services.memory.adapters import (
        ChatMemoryAdapter,
        GenericMemoryAdapter,
        StructuredMemoryInput,
    )
    from app.services.memory.extraction import FixtureExtractionModel
    from app.services.memory.extraction_contracts import (
        ConversationRole,
        ExtractionMode,
        TrustedConversationMessage,
    )
    from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator
    from app.services.memory.idempotency import MemoryIdempotency
    from tests.memory.extraction_helpers import extraction_input

    harness, _extraction, _diagnostics = extraction_harness(tmp_path)
    GenericMemoryAdapter(harness.coordinator).create(
        harness.context,
        StructuredMemoryInput(
            memory_type=MemoryType.PREFERENCE,
            domain_key="video_creation",
            slot_key="preference:video_creation:current_primary_goal",
            cardinality=Cardinality.EXCLUSIVE,
            canonical_value=NEW_GOAL,
            display_text=NEW_GOAL,
        ),
        idempotency_key=MemoryIdempotency.manual(
            harness.context.execution.owner_id, "wrong-category"
        ),
    )
    prior = f"I want to {NEW_GOAL}."
    current = "That is a goal, not a preference."
    value_start = prior.index(NEW_GOAL)
    current_span = {
        "message_id": "category-current",
        "start": 0,
        "end": len(current),
        "quoted_text": current,
    }
    prior_span = {
        "message_id": "category-prior",
        "start": value_start,
        "end": value_start + len(NEW_GOAL),
        "quoted_text": NEW_GOAL,
    }
    fixture = {
        "schema_version": 1,
        "assertions": [
            {
                "proposal_id": "category-assertion",
                "source_spans": [prior_span, current_span],
                "subject_hint": "user",
                "memory_type_hint": "goal",
                "domain_hint": "video_creation",
                "slot_hint": "current_primary_goal",
                "typed_value": NEW_GOAL,
                "display_hint": NEW_GOAL,
                "durability": "durable",
                "confidence": 0.99,
                "sensitivity_hint": "normal",
                "correction_group": "category-1",
                "explicit_type_change": True,
            }
        ],
        "retractions": [
            {
                "proposal_id": "category-retraction",
                "source_spans": [prior_span, current_span],
                "subject_hint": "user",
                "old_value_hint": NEW_GOAL,
                "memory_type_hint": "preference",
                "domain_hint": "video_creation",
                "confidence": 0.99,
                "correction_group": "category-1",
            }
        ],
        "exclusions": [],
    }
    model = FixtureExtractionModel({current: fixture})
    extraction = MemoryExtractionCoordinator(ChatMemoryAdapter(harness.coordinator), model=model)
    request, context = extraction_input(
        harness,
        current,
        message_id="category-current",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        supporting_window=(
            TrustedConversationMessage(
                message_id="category-prior",
                role=ConversationRole.USER,
                content=prior,
            ),
        ),
    )
    result = extraction.process(request, context)
    assert result.decisions[0].action is CandidateAction.REPLACE
    state = sql_state(harness.database_path)
    active = next(row for row in state["records"] if row["status"] == "active")
    assert active["memory_type"] == "goal"
    assert active["domain_key"] == "video_creation"


def test_explicit_domain_change_replaces_global_preference(tmp_path) -> None:
    from app.services.memory.adapters import (
        ChatMemoryAdapter,
        GenericMemoryAdapter,
        StructuredMemoryInput,
    )
    from app.services.memory.extraction import FixtureExtractionModel
    from app.services.memory.extraction_contracts import (
        ConversationRole,
        ExtractionMode,
        TrustedConversationMessage,
    )
    from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator
    from app.services.memory.idempotency import MemoryIdempotency
    from tests.memory.extraction_helpers import extraction_input

    harness, _extraction, _diagnostics = extraction_harness(tmp_path)
    old_value = "give me quick drills"
    GenericMemoryAdapter(harness.coordinator).create(
        harness.context,
        StructuredMemoryInput(
            memory_type=MemoryType.PREFERENCE,
            domain_key="global",
            slot_key="preference:global:format",
            cardinality=Cardinality.EXCLUSIVE,
            canonical_value=old_value,
            display_text=old_value,
        ),
        idempotency_key=MemoryIdempotency.manual(
            harness.context.execution.owner_id, "wrong-domain"
        ),
    )
    prior = "Give me quick drills."
    current = "That preference is about video editing, not my global response style."
    prior_span = {
        "message_id": "domain-prior",
        "start": 0,
        "end": len(old_value),
        "quoted_text": "Give me quick drills",
    }
    current_span = {
        "message_id": "domain-current",
        "start": 0,
        "end": len(current),
        "quoted_text": current,
    }
    fixture = {
        "schema_version": 1,
        "assertions": [
            {
                "proposal_id": "domain-assertion",
                "source_spans": [prior_span, current_span],
                "subject_hint": "user",
                "memory_type_hint": "preference",
                "domain_hint": "video_creation",
                "slot_hint": "format",
                "typed_value": old_value,
                "display_hint": old_value,
                "durability": "durable",
                "confidence": 0.99,
                "sensitivity_hint": "normal",
                "correction_group": "domain-1",
                "explicit_domain_change": True,
            }
        ],
        "retractions": [
            {
                "proposal_id": "domain-retraction",
                "source_spans": [prior_span, current_span],
                "subject_hint": "user",
                "old_value_hint": old_value,
                "memory_type_hint": "preference",
                "domain_hint": "global",
                "confidence": 0.99,
                "correction_group": "domain-1",
            }
        ],
        "exclusions": [],
    }
    model = FixtureExtractionModel({current: fixture})
    extraction = MemoryExtractionCoordinator(ChatMemoryAdapter(harness.coordinator), model=model)
    request, context = extraction_input(
        harness,
        current,
        message_id="domain-current",
        mode=ExtractionMode.POST_TURN_AUTOMATIC,
        supporting_window=(
            TrustedConversationMessage(
                message_id="domain-prior",
                role=ConversationRole.USER,
                content=prior,
            ),
        ),
    )
    result = extraction.process(request, context)
    assert result.decisions[0].action is CandidateAction.REPLACE
    active = next(
        row for row in sql_state(harness.database_path)["records"] if row["status"] == "active"
    )
    assert active["domain_key"] == "video_creation"
    assert active["slot_key"] == "preference:video_creation:format"


def test_pure_location_retraction_archives_instead_of_creating_not_pune(
    tmp_path,
) -> None:
    from app.services.memory.adapters import GenericMemoryAdapter, StructuredMemoryInput
    from app.services.memory.idempotency import MemoryIdempotency

    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    GenericMemoryAdapter(harness.coordinator).create(
        harness.context,
        StructuredMemoryInput(
            memory_type=MemoryType.IDENTITY,
            domain_key="global",
            slot_key="identity:global:current_location",
            cardinality=Cardinality.EXCLUSIVE,
            canonical_value="Pune",
            display_text="Pune",
        ),
        idempotency_key=MemoryIdempotency.manual(harness.context.execution.owner_id, "seed-pune"),
    )
    result = run_text(
        extraction,
        harness,
        "I no longer live in Pune.",
        message_id="leave-pune",
    )
    assert result.status is ExtractionStatus.APPLIED
    assert result.decisions[0].action is CandidateAction.RETRACT
    state = sql_state(harness.database_path)
    assert [row["status"] for row in state["records"]] == ["archived"]
    assert all(
        "not pune" not in str(row["canonical_payload"]).casefold() for row in state["records"]
    )


def test_not_only_is_not_a_retraction(tmp_path) -> None:
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    result = run_text(
        extraction,
        harness,
        "I prefer project-based learning, not only video courses.",
        message_id="not-only",
    )
    assert result.status is ExtractionStatus.APPLIED
    assert not result.preparse.retractions
    state = sql_state(harness.database_path)
    assert len(state["records"]) == 1
    assert state["records"][0]["status"] == "active"
    assert state["records"][0]["memory_type"] == "preference"


def test_additive_language_creates_two_independent_goals(tmp_path) -> None:
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    result = run_text(
        extraction,
        harness,
        "I still want edit YouTube videos, and I also want create Instagram reels.",
        message_id="additive-goals",
    )
    assert result.status is ExtractionStatus.APPLIED
    assert [item.action for item in result.decisions] == [
        CandidateAction.CREATE,
        CandidateAction.CREATE,
    ]
    records = sql_state(harness.database_path)["records"]
    assert len(records) == 2
    assert all(row["status"] == "active" for row in records)
    assert all(":independent:" in row["slot_key"] for row in records)


def test_hypothetical_and_third_party_text_do_not_activate(tmp_path) -> None:
    harness, extraction, _diagnostics = extraction_harness(tmp_path)
    hypothetical = run_text(
        extraction,
        harness,
        "Maybe I will learn Rust someday.",
        message_id="hypothetical",
    )
    third_party = run_text(
        extraction,
        harness,
        "My friend prefers Python.",
        message_id="third-party",
    )
    assert hypothetical.status is third_party.status is ExtractionStatus.NO_ACTION
    assert sql_state(harness.database_path)["records"] == []


def test_resolver_targets_all_conflicts_in_same_exclusive_slot() -> None:
    owner = "00000000-0000-4000-8000-000000000001"
    records = tuple(
        CanonicalMemorySnapshot(
            memory_id=uuid4(),
            owner_id=owner,
            subject_key="user",
            memory_type=MemoryType.GOAL,
            domain_key="video_creation",
            slot_key="goal:video_creation:current_primary_goal",
            cardinality=Cardinality.EXCLUSIVE,
            canonical_value=OLD_GOAL if index == 0 else "legacy conflicting video goal",
            display_text=OLD_GOAL if index == 0 else "legacy conflicting video goal",
            sensitivity=Sensitivity.NORMAL,
            status=MemoryLifecycleState.ACTIVE,
            revision=1,
        )
        for index in range(2)
    )
    from app.services.memory.contracts import (
        CandidateIntent,
        CandidateTargetHints,
        ValidatedCandidateProposal,
    )
    from app.services.memory.extraction_contracts import NormalizedExtractionCandidate

    proposal = ValidatedCandidateProposal(
        proposal_id=UUID("00000000-0000-4000-8000-000000000444"),
        intent=CandidateIntent.REPLACE,
        memory_type=MemoryType.GOAL,
        domain_key="video_creation",
        slot_key="goal:video_creation:current_primary_goal",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value=NEW_GOAL,
        display_text=NEW_GOAL,
        sensitivity=Sensitivity.NORMAL,
        confidence=0.99,
        importance=7,
        target_hints=CandidateTargetHints(old_value_phrases=(OLD_GOAL,)),
        evidence=(),
    )
    candidate = NormalizedExtractionCandidate(
        proposal=proposal,
        grounding_spans=(
            {
                "message_id": "m",
                "role": "assertion",
                "start": 0,
                "end": 1,
                "content_hash": "0" * 64,
            },
        ),
        old_value_hints=(OLD_GOAL,),
    )
    resolution = DeterministicCorrectionResolver().resolve(candidate, records)
    assert resolution.kind is CorrectionResolutionKind.REPLACE
    assert {item.memory_id for item in resolution.targets} == {item.memory_id for item in records}

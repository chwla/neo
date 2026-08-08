from __future__ import annotations

from uuid import UUID

import pytest

from app.services.memory.taxonomy import (
    DOMAIN_ALIASES,
    EXCLUSIVE_GOAL_ROLES,
    Cardinality,
    InitialDomain,
    MemoryIdentity,
    MemoryType,
    TaxonomyError,
    build_slot,
    inherit_predecessor_identity,
    normalize_unknown_domain,
    resolve_domain,
)


@pytest.mark.parametrize(
    ("text", "expected_alias"),
    [
        ("I edit videos for clients.", "edit videos"),
        ("I want to grow my YouTube creation workflow.", "youtube creation"),
        ("I make Instagram reels.", "instagram reels"),
        ("I am learning short-form video.", "short form video"),
        ("I want cinematic YouTube videos.", "cinematic youtube"),
    ],
)
def test_video_aliases_resolve_to_video_creation(text: str, expected_alias: str) -> None:
    resolution = resolve_domain(text)
    assert resolution.key == InitialDomain.VIDEO_CREATION.value
    assert resolution.matched_text == expected_alias


def test_initial_domain_set_is_exact_and_versioned() -> None:
    assert {domain.value for domain in InitialDomain} == {
        "global",
        "communication",
        "software_development",
        "learning",
        "career",
        "finance",
        "health_fitness",
        "travel",
        "video_creation",
        "gaming",
    }
    assert set(DOMAIN_ALIASES) == set(InitialDomain)


def test_clearly_can_never_be_an_unknown_domain() -> None:
    text = "I want to express myself clearly."
    with pytest.raises(TaxonomyError, match="unknown_domain_requires_grounded_topic"):
        resolve_domain(text)
    with pytest.raises(TaxonomyError, match="value_modifier_cannot_be_domain"):
        normalize_unknown_domain("clearly", source_text=text)


def test_known_topic_beats_trailing_value_adverb() -> None:
    resolution = resolve_domain("I want to create short Instagram reels clearly.")
    assert resolution.key == "video_creation"
    assert resolution.key != "clearly"


def test_unknown_domain_requires_an_explicit_grounded_phrase() -> None:
    resolution = resolve_domain(
        "I prefer astronomy advice in short daily exercises.",
        grounded_unknown_topic="astronomy",
    )
    assert resolution.key == "topic.astronomy"
    assert not resolution.is_known

    with pytest.raises(TaxonomyError, match="unknown_domain_must_be_grounded"):
        normalize_unknown_domain("watercolor", source_text="I enjoy astronomy.")


def test_domain_specific_advice_format_is_not_global_response_style() -> None:
    text = "I prefer video-editing advice in quick 15-minute drills."
    domain = resolve_domain(text)
    slot = build_slot(
        MemoryType.PREFERENCE,
        domain.key,
        preference_dimension="advice_format",
    )
    assert domain.key == "video_creation"
    assert slot.slot_key == "preference:video_creation:advice_format"
    assert slot.cardinality is Cardinality.EXCLUSIVE
    assert ":global:" not in slot.slot_key


def test_independent_goals_are_additive_with_non_value_entity_ids() -> None:
    first = build_slot(
        MemoryType.GOAL,
        InitialDomain.LEARNING,
        entity_id=UUID("00000000-0000-4000-8000-000000000301"),
    )
    second = build_slot(
        MemoryType.GOAL,
        InitialDomain.LEARNING,
        entity_id=UUID("00000000-0000-4000-8000-000000000302"),
    )
    assert first.cardinality is Cardinality.ADDITIVE
    assert second.cardinality is Cardinality.ADDITIVE
    assert first.slot_key != second.slot_key


@pytest.mark.parametrize("role", sorted(EXCLUSIVE_GOAL_ROLES))
def test_explicit_goal_roles_are_exclusive(role: str) -> None:
    slot = build_slot(MemoryType.GOAL, InitialDomain.VIDEO_CREATION, goal_role=role)
    assert slot.cardinality is Cardinality.EXCLUSIVE
    assert slot.slot_key == f"goal:video_creation:{role}"


def test_unsupported_goal_role_cannot_turn_every_goal_exclusive() -> None:
    with pytest.raises(TaxonomyError, match="unsupported_goal_role"):
        build_slot(
            MemoryType.GOAL,
            InitialDomain.LEARNING,
            goal_role="become_better_at_python",
            entity_id=UUID("00000000-0000-4000-8000-000000000303"),
        )


def test_slot_builder_has_no_canonical_value_parameter() -> None:
    with pytest.raises(TypeError):
        build_slot(  # type: ignore[call-arg]
            MemoryType.GOAL,
            InitialDomain.LEARNING,
            canonical_value="master Python",
        )


def test_identity_and_current_status_cardinality() -> None:
    identity = build_slot(
        MemoryType.IDENTITY,
        InitialDomain.GLOBAL,
        identity_key="preferred_name",
    )
    current_job = build_slot(
        MemoryType.EMPLOYMENT,
        InitialDomain.CAREER,
        current_field="current_status",
    )
    job_history = build_slot(
        MemoryType.EMPLOYMENT,
        InitialDomain.CAREER,
        entity_id=UUID("00000000-0000-4000-8000-000000000304"),
    )
    assert identity.cardinality is Cardinality.EXCLUSIVE
    assert current_job.cardinality is Cardinality.EXCLUSIVE
    assert job_history.cardinality is Cardinality.ADDITIVE


def test_correction_inherits_predecessor_domain_and_slot() -> None:
    predecessor = MemoryIdentity(
        memory_type=MemoryType.GOAL,
        domain_key="video_creation",
        slot_key="goal:video_creation:primary_output",
        cardinality=Cardinality.EXCLUSIVE,
    )
    inherited = inherit_predecessor_identity(predecessor)
    assert inherited == predecessor

    changed = inherit_predecessor_identity(
        predecessor,
        proposed_domain=InitialDomain.COMMUNICATION,
        explicit_domain_change=True,
    )
    assert changed.domain_key == "communication"
    assert changed.slot_key == "goal:communication:primary_output"

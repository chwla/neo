from __future__ import annotations

from uuid import UUID

from app.services.memory.taxonomy import (
    DOMAIN_ALIASES,
    Cardinality,
    InitialDomain,
    MemoryType,
    build_slot,
    resolve_domain,
)

ENTITY_ID = UUID("00000000-0000-4000-8000-000000000801")


def test_every_approved_domain_alias_resolves_to_its_declared_domain() -> None:
    for domain, aliases in DOMAIN_ALIASES.items():
        for alias in aliases:
            resolution = resolve_domain(f"My topic is {alias}.")
            assert resolution.key == domain.value
            assert resolution.is_known


def test_stable_slot_matrix_covers_every_phase0_cardinality_policy() -> None:
    expected = (
        (
            build_slot(MemoryType.IDENTITY, InitialDomain.GLOBAL, identity_key="preferred name"),
            "identity:global:preferred_name",
            Cardinality.EXCLUSIVE,
        ),
        (
            build_slot(
                MemoryType.PREFERENCE,
                InitialDomain.GLOBAL,
                preference_dimension="response tone",
            ),
            "preference:global:response_tone",
            Cardinality.EXCLUSIVE,
        ),
        (
            build_slot(
                MemoryType.PREFERENCE,
                InitialDomain.VIDEO_CREATION,
                preference_dimension="advice format",
            ),
            "preference:video_creation:advice_format",
            Cardinality.EXCLUSIVE,
        ),
        (
            build_slot(MemoryType.PROJECT, InitialDomain.CAREER, entity_id=ENTITY_ID),
            f"project:career:item:{ENTITY_ID}",
            Cardinality.ADDITIVE,
        ),
        (
            build_slot(MemoryType.EVENT, InitialDomain.TRAVEL, entity_id=ENTITY_ID),
            f"event:travel:item:{ENTITY_ID}",
            Cardinality.ADDITIVE,
        ),
        (
            build_slot(MemoryType.ACTIVITY, InitialDomain.HEALTH_FITNESS, entity_id=ENTITY_ID),
            f"activity:health_fitness:item:{ENTITY_ID}",
            Cardinality.ADDITIVE,
        ),
        (
            build_slot(MemoryType.KNOWLEDGE, InitialDomain.LEARNING, entity_id=ENTITY_ID),
            f"knowledge:learning:item:{ENTITY_ID}",
            Cardinality.ADDITIVE,
        ),
        (
            build_slot(MemoryType.EDUCATION, InitialDomain.LEARNING, entity_id=ENTITY_ID),
            f"education:learning:item:{ENTITY_ID}",
            Cardinality.ADDITIVE,
        ),
        (
            build_slot(MemoryType.EMPLOYMENT, InitialDomain.CAREER, entity_id=ENTITY_ID),
            f"employment:career:item:{ENTITY_ID}",
            Cardinality.ADDITIVE,
        ),
        (
            build_slot(
                MemoryType.EDUCATION,
                InitialDomain.LEARNING,
                current_field="current_status",
            ),
            "education:learning:current_status",
            Cardinality.EXCLUSIVE,
        ),
        (
            build_slot(
                MemoryType.EMPLOYMENT,
                InitialDomain.CAREER,
                current_field="current_status",
            ),
            "employment:career:current_status",
            Cardinality.EXCLUSIVE,
        ),
    )

    for slot, expected_key, expected_cardinality in expected:
        assert slot.slot_key == expected_key
        assert slot.cardinality is expected_cardinality

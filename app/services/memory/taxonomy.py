"""Deterministic Phase 0 taxonomy for Neo personal memory memory.

The taxonomy does not classify with an LLM and never derives a domain from the
last token or a slot from the remembered value.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.services.memory.versions import TAXONOMY_VERSION


class MemoryType(StrEnum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    GOAL = "goal"
    PROJECT = "project"
    EDUCATION = "education"
    EMPLOYMENT = "employment"
    ACTIVITY = "activity"
    EVENT = "event"
    KNOWLEDGE = "knowledge"


class MemoryField(StrEnum):
    """The six fields memory is organised into for the user.

    ``MemoryType`` stays the finer-grained storage type because it determines
    slot identity and cardinality.  A field is the durable user-facing grouping
    those types roll up into, so the set of fields can stay stable and small
    while types remain free to describe a fact precisely.
    """

    PROFILE = "profile"
    PREFERENCES = "preferences"
    GOALS = "goals"
    PROJECTS = "projects"
    EVENTS = "events"
    MISCELLANEOUS = "miscellaneous"


MEMORY_FIELD_FOR_TYPE: dict[MemoryType, MemoryField] = {
    MemoryType.IDENTITY: MemoryField.PROFILE,
    MemoryType.EDUCATION: MemoryField.PROFILE,
    MemoryType.EMPLOYMENT: MemoryField.PROFILE,
    MemoryType.PREFERENCE: MemoryField.PREFERENCES,
    MemoryType.GOAL: MemoryField.GOALS,
    MemoryType.PROJECT: MemoryField.PROJECTS,
    MemoryType.EVENT: MemoryField.EVENTS,
    MemoryType.ACTIVITY: MemoryField.EVENTS,
    MemoryType.KNOWLEDGE: MemoryField.MISCELLANEOUS,
}

# Only the projects field is bound to a single project.  Every other field is
# personal to the user rather than to a body of work, so it stays readable from
# any chat, inside or outside a project.
PROJECT_SCOPED_FIELD = MemoryField.PROJECTS


def memory_field_for_type(memory_type: MemoryType | str) -> MemoryField:
    """Return the user-facing field a storage type belongs to."""

    try:
        resolved = MemoryType(memory_type)
    except ValueError:
        return MemoryField.MISCELLANEOUS
    return MEMORY_FIELD_FOR_TYPE.get(resolved, MemoryField.MISCELLANEOUS)


class InitialDomain(StrEnum):
    GLOBAL = "global"
    COMMUNICATION = "communication"
    SOFTWARE_DEVELOPMENT = "software_development"
    LEARNING = "learning"
    CAREER = "career"
    FINANCE = "finance"
    HEALTH_FITNESS = "health_fitness"
    TRAVEL = "travel"
    VIDEO_CREATION = "video_creation"
    GAMING = "gaming"


class Cardinality(StrEnum):
    EXCLUSIVE = "exclusive"
    ADDITIVE = "additive"


DOMAIN_ALIASES: dict[InitialDomain, tuple[str, ...]] = {
    InitialDomain.GLOBAL: ("global", "across all topics", "for every topic"),
    InitialDomain.COMMUNICATION: (
        "communication",
        "public speaking",
        "presentation skills",
        "writing",
    ),
    InitialDomain.SOFTWARE_DEVELOPMENT: (
        "software development",
        "programming",
        "coding",
        "code review",
    ),
    InitialDomain.LEARNING: ("learning", "studying", "study skills"),
    InitialDomain.CAREER: (
        "career",
        "job search",
        "job interview",
        "professional development",
    ),
    InitialDomain.FINANCE: ("finance", "financial", "budgeting", "investing"),
    InitialDomain.HEALTH_FITNESS: (
        "health and fitness",
        "health fitness",
        "fitness",
        "exercise",
        "workout",
        "nutrition",
    ),
    InitialDomain.TRAVEL: ("travel", "trip planning", "vacation planning"),
    InitialDomain.VIDEO_CREATION: (
        "video creation",
        "video editing",
        "edit videos",
        "youtube creation",
        "youtube videos",
        "cinematic youtube",
        "instagram reels",
        "short form video",
        "short form videos",
        "reels creation",
    ),
    InitialDomain.GAMING: ("gaming", "video games", "computer games"),
}

EXCLUSIVE_GOAL_ROLES = frozenset({"primary_output", "current_primary_goal"})
GLOBAL_RESPONSE_STYLE_DIMENSIONS = frozenset({"verbosity", "tone", "format", "explanation_depth"})
CURRENT_STATUS_FIELD = "current_status"

# These can modify a value but cannot identify a topic on their own.
VALUE_ONLY_DOMAIN_TERMS = frozenset(
    {
        "better",
        "briefly",
        "carefully",
        "clearly",
        "confidently",
        "concisely",
        "detailed",
        "quickly",
        "short",
        "simply",
    }
)


class TaxonomyError(ValueError):
    """Raised when deterministic taxonomy rules cannot produce a safe identity."""


@dataclass(frozen=True)
class DomainResolution:
    key: str
    is_known: bool
    resolution: str
    matched_text: str
    taxonomy_version: str = TAXONOMY_VERSION


@dataclass(frozen=True)
class SlotDefinition:
    memory_type: MemoryType
    domain_key: str
    slot_key: str
    cardinality: Cardinality
    taxonomy_version: str = TAXONOMY_VERSION


@dataclass(frozen=True)
class MemoryIdentity:
    memory_type: MemoryType
    domain_key: str
    slot_key: str
    cardinality: Cardinality


def _words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.findall(r"[a-z0-9]+", normalized)


def _normalized_phrase(value: str) -> str:
    return " ".join(_words(value))


def _slug(value: str, *, label: str) -> str:
    words = _words(value)
    if not words:
        raise TaxonomyError(f"{label}_required")
    return "_".join(words)


def _contains_phrase(source_text: str, phrase: str) -> bool:
    source = f" {_normalized_phrase(source_text)} "
    normalized_phrase = f" {_normalized_phrase(phrase)} "
    return bool(normalized_phrase.strip()) and normalized_phrase in source


def _known_domain(value: str | InitialDomain) -> InitialDomain | None:
    if isinstance(value, InitialDomain):
        return value
    normalized = _slug(value, label="domain")
    try:
        return InitialDomain(normalized)
    except ValueError:
        return None


def validate_domain_key(value: str | InitialDomain) -> str:
    known = _known_domain(value)
    if known is not None:
        return known.value
    normalized = str(value).strip().casefold()
    if re.fullmatch(r"topic\.[a-z0-9]+(?:_[a-z0-9]+)*", normalized):
        return normalized
    raise TaxonomyError("invalid_domain_key")


def normalize_unknown_domain(topic_phrase: str, *, source_text: str) -> str:
    """Return a stable unknown-topic key only from an explicitly grounded phrase."""

    normalized = _normalized_phrase(topic_phrase)
    words = normalized.split()
    if not words:
        raise TaxonomyError("unknown_domain_requires_grounded_topic")
    if len(words) == 1 and words[0] in VALUE_ONLY_DOMAIN_TERMS:
        raise TaxonomyError("value_modifier_cannot_be_domain")
    if not _contains_phrase(source_text, topic_phrase):
        raise TaxonomyError("unknown_domain_must_be_grounded")
    return f"topic.{normalized.replace(' ', '_')}"


def resolve_domain(
    source_text: str,
    *,
    explicit_domain: str | InitialDomain | None = None,
    grounded_unknown_topic: str | None = None,
) -> DomainResolution:
    """Resolve a known alias or an explicitly supplied, grounded unknown topic.

    There is deliberately no final-token fallback. If neither a known alias nor
    a grounded unknown topic is available, the function fails closed.
    """

    if explicit_domain is not None:
        known = _known_domain(explicit_domain)
        if known is not None:
            return DomainResolution(
                key=known.value,
                is_known=True,
                resolution="explicit",
                matched_text=str(explicit_domain),
            )
        key = normalize_unknown_domain(str(explicit_domain), source_text=source_text)
        return DomainResolution(
            key=key,
            is_known=False,
            resolution="grounded_unknown",
            matched_text=str(explicit_domain),
        )

    aliases = sorted(
        (
            (alias, domain)
            for domain, domain_aliases in DOMAIN_ALIASES.items()
            for alias in domain_aliases
        ),
        key=lambda item: (len(_words(item[0])), len(item[0])),
        reverse=True,
    )
    for alias, domain in aliases:
        if _contains_phrase(source_text, alias):
            return DomainResolution(
                key=domain.value,
                is_known=True,
                resolution="alias",
                matched_text=alias,
            )

    if grounded_unknown_topic is not None:
        key = normalize_unknown_domain(grounded_unknown_topic, source_text=source_text)
        return DomainResolution(
            key=key,
            is_known=False,
            resolution="grounded_unknown",
            matched_text=grounded_unknown_topic,
        )
    raise TaxonomyError("unknown_domain_requires_grounded_topic")


def _entity_component(entity_id: UUID | str | None) -> str:
    if entity_id is None:
        raise TaxonomyError("additive_memory_requires_entity_id")
    try:
        return str(UUID(str(entity_id)))
    except (TypeError, ValueError) as exc:
        raise TaxonomyError("entity_id_must_be_uuid") from exc


def build_slot(
    memory_type: MemoryType,
    domain_key: str | InitialDomain,
    *,
    identity_key: str | None = None,
    preference_dimension: str | None = None,
    goal_role: str | None = None,
    entity_id: UUID | str | None = None,
    current_field: str | None = None,
) -> SlotDefinition:
    """Build a slot from semantic dimensions, never from a remembered value."""

    domain = validate_domain_key(domain_key)

    if memory_type is MemoryType.IDENTITY:
        if domain != InitialDomain.GLOBAL.value:
            raise TaxonomyError("identity_domain_must_be_global")
        key = _slug(identity_key or "", label="identity_key")
        return SlotDefinition(memory_type, domain, f"identity:global:{key}", Cardinality.EXCLUSIVE)

    if memory_type is MemoryType.PREFERENCE:
        dimension = _slug(preference_dimension or "", label="preference_dimension")
        return SlotDefinition(
            memory_type,
            domain,
            f"preference:{domain}:{dimension}",
            Cardinality.EXCLUSIVE,
        )

    if memory_type is MemoryType.GOAL:
        role = _slug(goal_role, label="goal_role") if goal_role else "independent_goal"
        if role in EXCLUSIVE_GOAL_ROLES:
            return SlotDefinition(
                memory_type,
                domain,
                f"goal:{domain}:{role}",
                Cardinality.EXCLUSIVE,
            )
        if role != "independent_goal":
            raise TaxonomyError("unsupported_goal_role")
        entity = _entity_component(entity_id)
        return SlotDefinition(
            memory_type,
            domain,
            f"goal:{domain}:independent:{entity}",
            Cardinality.ADDITIVE,
        )

    if memory_type in {MemoryType.EDUCATION, MemoryType.EMPLOYMENT} and current_field:
        field = _slug(current_field, label="current_field")
        if field != CURRENT_STATUS_FIELD:
            raise TaxonomyError("unsupported_current_status_field")
        return SlotDefinition(
            memory_type,
            domain,
            f"{memory_type.value}:{domain}:{field}",
            Cardinality.EXCLUSIVE,
        )

    if memory_type in {
        MemoryType.PROJECT,
        MemoryType.EDUCATION,
        MemoryType.EMPLOYMENT,
        MemoryType.ACTIVITY,
        MemoryType.EVENT,
        MemoryType.KNOWLEDGE,
    }:
        entity = _entity_component(entity_id)
        return SlotDefinition(
            memory_type,
            domain,
            f"{memory_type.value}:{domain}:item:{entity}",
            Cardinality.ADDITIVE,
        )

    raise TaxonomyError("unsupported_slot_policy")


def inherit_predecessor_identity(
    predecessor: MemoryIdentity,
    *,
    proposed_domain: str | InitialDomain | None = None,
    proposed_slot: str | None = None,
    explicit_domain_change: bool = False,
    explicit_slot_change: bool = False,
) -> MemoryIdentity:
    """Inherit correction identity unless the user explicitly changes it."""

    if proposed_domain is not None and not explicit_domain_change:
        raise TaxonomyError("proposed_domain_requires_explicit_change")
    if proposed_slot is not None and not explicit_slot_change:
        raise TaxonomyError("proposed_slot_requires_explicit_change")

    domain = (
        validate_domain_key(proposed_domain)
        if explicit_domain_change and proposed_domain is not None
        else predecessor.domain_key
    )
    if explicit_domain_change and proposed_domain is None:
        raise TaxonomyError("explicit_domain_change_requires_domain")

    if explicit_slot_change:
        if not proposed_slot:
            raise TaxonomyError("explicit_slot_change_requires_slot")
        slot = proposed_slot
    elif explicit_domain_change:
        parts = predecessor.slot_key.split(":")
        if len(parts) < 3 or parts[0] != predecessor.memory_type.value:
            raise TaxonomyError("invalid_predecessor_slot")
        parts[1] = domain
        slot = ":".join(parts)
    else:
        slot = predecessor.slot_key

    slot_parts = slot.split(":")
    if len(slot_parts) < 3:
        raise TaxonomyError("invalid_slot")
    if slot_parts[0] != predecessor.memory_type.value or slot_parts[1] != domain:
        raise TaxonomyError("slot_does_not_match_identity")
    return MemoryIdentity(
        memory_type=predecessor.memory_type,
        domain_key=domain,
        slot_key=slot,
        cardinality=predecessor.cardinality,
    )

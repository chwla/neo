"""Tier 1 — deterministic taxonomy (plan section TAX).

The taxonomy decides a memory's identity: its domain, its slot, and whether the
slot holds one value or many.  Everything downstream — duplicate detection,
replacement, recall by slot — is built on it, so a wrong answer here is not a
wrong answer in one place, it is silent data corruption everywhere.

The two rules worth keeping in view while reading these tests: a domain is never
guessed from the last word of a sentence, and a slot is never derived from the
value being remembered.  Both exist because the earlier versions did guess, and
"I want to get better at running" filed itself under a `running` domain that
nothing else ever matched.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.memory.taxonomy import (
    DOMAIN_ALIASES,
    EXCLUSIVE_GOAL_ROLES,
    MEMORY_FIELD_FOR_TYPE,
    PROJECT_SCOPED_FIELD,
    VALUE_ONLY_DOMAIN_TERMS,
    Cardinality,
    InitialDomain,
    MemoryField,
    MemoryIdentity,
    MemoryType,
    TaxonomyError,
    build_slot,
    inherit_predecessor_identity,
    memory_field_for_type,
    normalize_unknown_domain,
    resolve_domain,
    validate_domain_key,
)

ENTITY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class TestDomainResolution:
    @pytest.mark.parametrize(
        ("domain", "alias"),
        [(domain, alias) for domain, aliases in DOMAIN_ALIASES.items() for alias in aliases],
    )
    def test_every_alias_resolves_to_its_own_domain(
        self, domain: InitialDomain, alias: str
    ) -> None:
        """TAX-01 — every alias we advertise actually works."""

        resolved = resolve_domain(f"I want to get better at {alias} this year")
        assert resolved.key == domain.value
        assert resolved.is_known is True
        assert resolved.resolution == "alias"

    def test_the_longest_matching_alias_wins(self) -> None:
        """TAX-02 — "video editing" must not lose to a shorter overlapping alias."""

        resolved = resolve_domain("I spend my evenings on short form video work")
        assert resolved.key == InitialDomain.VIDEO_CREATION.value
        assert resolved.matched_text == "short form video"

    def test_an_explicit_domain_overrides_alias_matching(self) -> None:
        """TAX-03"""

        resolved = resolve_domain(
            "I want to get better at coding",
            explicit_domain=InitialDomain.LEARNING,
        )
        assert resolved.key == InitialDomain.LEARNING.value
        assert resolved.resolution == "explicit"

    def test_no_alias_and_no_grounded_topic_fails_closed(self) -> None:
        """TAX-04 — there is deliberately no last-token fallback."""

        with pytest.raises(TaxonomyError, match="unknown_domain_requires_grounded_topic"):
            resolve_domain("I want to get better at sourdough")

    def test_an_alias_inside_a_longer_word_does_not_match(self) -> None:
        """TAX-05 — matching is word-bounded, so "scoding" is not "coding"."""

        with pytest.raises(TaxonomyError):
            resolve_domain("I have been scoding all week")

    @pytest.mark.parametrize("text", ["CODING", "Coding", "ｃｏｄｉｎｇ"])
    def test_alias_matching_is_case_and_unicode_insensitive(self, text: str) -> None:
        """TAX-06 — NFKC first, then case fold."""

        assert resolve_domain(f"I want to improve at {text}").key == (
            InitialDomain.SOFTWARE_DEVELOPMENT.value
        )

    def test_a_grounded_unknown_topic_becomes_a_topic_key(self) -> None:
        """TAX-07"""

        resolved = resolve_domain(
            "I want to get better at sourdough baking",
            grounded_unknown_topic="sourdough baking",
        )
        assert resolved.key == "topic.sourdough_baking"
        assert resolved.is_known is False
        assert resolved.resolution == "grounded_unknown"

    def test_an_unknown_topic_absent_from_the_text_is_rejected(self) -> None:
        """TAX-08 — the model cannot invent a topic the user never said."""

        with pytest.raises(TaxonomyError, match="unknown_domain_must_be_grounded"):
            resolve_domain(
                "I want to get better at sourdough baking",
                grounded_unknown_topic="competitive cheesemaking",
            )

    @pytest.mark.parametrize("term", sorted(VALUE_ONLY_DOMAIN_TERMS))
    def test_a_value_only_word_cannot_be_a_domain_on_its_own(self, term: str) -> None:
        """TAX-09 — "briefly" describes how, not what about."""

        with pytest.raises(TaxonomyError, match="value_modifier_cannot_be_domain"):
            normalize_unknown_domain(term, source_text=f"Answer me {term} from now on")

    def test_a_phrase_containing_a_value_only_word_is_allowed(self) -> None:
        """TAX-10 — the guard is for the bare word, not any phrase containing it."""

        key = normalize_unknown_domain(
            "short story writing",
            source_text="I want to get better at short story writing",
        )
        assert key == "topic.short_story_writing"

    @pytest.mark.parametrize("domain", list(InitialDomain))
    def test_every_known_domain_validates(self, domain: InitialDomain) -> None:
        """TAX-11a"""

        assert validate_domain_key(domain) == domain.value
        assert validate_domain_key(domain.value) == domain.value

    def test_a_well_formed_topic_key_validates(self) -> None:
        """TAX-11b"""

        assert validate_domain_key("topic.sourdough_baking") == "topic.sourdough_baking"

    @pytest.mark.parametrize(
        "value",
        [
            "topic.",
            "topic",
            "topic._leading",
            "topic.trailing_",
            "topic.double__underscore",
            "topic.has space",
            "topic.punctuation!",
            "not_a_domain",
        ],
    )
    def test_a_malformed_domain_key_is_rejected(self, value: str) -> None:
        """TAX-12"""

        with pytest.raises(TaxonomyError, match="invalid_domain_key"):
            validate_domain_key(value)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("topic.Sourdough", "topic.sourdough"),
            ("TOPIC.SOURDOUGH", "topic.sourdough"),
            ("  topic.sourdough  ", "topic.sourdough"),
        ],
    )
    def test_a_topic_key_is_case_folded_rather_than_rejected(
        self, value: str, expected: str
    ) -> None:
        """TAX-12b — pinning current behaviour, not endorsing it.

        ``validate_domain_key`` case-folds and strips before matching its
        pattern, so an upper-case topic key normalises instead of failing.  That
        is defensible (it makes the key canonical), but it is a choice rather
        than an obvious consequence, so it is pinned here: changing it should be
        a deliberate decision that breaks a test, not a silent drift.
        """

        assert validate_domain_key(value) == expected

    @pytest.mark.parametrize("value", ["", "   ", "!!!", "..."])
    def test_an_empty_or_punctuation_only_domain_is_rejected(self, value: str) -> None:
        """TAX-13"""

        with pytest.raises(TaxonomyError):
            validate_domain_key(value)


class TestSlotConstruction:
    def test_identity_outside_the_global_domain_is_rejected(self) -> None:
        """TAX-14 — who you are is not domain-specific."""

        with pytest.raises(TaxonomyError, match="identity_domain_must_be_global"):
            build_slot(MemoryType.IDENTITY, InitialDomain.CAREER, identity_key="name")

    def test_identity_builds_an_exclusive_global_slot(self) -> None:
        """TAX-15"""

        slot = build_slot(MemoryType.IDENTITY, InitialDomain.GLOBAL, identity_key="name")
        assert slot.slot_key == "identity:global:name"
        assert slot.cardinality is Cardinality.EXCLUSIVE

    def test_identity_without_a_key_is_rejected(self) -> None:
        """TAX-16"""

        with pytest.raises(TaxonomyError, match="identity_key_required"):
            build_slot(MemoryType.IDENTITY, InitialDomain.GLOBAL)

    def test_preference_builds_a_domain_scoped_exclusive_slot(self) -> None:
        """TAX-17"""

        slot = build_slot(
            MemoryType.PREFERENCE,
            InitialDomain.COMMUNICATION,
            preference_dimension="verbosity",
        )
        assert slot.slot_key == "preference:communication:verbosity"
        assert slot.cardinality is Cardinality.EXCLUSIVE

    def test_preference_without_a_dimension_is_rejected(self) -> None:
        """TAX-18"""

        with pytest.raises(TaxonomyError, match="preference_dimension_required"):
            build_slot(MemoryType.PREFERENCE, InitialDomain.COMMUNICATION)

    @pytest.mark.parametrize("role", sorted(EXCLUSIVE_GOAL_ROLES))
    def test_an_exclusive_goal_role_builds_a_three_part_slot(self, role: str) -> None:
        """TAX-19 — you can only have one current primary goal per domain."""

        slot = build_slot(MemoryType.GOAL, InitialDomain.LEARNING, goal_role=role)
        assert slot.slot_key == f"goal:learning:{role}"
        assert slot.cardinality is Cardinality.EXCLUSIVE

    def test_a_goal_with_no_role_is_an_independent_additive_goal(self) -> None:
        """TAX-20 — you can hold many independent goals at once."""

        slot = build_slot(MemoryType.GOAL, InitialDomain.LEARNING, entity_id=ENTITY)
        assert slot.slot_key == f"goal:learning:independent:{ENTITY}"
        assert slot.cardinality is Cardinality.ADDITIVE

    def test_an_unrecognised_goal_role_is_rejected(self) -> None:
        """TAX-21"""

        with pytest.raises(TaxonomyError, match="unsupported_goal_role"):
            build_slot(MemoryType.GOAL, InitialDomain.LEARNING, goal_role="stretch_goal")

    def test_an_additive_goal_without_an_entity_id_is_rejected(self) -> None:
        """TAX-22 — without one, every independent goal would collide."""

        with pytest.raises(TaxonomyError, match="additive_memory_requires_entity_id"):
            build_slot(MemoryType.GOAL, InitialDomain.LEARNING)

    def test_a_non_uuid_entity_id_is_rejected(self) -> None:
        """TAX-23"""

        with pytest.raises(TaxonomyError, match="entity_id_must_be_uuid"):
            build_slot(MemoryType.GOAL, InitialDomain.LEARNING, entity_id="not-a-uuid")

    @pytest.mark.parametrize("memory_type", [MemoryType.EDUCATION, MemoryType.EMPLOYMENT])
    def test_current_status_builds_an_exclusive_slot(self, memory_type: MemoryType) -> None:
        """TAX-24 — you hold one current job and one current course of study."""

        slot = build_slot(
            memory_type,
            InitialDomain.CAREER,
            current_field="current_status",
        )
        assert slot.slot_key == f"{memory_type.value}:career:current_status"
        assert slot.cardinality is Cardinality.EXCLUSIVE

    def test_an_unsupported_current_field_is_rejected(self) -> None:
        """TAX-25"""

        with pytest.raises(TaxonomyError, match="unsupported_current_status_field"):
            build_slot(
                MemoryType.EMPLOYMENT,
                InitialDomain.CAREER,
                current_field="previous_status",
            )

    @pytest.mark.parametrize(
        "memory_type",
        [
            MemoryType.PROJECT,
            MemoryType.EDUCATION,
            MemoryType.EMPLOYMENT,
            MemoryType.ACTIVITY,
            MemoryType.EVENT,
            MemoryType.KNOWLEDGE,
        ],
    )
    def test_item_types_build_additive_item_slots(self, memory_type: MemoryType) -> None:
        """TAX-26"""

        slot = build_slot(memory_type, InitialDomain.GLOBAL, entity_id=ENTITY)
        assert slot.slot_key == f"{memory_type.value}:global:item:{ENTITY}"
        assert slot.cardinality is Cardinality.ADDITIVE

    def test_slot_building_is_deterministic(self) -> None:
        """TAX-27"""

        first = build_slot(MemoryType.GOAL, InitialDomain.LEARNING, entity_id=ENTITY)
        second = build_slot(MemoryType.GOAL, InitialDomain.LEARNING, entity_id=ENTITY)
        assert first == second

    def test_the_remembered_value_never_reaches_the_slot(self) -> None:
        """TAX-28 — the invariant the whole module exists to hold.

        Two entirely different facts with the same semantic dimensions must land
        in the same slot, because the slot describes *what kind of thing* is
        being remembered, not *what* was remembered.
        """

        first = build_slot(
            MemoryType.PREFERENCE,
            InitialDomain.COMMUNICATION,
            preference_dimension="verbosity",
        )
        second = build_slot(
            MemoryType.PREFERENCE,
            InitialDomain.COMMUNICATION,
            preference_dimension="verbosity",
        )
        assert first.slot_key == second.slot_key


class TestFieldRollup:
    @pytest.mark.parametrize("memory_type", list(MemoryType))
    def test_every_memory_type_rolls_up_to_a_field(self, memory_type: MemoryType) -> None:
        """TAX-29 — no type may fall through the user-facing grouping."""

        assert memory_type in MEMORY_FIELD_FOR_TYPE
        assert isinstance(memory_field_for_type(memory_type), MemoryField)

    def test_an_unknown_type_string_falls_back_rather_than_raising(self) -> None:
        """TAX-30 — a stored row from a future version must still render."""

        assert memory_field_for_type("not_a_type") is MemoryField.MISCELLANEOUS

    def test_only_projects_is_project_scoped(self) -> None:
        """TAX-31 — everything else is personal and readable from any chat."""

        assert PROJECT_SCOPED_FIELD is MemoryField.PROJECTS


class TestPredecessorInheritance:
    @staticmethod
    def _predecessor(
        *,
        memory_type: MemoryType = MemoryType.GOAL,
        domain_key: str = "learning",
        slot_key: str | None = None,
        cardinality: Cardinality = Cardinality.ADDITIVE,
    ) -> MemoryIdentity:
        return MemoryIdentity(
            memory_type=memory_type,
            domain_key=domain_key,
            slot_key=slot_key or f"goal:{domain_key}:independent:{ENTITY}",
            cardinality=cardinality,
        )

    def test_identity_is_inherited_verbatim_by_default(self) -> None:
        """TAX-32 — a correction keeps the fact where the user filed it."""

        predecessor = self._predecessor()
        inherited = inherit_predecessor_identity(predecessor)
        assert inherited == predecessor

    def test_proposing_a_domain_without_permission_is_rejected(self) -> None:
        """TAX-33 — the model cannot quietly refile a fact."""

        with pytest.raises(TaxonomyError, match="proposed_domain_requires_explicit_change"):
            inherit_predecessor_identity(self._predecessor(), proposed_domain="career")

    def test_proposing_a_slot_without_permission_is_rejected(self) -> None:
        """TAX-34"""

        with pytest.raises(TaxonomyError, match="proposed_slot_requires_explicit_change"):
            inherit_predecessor_identity(
                self._predecessor(),
                proposed_slot=f"goal:learning:independent:{uuid4()}",
            )

    def test_an_explicit_domain_change_without_a_domain_is_rejected(self) -> None:
        """TAX-35"""

        with pytest.raises(TaxonomyError, match="explicit_domain_change_requires_domain"):
            inherit_predecessor_identity(self._predecessor(), explicit_domain_change=True)

    def test_an_explicit_slot_change_without_a_slot_is_rejected(self) -> None:
        """TAX-36"""

        with pytest.raises(TaxonomyError, match="explicit_slot_change_requires_slot"):
            inherit_predecessor_identity(self._predecessor(), explicit_slot_change=True)

    def test_a_domain_change_rewrites_only_the_domain_part_of_the_slot(self) -> None:
        """TAX-37 — the entity component survives, so it stays the same goal."""

        inherited = inherit_predecessor_identity(
            self._predecessor(),
            proposed_domain="career",
            explicit_domain_change=True,
        )
        assert inherited.domain_key == "career"
        assert inherited.slot_key == f"goal:career:independent:{ENTITY}"

    def test_a_predecessor_slot_with_the_wrong_type_prefix_is_rejected(self) -> None:
        """TAX-38"""

        predecessor = self._predecessor(slot_key=f"preference:learning:independent:{ENTITY}")
        with pytest.raises(TaxonomyError, match="invalid_predecessor_slot"):
            inherit_predecessor_identity(
                predecessor,
                proposed_domain="career",
                explicit_domain_change=True,
            )

    def test_a_proposed_slot_inconsistent_with_the_identity_is_rejected(self) -> None:
        """TAX-39"""

        with pytest.raises(TaxonomyError, match="slot_does_not_match_identity"):
            inherit_predecessor_identity(
                self._predecessor(),
                proposed_slot=f"preference:learning:independent:{ENTITY}",
                explicit_slot_change=True,
            )

    def test_cardinality_is_always_inherited(self) -> None:
        """TAX-40 — a correction cannot turn a one-of slot into a many-of slot."""

        predecessor = self._predecessor(cardinality=Cardinality.EXCLUSIVE)
        inherited = inherit_predecessor_identity(
            predecessor,
            proposed_domain="career",
            explicit_domain_change=True,
        )
        assert inherited.cardinality is Cardinality.EXCLUSIVE

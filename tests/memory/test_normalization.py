"""Tier 1 — deterministic normalization (plan section NRM).

Normalization is where a proposal becomes a storable fact: text is folded to one
canonical shape, the value is turned into stable bytes, and those bytes become
the fingerprint that decides whether this is a new memory or one we already
hold.  A fingerprint that is too strict duplicates facts; one that is too loose
merges facts that were never the same.  Both failures have been seen in this
codebase, which is why the folding rules get this much attention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.services.memory.contracts import (
    CandidateIntent,
    CandidateTargetHints,
    EvidenceRole,
    EvidenceSpan,
    MemorySource,
    Sensitivity,
    SourceKind,
)
from app.services.memory.crypto import KeyedDigest
from app.services.memory.local_crypto import LocalMemoryCrypto
from app.services.memory.normalization import (
    ALLOWED_METADATA_KEYS,
    MAX_EVIDENCE_TEXT_CHARS,
    MAX_METADATA_JSON_BYTES,
    MemoryNormalizationError,
    canonical_fingerprint,
    canonical_json_bytes,
    compatible_refinement,
    normalize_candidate,
    normalize_metadata,
    normalize_record_value,
    normalize_source,
    normalize_text,
    operation_request_hash,
    validate_command_versions,
)
from app.services.memory.taxonomy import Cardinality, MemoryIdentity, MemoryType
from tests.memory import factories
from tests.memory.conftest import OWNER_ID

GOAL_SLOT = factories.DEFAULT_GOAL_SLOT


def _fingerprint(
    crypto: LocalMemoryCrypto,
    *,
    value: object = "improve at urban sketching",
    owner: str = OWNER_ID,
    subject_key: str = "user",
    memory_type: MemoryType = MemoryType.GOAL,
    domain_key: str = "global",
    slot_key: str = GOAL_SLOT,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    scope_type: str = "global",
    scope_project_id: str | None = None,
) -> str:
    return canonical_fingerprint(
        owner_id=owner,
        subject_key=subject_key,
        memory_type=memory_type,
        domain_key=domain_key,
        slot_key=slot_key,
        canonical_value=value,
        sensitivity=sensitivity,
        keyed_provider=crypto,
        scope_type=scope_type,
        scope_project_id=scope_project_id,
    )


class TestTextNormalization:
    def test_whitespace_is_collapsed_and_unicode_is_folded(self) -> None:
        """NRM-01"""

        assert normalize_text("  hello\t\n  world  ", code="x") == "hello world"
        assert normalize_text("ｈｅｌｌｏ", code="x") == "hello"

    @pytest.mark.parametrize("value", ["", "   ", "\t\n", " "])
    def test_empty_input_raises_with_the_supplied_code(self, value: str) -> None:
        """NRM-02"""

        with pytest.raises(MemoryNormalizationError) as excinfo:
            normalize_text(value, code="subject_required")
        assert excinfo.value.code == "subject_required"

    def test_a_value_over_the_limit_raises_a_too_long_code(self) -> None:
        """NRM-03"""

        with pytest.raises(MemoryNormalizationError) as excinfo:
            normalize_text("x" * 11, code="display", limit=10)
        assert excinfo.value.code == "display_too_long"

    def test_a_value_exactly_at_the_limit_is_accepted(self) -> None:
        """NRM-03b — boundaries are where off-by-one lives."""

        assert normalize_text("x" * 10, code="display", limit=10) == "x" * 10


class TestCanonicalJson:
    def test_object_keys_are_sorted_recursively(self) -> None:
        """NRM-04"""

        encoded = canonical_json_bytes({"b": {"z": 1, "a": 2}, "a": 3})
        assert encoded == b'{"a":3,"b":{"a":2,"z":1}}'

    def test_equal_values_built_in_different_orders_encode_identically(self) -> None:
        """NRM-05 — the property the fingerprint depends on."""

        first = canonical_json_bytes({"a": 1, "b": [1, 2], "c": {"x": 1, "y": 2}})
        second = canonical_json_bytes({"c": {"y": 2, "x": 1}, "b": [1, 2], "a": 1})
        assert first == second

    def test_non_ascii_survives_unescaped(self) -> None:
        """NRM-06 — escaping would make the same fact hash two ways."""

        assert canonical_json_bytes("café") == '"café"'.encode()

    @pytest.mark.parametrize(
        "value",
        [None, {"a": None}, [None], {"a": {"b": None}}],
    )
    def test_a_null_anywhere_is_rejected(self, value: object) -> None:
        """NRM-07 — a memory records what is true, never what is absent."""

        with pytest.raises(MemoryNormalizationError):
            canonical_json_bytes(value)

    def test_nested_structures_normalise_recursively(self) -> None:
        """NRM-08"""

        encoded = canonical_json_bytes({"a": ["  x  y  ", {"  k  ": "  v  "}]})
        assert json.loads(encoded) == {"a": ["x y", {"k": "v"}]}

    @pytest.mark.parametrize("value", [1, 1.5, True, False, 0])
    def test_scalars_pass_through_unchanged(self, value: object) -> None:
        """NRM-09 — no float coercion, no truthiness games."""

        assert json.loads(canonical_json_bytes(value)) == value

    def test_an_over_long_object_key_is_rejected(self) -> None:
        """NRM-10"""

        with pytest.raises(MemoryNormalizationError):
            canonical_json_bytes({"k" * 201: "v"})


class TestFingerprints:
    def test_identical_facts_fingerprint_identically(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-11"""

        assert _fingerprint(crypto) == _fingerprint(crypto)

    def test_slug_and_prose_forms_share_a_fingerprint(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-12 — the regression that wrote a second record for one fact.

        The model alternates between "improve at urban sketching" and
        "improve_at_urban_sketching" for the same fact.  Before the identity
        fold, those hashed differently and the planner stored both.
        """

        prose = _fingerprint(crypto, value="improve at urban sketching")
        slug = _fingerprint(crypto, value="improve_at_urban_sketching")
        assert prose == slug

    def test_case_variants_share_a_fingerprint(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-13"""

        assert _fingerprint(crypto, value="Improve At Urban Sketching") == _fingerprint(
            crypto, value="improve at urban sketching"
        )

    def test_punctuation_differences_share_a_fingerprint(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-14 — the identity of a value is its words, not its separators."""

        assert _fingerprint(crypto, value="improve at urban-sketching!") == _fingerprint(
            crypto, value="improve at urban sketching"
        )

    def test_the_owner_does_not_change_an_unkeyed_fingerprint(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """NRM-15 — pinning current behaviour, not endorsing it.

        Owner scoping for normal memories is done by the ``owner_id`` column and
        the owner-scoped unique indexes, not by the digest, so two owners
        recording the same fact share a fingerprint string.  That is safe as
        long as every query stays owner-filtered — which the ISO tests check
        separately.  Pinned here so that if the digest ever *should* include the
        owner, the change is deliberate.
        """

        mine = _fingerprint(crypto, owner=OWNER_ID)
        theirs = _fingerprint(crypto, owner="22222222-2222-4222-8222-222222222222")
        assert mine == theirs

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("domain_key", "learning"),
            ("slot_key", "goal:global:independent:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            ("memory_type", MemoryType.KNOWLEDGE),
            ("subject_key", "partner"),
            ("scope_type", "project"),
        ],
    )
    def test_changing_any_identity_component_changes_the_fingerprint(
        self, crypto: LocalMemoryCrypto, field: str, value: object
    ) -> None:
        """NRM-16"""

        baseline = _fingerprint(crypto)
        assert _fingerprint(crypto, **{field: value}) != baseline

    def test_the_project_scope_participates_in_the_fingerprint(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """NRM-17 — the same fact in two projects is two facts."""

        first = _fingerprint(crypto, scope_type="project", scope_project_id="alpha")
        second = _fingerprint(crypto, scope_type="project", scope_project_id="beta")
        assert first != second

    def test_a_sensitive_fact_gets_a_keyed_fingerprint(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-18 — the digest must not be guessable from the plaintext."""

        digest = _fingerprint(crypto, sensitivity=Sensitivity.SENSITIVE)
        assert digest.startswith("keyed:local-memory-v1:")
        assert digest != _fingerprint(crypto, sensitivity=Sensitivity.NORMAL)

    def test_prohibited_content_is_never_fingerprinted(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-19 — the earliest point that refuses to persist it."""

        with pytest.raises(MemoryNormalizationError, match="prohibited_content_not_persisted"):
            _fingerprint(crypto, sensitivity=Sensitivity.PROHIBITED)

    def test_the_keyed_provider_receives_the_owner(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-20 — otherwise two owners' sensitive digests would collide."""

        seen: list[str] = []

        class SpyProvider:
            def fingerprint(self, material: bytes, *, owner_id: str) -> KeyedDigest:
                seen.append(owner_id)
                return crypto.fingerprint(material, owner_id=owner_id)

        canonical_fingerprint(
            owner_id=OWNER_ID,
            subject_key="user",
            memory_type=MemoryType.GOAL,
            domain_key="global",
            slot_key=GOAL_SLOT,
            canonical_value="a secret fact",
            sensitivity=Sensitivity.SENSITIVE,
            keyed_provider=SpyProvider(),
        )
        assert seen == [OWNER_ID]

    @pytest.mark.parametrize("sensitivity", [Sensitivity.NORMAL, Sensitivity.SENSITIVE])
    def test_a_fingerprint_fits_the_database_column(
        self, crypto: LocalMemoryCrypto, sensitivity: Sensitivity
    ) -> None:
        """NRM-21 — the column is 128 chars; silent truncation would collide."""

        from app.models.memory import FINGERPRINT_LENGTH

        assert len(_fingerprint(crypto, sensitivity=sensitivity)) <= FINGERPRINT_LENGTH


class TestSlotValidation:
    """NRM-22 … NRM-29 — reached through ``normalize_record_value``."""

    @staticmethod
    def _normalize(crypto: LocalMemoryCrypto, *, memory_type: MemoryType, slot_key: str, **kw):
        return normalize_record_value(
            owner_id=OWNER_ID,
            subject_key="user",
            memory_type=memory_type,
            domain_key=kw.pop("domain_key", "global"),
            slot_key=slot_key,
            canonical_value=kw.pop("canonical_value", "a value"),
            display_text=kw.pop("display_text", "a value"),
            sensitivity=Sensitivity.NORMAL,
            value_schema_version=kw.pop("value_schema_version", 1),
            keyed_provider=crypto,
            **kw,
        )

    @pytest.mark.parametrize(
        "slot_key",
        [
            "goal:global:primary_output:extra",
            "goal:global:not_a_role",
        ],
    )
    def test_a_malformed_exclusive_goal_slot_is_rejected(
        self, crypto: LocalMemoryCrypto, slot_key: str
    ) -> None:
        """NRM-22"""

        with pytest.raises(MemoryNormalizationError):
            self._normalize(crypto, memory_type=MemoryType.GOAL, slot_key=slot_key)

    def test_a_well_formed_exclusive_goal_slot_is_accepted(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-22b"""

        result = self._normalize(
            crypto, memory_type=MemoryType.GOAL, slot_key="goal:global:primary_output"
        )
        assert result.canonical_fingerprint

    @pytest.mark.parametrize(
        "slot_key",
        [
            "goal:global:independent",
            "goal:global:independent:not-a-uuid",
            "goal:global:dependent:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ],
    )
    def test_a_malformed_additive_goal_slot_is_rejected(
        self, crypto: LocalMemoryCrypto, slot_key: str
    ) -> None:
        """NRM-23"""

        with pytest.raises((MemoryNormalizationError, ValueError)):
            self._normalize(crypto, memory_type=MemoryType.GOAL, slot_key=slot_key)

    def test_a_preference_slot_must_be_three_parts(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-24"""

        with pytest.raises(MemoryNormalizationError, match="invalid_preference_slot"):
            self._normalize(
                crypto,
                memory_type=MemoryType.PREFERENCE,
                slot_key="preference:global:verbosity:extra",
            )

    def test_an_identity_slot_must_be_global(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-25"""

        with pytest.raises(MemoryNormalizationError):
            self._normalize(
                crypto,
                memory_type=MemoryType.IDENTITY,
                domain_key="career",
                slot_key="identity:career:name",
            )

    def test_an_additive_item_slot_must_carry_a_uuid(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-26"""

        with pytest.raises((MemoryNormalizationError, ValueError)):
            self._normalize(
                crypto,
                memory_type=MemoryType.KNOWLEDGE,
                slot_key="knowledge:global:item:not-a-uuid",
            )

    def test_an_exclusive_non_goal_slot_must_be_current_status(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """NRM-27"""

        with pytest.raises(MemoryNormalizationError, match="invalid_exclusive_slot"):
            self._normalize(
                crypto,
                memory_type=MemoryType.KNOWLEDGE,
                slot_key="knowledge:global:something",
            )

    def test_a_slot_whose_type_prefix_is_wrong_is_rejected(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-28"""

        with pytest.raises(MemoryNormalizationError, match="invalid_slot_identity"):
            self._normalize(
                crypto,
                memory_type=MemoryType.GOAL,
                slot_key="preference:global:verbosity",
            )

    def test_a_slot_whose_domain_is_wrong_is_rejected(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-29"""

        with pytest.raises(MemoryNormalizationError, match="invalid_slot_identity"):
            self._normalize(
                crypto,
                memory_type=MemoryType.PREFERENCE,
                domain_key="global",
                slot_key="preference:learning:verbosity",
            )


class TestPositiveValueGuard:
    """A memory records what is true now, never what stopped being true."""

    @pytest.mark.parametrize(
        "display",
        [
            "no longer wants to run",
            "do not want coffee",
            "don't want coffee",
            "did not want coffee",
            "used to play guitar",
            "stopped wanting coffee",
            "stop wanting coffee",
        ],
    )
    def test_a_negated_fact_is_rejected(self, crypto: LocalMemoryCrypto, display: str) -> None:
        """NRM-30"""

        with pytest.raises(MemoryNormalizationError, match="positive_current_fact_required"):
            normalize_record_value(
                owner_id=OWNER_ID,
                subject_key="user",
                memory_type=MemoryType.GOAL,
                domain_key="global",
                slot_key=GOAL_SLOT,
                canonical_value="x",
                display_text=display,
                sensitivity=Sensitivity.NORMAL,
                value_schema_version=1,
                keyed_provider=crypto,
            )

    def test_a_third_person_negation_is_rejected(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-30b — fixed. The negated-want forms are now generated, not listed.

        ``_validate_positive_text`` matches "do not want" and "did not want" but
        has no "does not want" branch.  Display text is normally the user's own
        first-person words, which is why this has not bitten yet — but a
        model-written display hint reading "does not want coffee" would be
        stored as a positive fact.  Marked strict so that fixing the pattern
        turns this red and the test gets promoted into NRM-30.
        """

        with pytest.raises(MemoryNormalizationError, match="positive_current_fact_required"):
            normalize_record_value(
                owner_id=OWNER_ID,
                subject_key="user",
                memory_type=MemoryType.GOAL,
                domain_key="global",
                slot_key=GOAL_SLOT,
                canonical_value="x",
                display_text="does not want coffee",
                sensitivity=Sensitivity.NORMAL,
                value_schema_version=1,
                keyed_provider=crypto,
            )

    def test_not_only_is_explicitly_exempt(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-31 — "not only X but Y" is an assertion, not a retraction."""

        result = normalize_record_value(
            owner_id=OWNER_ID,
            subject_key="user",
            memory_type=MemoryType.GOAL,
            domain_key="global",
            slot_key=GOAL_SLOT,
            canonical_value="x",
            display_text="not only sketching but also watercolour",
            sensitivity=Sensitivity.NORMAL,
            value_schema_version=1,
            keyed_provider=crypto,
        )
        assert "not only" in result.display_text

    @pytest.mark.parametrize("display", ["NO LONGER wants to run", "No Longer wants to run"])
    def test_the_guard_is_case_insensitive(self, crypto: LocalMemoryCrypto, display: str) -> None:
        """NRM-32"""

        with pytest.raises(MemoryNormalizationError):
            normalize_record_value(
                owner_id=OWNER_ID,
                subject_key="user",
                memory_type=MemoryType.GOAL,
                domain_key="global",
                slot_key=GOAL_SLOT,
                canonical_value="x",
                display_text=display,
                sensitivity=Sensitivity.NORMAL,
                value_schema_version=1,
                keyed_provider=crypto,
            )

    def test_display_text_over_four_thousand_characters_is_rejected(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """NRM-33"""

        with pytest.raises(MemoryNormalizationError):
            normalize_record_value(
                owner_id=OWNER_ID,
                subject_key="user",
                memory_type=MemoryType.GOAL,
                domain_key="global",
                slot_key=GOAL_SLOT,
                canonical_value="x",
                display_text="x" * 4_001,
                sensitivity=Sensitivity.NORMAL,
                value_schema_version=1,
                keyed_provider=crypto,
            )


class TestCandidateNormalization:
    def test_a_global_scope_with_a_project_id_is_rejected(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-34"""

        with pytest.raises(MemoryNormalizationError, match="invalid_memory_scope"):
            normalize_candidate(
                factories.proposal(scope_type="global", scope_project_id="alpha"),
                owner_id=OWNER_ID,
                keyed_provider=crypto,
            )

    def test_a_project_scope_without_a_project_id_is_rejected(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """NRM-35"""

        with pytest.raises(MemoryNormalizationError, match="invalid_memory_scope"):
            normalize_candidate(
                factories.proposal(scope_type="project"),
                owner_id=OWNER_ID,
                keyed_provider=crypto,
            )

    def test_an_unsupported_value_schema_version_is_rejected(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """NRM-36"""

        with pytest.raises(MemoryNormalizationError, match="unsupported_value_schema_version"):
            normalize_candidate(
                factories.proposal(value_schema_version=99),
                owner_id=OWNER_ID,
                keyed_provider=crypto,
            )

    def test_evidence_text_is_normalised_and_capped(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-41"""

        normalized = normalize_candidate(
            factories.proposal(
                evidence=(
                    EvidenceSpan(role=EvidenceRole.ASSERTION, text="  spaced   out  text  "),
                ),
            ),
            owner_id=OWNER_ID,
            keyed_provider=crypto,
        )
        assert normalized.evidence[0].text == "spaced out text"

        with pytest.raises(MemoryNormalizationError):
            normalize_candidate(
                factories.proposal(
                    evidence=(
                        EvidenceSpan(
                            role=EvidenceRole.ASSERTION,
                            text="x" * (MAX_EVIDENCE_TEXT_CHARS + 1),
                        ),
                    ),
                ),
                owner_id=OWNER_ID,
                keyed_provider=crypto,
            )

    def test_an_unknown_topic_domain_must_be_grounded_in_the_candidate(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """NRM-51 — the model cannot file a fact under a topic it invented."""

        with pytest.raises(MemoryNormalizationError, match="unknown_domain_must_be_grounded"):
            normalize_candidate(
                factories.proposal(
                    domain_key="topic.competitive_cheesemaking",
                    slot_key=(
                        "goal:topic.competitive_cheesemaking:independent:"
                        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                    ),
                    canonical_value="improve at urban sketching",
                    display_text="improve at urban sketching",
                ),
                owner_id=OWNER_ID,
                keyed_provider=crypto,
            )

    def test_a_grounded_topic_domain_is_accepted(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-51b"""

        normalized = normalize_candidate(
            factories.proposal(
                domain_key="topic.sourdough_baking",
                slot_key=(
                    "goal:topic.sourdough_baking:independent:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                ),
                canonical_value="get better at sourdough baking",
                display_text="get better at sourdough baking",
            ),
            owner_id=OWNER_ID,
            keyed_provider=crypto,
        )
        assert normalized.domain_key == "topic.sourdough_baking"

    def test_a_predecessor_identity_is_inherited(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-51c — a correction stays where the user originally filed it."""

        predecessor = MemoryIdentity(
            memory_type=MemoryType.GOAL,
            domain_key="learning",
            slot_key="goal:learning:independent:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            cardinality=Cardinality.ADDITIVE,
        )
        normalized = normalize_candidate(
            factories.proposal(intent=CandidateIntent.REPLACE),
            owner_id=OWNER_ID,
            keyed_provider=crypto,
            predecessor=predecessor,
        )
        assert normalized.domain_key == "learning"
        assert normalized.slot_key == predecessor.slot_key

    def test_an_explicit_domain_change_is_honoured(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-51d"""

        predecessor = MemoryIdentity(
            memory_type=MemoryType.GOAL,
            domain_key="learning",
            slot_key="goal:learning:independent:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            cardinality=Cardinality.ADDITIVE,
        )
        normalized = normalize_candidate(
            factories.proposal(
                domain_key="career",
                target_hints=CandidateTargetHints(explicit_domain_change=True),
            ),
            owner_id=OWNER_ID,
            keyed_provider=crypto,
            predecessor=predecessor,
        )
        assert normalized.domain_key == "career"


class TestMetadata:
    @pytest.mark.parametrize("key", sorted(ALLOWED_METADATA_KEYS))
    def test_each_allowed_key_survives(self, key: str) -> None:
        """NRM-37a"""

        assert normalize_metadata({key: "value"}) == {key: "value"}

    @pytest.mark.parametrize("key", ["owner_id", "secret", "canonical_payload", ""])
    def test_an_unknown_key_is_rejected(self, key: str) -> None:
        """NRM-37b — metadata is not a back door into the record."""

        with pytest.raises(MemoryNormalizationError, match="metadata_key_not_allowed"):
            normalize_metadata({key: "value"})

    def test_an_over_large_payload_is_rejected(self) -> None:
        """NRM-38"""

        with pytest.raises(MemoryNormalizationError, match="metadata_too_large"):
            normalize_metadata({"tags": ["x" * MAX_METADATA_JSON_BYTES]})

    def test_none_normalises_to_an_empty_mapping(self) -> None:
        """NRM-39"""

        assert normalize_metadata(None) == {}


class TestCommandVersions:
    def test_a_matching_command_validates(self) -> None:
        """NRM-40a"""

        validate_command_versions(factories.create_command())

    @pytest.mark.parametrize(
        ("field", "code"),
        [
            ("contract_version", "unsupported_contract_version"),
            ("policy_version", "unsupported_policy_version"),
            ("taxonomy_version", "unsupported_taxonomy_version"),
        ],
    )
    def test_each_mismatched_version_raises_its_own_code(self, field: str, code: str) -> None:
        """NRM-40b — a stale client must be told which contract it is behind on."""

        command = factories.create_command()
        stale = command.model_copy(update={field: "neo.memory.something.v0"})
        with pytest.raises(MemoryNormalizationError) as excinfo:
            validate_command_versions(stale)
        assert excinfo.value.code == code


class TestSourceNormalization:
    def test_a_naive_observed_at_is_coerced_to_utc(self) -> None:
        """NRM-42a"""

        normalized = normalize_source(
            MemorySource(
                kind=SourceKind.CHAT_MESSAGE,
                observed_at=datetime(2026, 6, 15, 12, 0, 0),
            )
        )
        assert normalized.observed_at == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    def test_an_aware_observed_at_is_left_alone(self) -> None:
        """NRM-42b"""

        moment = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        assert (
            normalize_source(
                MemorySource(kind=SourceKind.CHAT_MESSAGE, observed_at=moment)
            ).observed_at
            == moment
        )

    def test_evidence_is_normalised(self) -> None:
        """NRM-42c"""

        normalized = normalize_source(
            MemorySource(
                kind=SourceKind.CHAT_MESSAGE,
                evidence=(EvidenceSpan(role=EvidenceRole.ASSERTION, text="  padded   text  "),),
            )
        )
        assert normalized.evidence[0].text == "padded text"


class TestCompatibleRefinement:
    def test_equal_values_are_compatible(self) -> None:
        """NRM-43"""

        assert compatible_refinement("a value", "a value") is True

    def test_a_strictly_longer_string_containing_the_old_one_is_a_refinement(self) -> None:
        """NRM-44 — "run a 5K" refined to "run a 5K under 25 minutes"."""

        assert compatible_refinement("run a 5K", "run a 5K under 25 minutes") is True
        assert compatible_refinement("RUN A 5K", "run a 5k under 25 minutes") is True

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("run a 5K under 25 minutes", "run a 5K"),
            ("run a 5K", "swim a mile"),
            ("run a 5K", "run a 5K"[:4]),
        ],
    )
    def test_a_shorter_or_unrelated_string_is_not_a_refinement(self, old: str, new: str) -> None:
        """NRM-45 — narrowing or replacing is a different operation."""

        assert compatible_refinement(old, new) is False

    def test_a_superset_dictionary_is_a_refinement(self) -> None:
        """NRM-46"""

        assert compatible_refinement({"a": 1}, {"a": 1, "b": 2}) is True

    def test_a_changed_shared_key_is_not_a_refinement(self) -> None:
        """NRM-47 — that is a contradiction, and needs the replace path."""

        assert compatible_refinement({"a": 1}, {"a": 2, "b": 3}) is False

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("a", {"a": 1}),
            ({"a": 1}, "a"),
            (["a"], ["a", "b"]),
            (1, 2),
        ],
    )
    def test_mismatched_or_unsupported_types_are_not_refinements(
        self, old: object, new: object
    ) -> None:
        """NRM-48 — only strings and dicts have a defined refinement rule."""

        assert compatible_refinement(old, new) is False


class TestOperationRequestHash:
    def test_equal_commands_hash_identically(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-49a"""

        first = factories.create_command(idempotency_key="k")
        second = factories.create_command(idempotency_key="k")
        # The proposal id is generated per call, so align it before comparing.
        aligned = second.model_copy(update={"candidate": first.candidate})
        assert operation_request_hash(
            first, keyed_provider=crypto, sensitivity=Sensitivity.NORMAL
        ) == operation_request_hash(aligned, keyed_provider=crypto, sensitivity=Sensitivity.NORMAL)

    def test_any_field_change_changes_the_hash(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-49b — this is what makes idempotent replay safe."""

        command = factories.create_command(idempotency_key="k")
        changed = command.model_copy(update={"idempotency_key": "other"})
        assert operation_request_hash(
            command, keyed_provider=crypto, sensitivity=Sensitivity.NORMAL
        ) != operation_request_hash(changed, keyed_provider=crypto, sensitivity=Sensitivity.NORMAL)

    def test_a_sensitive_command_uses_the_keyed_path(self, crypto: LocalMemoryCrypto) -> None:
        """NRM-50 — the hash must not reveal the command it stands for."""

        command = factories.create_command()
        digest = operation_request_hash(
            command, keyed_provider=crypto, sensitivity=Sensitivity.SENSITIVE
        )
        assert digest.startswith("keyed:local-memory-v1:")
        assert digest != operation_request_hash(
            command, keyed_provider=crypto, sensitivity=Sensitivity.NORMAL
        )

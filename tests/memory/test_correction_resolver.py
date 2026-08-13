"""Tier 2 — candidate building in the correction resolver (plan section COR).

`test_forget_and_duplicates.py` already covers this module from the outside: the
slot-sharing and retraction behaviours that a user would notice.  This file
covers the private helpers underneath, which is where the properties those
behaviours *depend on* actually live.

The recurring theme is identity: which two facts count as the same fact.  A slot
derived from the wrong material means one goal stored twice and a delete that can
never resolve to a single target — the bug decision-16 and the docstring on
`_value_entity_id` both describe.  These helpers are the arithmetic behind that,
so they are tested for the algebraic properties (stable, owner-scoped, folded)
rather than for specific output strings.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.services.memory.contracts import (
    CanonicalMemorySnapshot,
    MemoryLifecycleState,
    Sensitivity,
)
from app.services.memory.correction_resolver import (
    _SLOT_TOKEN_ALPHABET,
    CorrectionResolutionKind,
    DeterministicCorrectionResolver,
    _candidate_uuid,
    _domain_for,
    _entity_token,
    _fold,
    _sensitivity,
    _value_entity_id,
    build_candidate,
)
from app.services.memory.extraction_contracts import ExtractionMode, ExtractionRequest
from app.services.memory.grounding import ground_assertion
from app.services.memory.model_schema import (
    DurabilityHint,
    ModelAssertionProposal,
    ModelRetractionProposal,
    ModelSourceSpan,
    SubjectHint,
)
from app.services.memory.taxonomy import Cardinality, MemoryType

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER = "22222222-2222-4222-8222-222222222222"


def _request(
    message: str,
    *,
    message_id: str = "m1",
    owner: str = OWNER,
    explicit: bool = True,
) -> ExtractionRequest:
    return ExtractionRequest(
        request_id="request-1",
        owner_id=owner,
        conversation_id="c1",
        session_id="s1",
        message_id=message_id,
        user_message=message,
        explicit_memory_intent=explicit,
        mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
        source_content_hash=ExtractionRequest.content_hash(message),
    )


def _proposal(
    message: str,
    quoted: str,
    *,
    typed_value: object | None = None,
    memory_type: MemoryType = MemoryType.GOAL,
    domain_hint: str | None = None,
    sensitivity_hint: Sensitivity = Sensitivity.NORMAL,
    durability: DurabilityHint = DurabilityHint.DURABLE,
    proposal_id: str = "p1",
    message_id: str = "m1",
) -> ModelAssertionProposal:
    """One assertion whose span genuinely selects `quoted` inside `message`."""

    start = message.index(quoted)
    return ModelAssertionProposal(
        proposal_id=proposal_id,
        source_spans=(
            ModelSourceSpan(
                message_id=message_id,
                start=start,
                end=start + len(quoted),
                quoted_text=quoted,
            ),
        ),
        subject_hint=SubjectHint.USER,
        memory_type_hint=memory_type,
        typed_value=quoted if typed_value is None else typed_value,
        display_hint=quoted,
        durability=durability,
        confidence=0.9,
        sensitivity_hint=sensitivity_hint,
        domain_hint=domain_hint,
    )


class TestValueFolding:
    """COR-07 — the entity id is a pure function of the *folded* value.

    This is the fix described in `_value_entity_id`'s docstring.  The entity
    component used to be the candidate id, which mixes in the message id, so the
    same fact restated on a later turn produced a different slot, a different
    fingerprint, and a brand-new record.  Every re-extraction appended another
    copy — including the ones triggered by merely asking what is remembered —
    and a delete could then never resolve to one target.
    """

    def test_the_same_value_yields_the_same_entity_regardless_of_candidate(self) -> None:
        proposal = _proposal(
            "I want to improve at urban sketching always",
            "improve at urban sketching",
        )

        first = _value_entity_id(proposal, domain="art", candidate_id=uuid4())
        second = _value_entity_id(proposal, domain="art", candidate_id=uuid4())

        assert first == second

    def test_slug_and_prose_forms_share_an_entity(self) -> None:
        """The regression `_fold`'s comment describes, at the entity level.

        A model asked for a canonical value answers "improve at urban sketching"
        one turn and "improve_at_urban_sketching" the next.  Underscores separate
        words rather than joining them, so both fold to one string.
        """

        prose = _proposal(
            "I want to improve at urban sketching always",
            "improve at urban sketching",
        )
        slug = _proposal(
            "I want to improve_at_urban_sketching always",
            "improve_at_urban_sketching",
        )

        assert _value_entity_id(prose, domain="art", candidate_id=uuid4()) == _value_entity_id(
            slug, domain="art", candidate_id=uuid4()
        )

    def test_a_different_domain_yields_a_different_entity(self) -> None:
        """The same words under two domains are two facts, not one."""

        proposal = _proposal("I want to get better always", "get better")

        assert _value_entity_id(
            proposal, domain="art", candidate_id=uuid4()
        ) != _value_entity_id(proposal, domain="fitness", candidate_id=uuid4())

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("improve at urban sketching", "improve_at_urban_sketching"),
            ("improve at urban sketching", "Improve At Urban Sketching"),
            ("make videos", "making videos"),
            ("always answer me with detailed explanations", "detailed explanations"),
        ],
    )
    def test_folding_equivalences(self, left: str, right: str) -> None:
        """COR-07b — every fold rule the module documents, one case each.

        The last pair is the one worth reading twice: a global style is stored as
        an imperative ("always answer me with detailed explanations") but
        corrected as a preference ("I don't prefer detailed explanations").  The
        framing is not part of the value, so it is stripped — otherwise an
        explicitly grounded replacement could never find its predecessor.
        """

        assert _fold(left) == _fold(right)


class TestEntityToken:
    """COR-08 — the token is letters only, and that is a correctness property.

    A raw hex identifier occasionally contains a Luhn-valid run of 13+ digits,
    which the repository's content guard reads as a card number and refuses to
    persist.  Since the token is embedded in a slot key, that would make a
    perfectly ordinary memory unstorable, at random, depending on its UUID.
    """

    def test_the_token_is_stable_for_an_identifier(self) -> None:
        value = UUID("12345678-1234-5678-1234-567812345678")

        assert _entity_token(value) == _entity_token(value)

    @pytest.mark.parametrize("_run", range(25))
    def test_the_token_never_contains_a_digit(self, _run: int) -> None:
        """Run over many random UUIDs — the failure this prevents is data-dependent."""

        token = _entity_token(uuid4())

        assert token.isalpha()
        assert set(token) <= set(_SLOT_TOKEN_ALPHABET)

    def test_distinct_identifiers_give_distinct_tokens(self) -> None:
        """The encoding is a nibble-to-letter map, so it must not lose information."""

        tokens = {_entity_token(uuid4()) for _ in range(50)}

        assert len(tokens) == 50


class TestCandidateUuid:
    """COR-09 — deterministic per (owner, message, proposal), and nothing else.

    Determinism is what makes re-processing the same turn idempotent: the second
    run derives the same candidate id and recognises the candidate it already
    stored, rather than creating a second one.
    """

    def test_it_is_stable_for_the_same_inputs(self) -> None:
        request = _request("I want to learn Rust")

        assert _candidate_uuid(request, "p1") == _candidate_uuid(request, "p1")

    @pytest.mark.parametrize(
        ("left", "right", "proposal_id"),
        [
            (_request("x", message_id="m1"), _request("x", message_id="m2"), "p1"),
            (_request("x", owner=OWNER), _request("x", owner=OTHER_OWNER), "p1"),
        ],
        ids=["message_id", "owner_id"],
    )
    def test_it_varies_with_each_component(
        self, left: ExtractionRequest, right: ExtractionRequest, proposal_id: str
    ) -> None:
        assert _candidate_uuid(left, proposal_id) != _candidate_uuid(right, proposal_id)

    def test_it_varies_with_the_proposal_id(self) -> None:
        """Two proposals from one message are two candidates."""

        request = _request("I want to learn Rust and Go")

        assert _candidate_uuid(request, "p1") != _candidate_uuid(request, "p2")

    def test_it_does_not_depend_on_the_message_text(self) -> None:
        """Pinning what the id is keyed on, because the text is *not* in it.

        The message id already identifies the message.  Including the text would
        mean an edited message produced a different candidate id for the same
        logical proposal — so this is correct, but it is worth asserting rather
        than assuming, since it is invisible from the call site.
        """

        assert _candidate_uuid(_request("first", message_id="m1"), "p1") == _candidate_uuid(
            _request("totally different", message_id="m1"), "p1"
        )


class TestSensitivityEscalation:
    """COR-10 — the classifier escalates the model's hint and never softens it."""

    def test_a_normal_hint_is_escalated_by_the_classifier(self) -> None:
        """The model calling a diagnosis "normal" does not make it normal."""

        request = _request("I have asthma")
        proposal = _proposal("I have asthma", "asthma", sensitivity_hint=Sensitivity.NORMAL)

        sensitivity, error = _sensitivity(request, proposal, "I have asthma")

        assert error is None
        assert sensitivity is Sensitivity.SENSITIVE

    def test_prohibited_content_is_refused_outright(self) -> None:
        request = _request("my password is hunter2")
        proposal = _proposal("my password is hunter2", "hunter2")

        sensitivity, error = _sensitivity(request, proposal, "my password is hunter2")

        assert sensitivity is None
        assert error == "prohibited_content_not_persisted"

    def test_sensitive_content_requires_an_explicit_request(self) -> None:
        """COR-14, at the helper — the same rule the contract enforces later."""

        request = _request("I have asthma", explicit=False)
        proposal = _proposal("I have asthma", "asthma")

        sensitivity, error = _sensitivity(request, proposal, "I have asthma")

        assert sensitivity is None
        assert error == "sensitive_requires_explicit_request"

    def test_an_ordinary_fact_stays_normal(self) -> None:
        """The regression behind POL-16 — "I have two cats" is not a diagnosis."""

        request = _request("I have two cats")
        proposal = _proposal("I have two cats", "two cats")

        sensitivity, error = _sensitivity(request, proposal, "I have two cats")

        assert error is None
        assert sensitivity is Sensitivity.NORMAL


class TestDomainResolution:
    def test_a_resolvable_hint_is_used(self) -> None:
        """COR-11a"""

        proposal = _proposal("I edit videos daily", "videos", domain_hint="video_creation")

        assert _domain_for(proposal, "I edit videos daily") == "video_creation"

    def test_an_unresolvable_hint_falls_back_to_the_source_text(self) -> None:
        """COR-11b — the model's label is discarded, the fact is kept.

        The model names a domain that is neither a known alias nor a phrase in
        the message ("demographics" for "I am 21 years old").  A domain is an
        organising facet, so the label is dropped rather than the fact.
        """

        proposal = _proposal("I edit videos daily", "videos", domain_hint="demographics")

        assert _domain_for(proposal, "I edit videos daily") == "video_creation"

    def test_a_global_preference_about_video_is_narrowed(self) -> None:
        """COR-11c — the one place a hint is *overridden* rather than trusted.

        "For video-editing advice, keep it brief" hinted as global would apply
        that style to every answer.  Topic-specific wording with no global
        wording narrows it.
        """

        proposal = _proposal(
            "For video editing keep it brief",
            "keep it brief",
            memory_type=MemoryType.PREFERENCE,
            domain_hint="global",
        )

        assert _domain_for(proposal, "For video editing keep it brief") == "video_creation"

    def test_genuinely_global_wording_stays_global(self) -> None:
        """COR-11d — the other side of the narrowing rule."""

        proposal = _proposal(
            "Always answer briefly",
            "briefly",
            memory_type=MemoryType.PREFERENCE,
            domain_hint="global",
        )

        assert _domain_for(proposal, "Always answer briefly") == "global"

    def test_an_ungroundable_domain_defaults_to_global_rather_than_failing(self) -> None:
        """COR-12 — corrected: this does **not** fail closed, deliberately.

        The plan predicted a refusal here.  `resolve_domain` does fail closed —
        TAX-04 pins that — but `_domain_for` catches it and defaults to `global`,
        on the reasoning in its comment: a domain is only an organising facet, so
        losing a durable fact over an unrecognised label is the worse outcome.

        The safety property people expect from "fails closed" is still enforced,
        just elsewhere: the *value* must be grounded in the user's own words, and
        `ground_assertion` checks that independently of the domain.
        """

        proposal = _proposal("zzz qqq", "zzz")

        assert _domain_for(proposal, "zzz qqq") == "global"


class TestBuildCandidateFailures:
    """COR-13 — every failure path names itself.

    `build_candidate` returns a result object rather than raising, so a bare
    `None` reason would leave the coordinator unable to say why a candidate was
    dropped — and dropping a memory silently is the failure mode this whole
    layer exists to avoid.
    """

    def _built(self, request: ExtractionRequest, proposal: ModelAssertionProposal):
        return build_candidate(request, proposal, ground_assertion(request, proposal), ())

    def test_a_successful_build_still_carries_a_reason(self) -> None:
        request = _request("I want to learn Rust")
        result = self._built(request, _proposal("I want to learn Rust", "learn Rust"))

        assert result.candidate is not None
        assert result.reason == "candidate_normalized"

    @pytest.mark.parametrize(
        ("durability", "expected"),
        [
            (DurabilityHint.TEMPORARY, "temporary_candidate_ignored"),
            (DurabilityHint.UNCERTAIN, "uncertain_durability_requires_review"),
        ],
    )
    def test_durability_rejections_name_themselves(
        self, durability: DurabilityHint, expected: str
    ) -> None:
        request = _request("I want to learn Rust")
        proposal = _proposal("I want to learn Rust", "learn Rust", durability=durability)

        result = self._built(request, proposal)

        assert result.candidate is None
        assert result.reason == expected

    def test_an_ungrounded_proposal_is_rejected_with_groundings_reason(self) -> None:
        """The reason is passed through rather than replaced, so it stays specific."""

        request = _request("I want to learn Rust")
        proposal = _proposal("I want to learn Rust", "learn Rust")
        forged = proposal.model_copy(
            update={
                "source_spans": (
                    ModelSourceSpan(
                        message_id="m1",
                        start=0,
                        end=20,
                        quoted_text="something never said",
                    ),
                ),
            }
        )

        result = build_candidate(request, forged, ground_assertion(request, forged), ())

        assert result.candidate is None
        assert result.reason == "source_span_quote_mismatch"

    def test_prohibited_content_is_refused_by_name(self) -> None:
        request = _request("my password is hunter2")
        proposal = _proposal("my password is hunter2", "hunter2")

        result = self._built(request, proposal)

        assert result.candidate is None
        assert result.reason == "prohibited_content_not_persisted"

    def test_a_sensitive_candidate_without_an_explicit_request_is_not_validated(self) -> None:
        """COR-14 — the whole point: it must not come back as a usable candidate."""

        request = _request("I have asthma", explicit=False)
        proposal = _proposal("I have asthma", "asthma")

        result = self._built(request, proposal)

        assert result.candidate is None
        assert result.reason == "sensitive_requires_explicit_request"

    def test_the_same_sensitive_fact_is_accepted_when_explicitly_requested(self) -> None:
        """The guard distinguishes on intent, not on the content alone.

        Without this, the test above would pass for a build that rejected every
        sensitive fact unconditionally, which is a different (and wrong) rule.
        """

        request = _request("I have asthma", explicit=True)
        proposal = _proposal("I have asthma", "asthma")

        result = self._built(request, proposal)

        assert result.candidate is not None
        assert result.candidate.proposal.sensitivity is Sensitivity.SENSITIVE
        assert result.candidate.proposal.explicit_user_request is True


# --------------------------------------------------------------------------
# Resolution — COR-18..24
# --------------------------------------------------------------------------


def _snapshot(
    value: str,
    *,
    memory_type: MemoryType = MemoryType.GOAL,
    domain: str = "video_creation",
    slot: str = "goal:video_creation:current_primary_goal",
    cardinality: Cardinality = Cardinality.EXCLUSIVE,
    status: MemoryLifecycleState = MemoryLifecycleState.ACTIVE,
    owner: str = OWNER,
) -> CanonicalMemorySnapshot:
    """One stored record, as the resolver receives it.

    Kept local rather than promoted to `factories.py`: the two other suites that
    build snapshots want different default shapes (an exclusive preference, and
    an additive knowledge record feeding the override builder).  Three uses of
    one contract type is not three uses of one helper — consolidating now would
    mean a default shape none of the three wants and four overrides at every
    call site.
    """

    return CanonicalMemorySnapshot(
        memory_id=uuid4(),
        owner_id=owner,
        subject_key="user",
        memory_type=memory_type,
        domain_key=domain,
        slot_key=slot,
        cardinality=cardinality,
        canonical_value=value,
        display_text=value,
        sensitivity=Sensitivity.NORMAL,
        status=status,
        revision=1,
    )


def _candidate(
    message: str,
    quoted: str,
    *,
    memory_type: MemoryType = MemoryType.GOAL,
    slot_hint: str | None = None,
    old_value_hints: tuple[str, ...] = (),
):
    """A normalized candidate, optionally carrying retraction evidence."""

    request = _request(message)
    start = message.index(quoted)
    spans = (
        ModelSourceSpan(message_id="m1", start=start, end=start + len(quoted), quoted_text=quoted),
    )
    proposal = ModelAssertionProposal(
        proposal_id="p1",
        source_spans=spans,
        subject_hint=SubjectHint.USER,
        memory_type_hint=memory_type,
        typed_value=quoted,
        display_hint=quoted,
        durability=DurabilityHint.DURABLE,
        confidence=0.9,
        sensitivity_hint=Sensitivity.NORMAL,
        domain_hint="video_creation",
        slot_hint=slot_hint,
        correction_group="g1" if old_value_hints else None,
    )
    retractions = tuple(
        ModelRetractionProposal(
            proposal_id=f"r{index}",
            source_spans=spans,
            subject_hint=SubjectHint.USER,
            confidence=0.9,
            old_value_hint=hint,
            correction_group="g1",
        )
        for index, hint in enumerate(old_value_hints)
    )
    result = build_candidate(request, proposal, ground_assertion(request, proposal), retractions)
    assert result.candidate is not None, result.reason
    return result.candidate


def _retraction(
    hint: str,
    *,
    memory_type: MemoryType | None = None,
    domain: str | None = None,
    slot: str | None = None,
) -> ModelRetractionProposal:
    return ModelRetractionProposal(
        proposal_id="r1",
        source_spans=(
            ModelSourceSpan(message_id="m1", start=0, end=6, quoted_text="forget"),
        ),
        subject_hint=SubjectHint.USER,
        confidence=0.9,
        old_value_hint=hint,
        memory_type_hint=memory_type,
        domain_hint=domain,
        slot_hint=slot,
    )


RESOLVER = DeterministicCorrectionResolver()


class TestResolution:
    def test_an_empty_store_always_creates(self) -> None:
        """COR-22"""

        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            slot_hint="current_primary_goal",
        )

        resolution = RESOLVER.resolve(candidate, ())

        assert resolution.kind is CorrectionResolutionKind.CREATE
        assert resolution.reason == "independent_assertion"
        assert resolution.targets == ()

    def test_an_occupied_exclusive_slot_without_evidence_needs_review(self) -> None:
        """COR-20 — the conflict is surfaced, not resolved by guessing.

        Something already holds this slot and the user did not say what they were
        correcting.  Replacing on that basis would discard a fact on an
        assumption, so the occupant is returned as context for a human decision.
        """

        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            slot_hint="current_primary_goal",
        )
        occupant = _snapshot("write a book")

        resolution = RESOLVER.resolve(candidate, (occupant,))

        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW
        assert resolution.reason == "unlinked_exclusive_slot_conflict"
        assert resolution.targets == (occupant,)

    def test_correction_evidence_turns_the_same_conflict_into_a_replace(self) -> None:
        """COR-19 — identical state, different outcome, because the user said so.

        The only difference from the case above is that the turn named what it
        was replacing.  That is what licenses the destructive operation.
        """

        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            slot_hint="current_primary_goal",
            old_value_hints=("write a book",),
        )
        occupant = _snapshot("write a book")

        resolution = RESOLVER.resolve(candidate, (occupant,))

        assert resolution.kind is CorrectionResolutionKind.REPLACE
        assert resolution.reason == "grounded_predecessor_match"
        assert resolution.targets == (occupant,)

    def test_evidence_naming_a_value_that_is_not_stored_needs_review(self) -> None:
        """COR-19b — a hint that resolves to nothing must not fall through to CREATE.

        Creating here would leave the fact the user meant to correct still
        active, alongside its replacement.
        """

        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            slot_hint="current_primary_goal",
            old_value_hints=("something never stored",),
        )

        resolution = RESOLVER.resolve(candidate, ())

        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW
        assert resolution.reason == "grounded_retraction_target_not_found"
        assert resolution.targets == ()

    def test_an_additive_slot_creates_alongside_rather_than_replacing(self) -> None:
        """COR-21 — additive means "and also", so occupancy is not a conflict."""

        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            memory_type=MemoryType.KNOWLEDGE,
        )
        assert candidate.proposal.cardinality is Cardinality.ADDITIVE
        existing = _snapshot(
            "something else",
            memory_type=MemoryType.KNOWLEDGE,
            slot=candidate.proposal.slot_key,
            domain=candidate.proposal.domain_key,
            cardinality=Cardinality.ADDITIVE,
        )

        resolution = RESOLVER.resolve(candidate, (existing,))

        assert resolution.kind is CorrectionResolutionKind.CREATE
        assert resolution.targets == ()

    def test_an_exact_duplicate_reconfirms(self) -> None:
        """The path COR-19/20 must not swallow: same slot, same value."""

        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            slot_hint="current_primary_goal",
        )
        existing = _snapshot("create YouTube videos")

        resolution = RESOLVER.resolve(candidate, (existing,))

        assert resolution.kind is CorrectionResolutionKind.RECONFIRM
        assert resolution.reason == "exact_active_duplicate"


class TestResolverTrustBoundary:
    """COR-23 / COR-24 — corrected: the resolver filters neither status nor owner.

    The plan expected `resolve` to ignore non-active snapshots and never target a
    foreign one.  It does neither, because it never reads `status` or `owner_id`:
    it is a pure function over the records it is handed, and both guarantees are
    held by the caller.  `MutationService.list_active_records` filters to ACTIVE
    and is built against an owner-scoped repository — MUT-34 pins that.

    Testing the resolver for those properties would have meant asserting
    something it does not do, and passing only because the fixtures happened to
    be clean.  So these assert the trust boundary as it really is: hand it an
    archived or foreign record and it *will* match.  That is not a defect, but it
    is a constraint on every future caller, and it was not written down anywhere.
    """

    def test_an_archived_snapshot_is_matched_if_it_is_passed_in(self) -> None:
        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            slot_hint="current_primary_goal",
        )
        archived = _snapshot("write a book", status=MemoryLifecycleState.ARCHIVED)

        resolution = RESOLVER.resolve(candidate, (archived,))

        assert resolution.targets == (archived,)
        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW

    def test_a_foreign_snapshot_is_matched_if_it_is_passed_in(self) -> None:
        candidate = _candidate(
            "I want to create YouTube videos",
            "create YouTube videos",
            slot_hint="current_primary_goal",
        )
        foreign = _snapshot("write a book", owner=OTHER_OWNER)

        resolution = RESOLVER.resolve(candidate, (foreign,))

        assert resolution.targets == (foreign,)

    def test_a_foreign_snapshot_is_retracted_if_it_is_passed_in(self) -> None:
        """The same boundary on the retraction path, where it would delete."""

        foreign = _snapshot(
            "fineliner pen",
            memory_type=MemoryType.KNOWLEDGE,
            domain="art",
            slot="knowledge:art:item:x",
            cardinality=Cardinality.ADDITIVE,
            owner=OTHER_OWNER,
        )

        resolution = RESOLVER.resolve_retraction(_retraction("fineliner pen"), (foreign,))

        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert [target.owner_id for target in resolution.targets] == [OTHER_OWNER]


class TestRetractionResolution:
    def _pen(self) -> CanonicalMemorySnapshot:
        return _snapshot(
            "fineliner pen",
            memory_type=MemoryType.KNOWLEDGE,
            domain="art",
            slot="knowledge:art:item:x",
            cardinality=Cardinality.ADDITIVE,
        )

    @pytest.mark.parametrize(
        "hint",
        [
            "fineliner pen",
            "Fineliner Pen",
            "FINELINER PEN!",
            "fineliner_pen",
            "  fineliner   pen  ",
        ],
    )
    def test_matching_ignores_case_punctuation_and_spacing(self, hint: str) -> None:
        """COR-33 — the user retypes a value; they do not copy it back exactly."""

        resolution = RESOLVER.resolve_retraction(_retraction(hint), (self._pen(),))

        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert resolution.reason == "exact_retraction_target"

    def test_a_value_named_inside_a_sentence_still_retracts(self) -> None:
        """Containment runs one way only, which is what makes it safe.

        The user must name at least the whole stored value, so a longer sentence
        containing it matches — but naming one word of a remembered phrase never
        can.
        """

        resolution = RESOLVER.resolve_retraction(
            _retraction("forget that I use a fineliner pen for sketching"),
            (self._pen(),),
        )

        assert resolution.kind is CorrectionResolutionKind.RETRACT

    def test_one_word_of_a_stored_phrase_does_not_retract_it(self) -> None:
        """The other direction of the same rule — over-deletion is the worse error."""

        resolution = RESOLVER.resolve_retraction(_retraction("pen"), (self._pen(),))

        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW
        assert resolution.targets == ()

    def test_a_hint_matching_nothing_needs_review_with_no_targets(self) -> None:
        """COR-32 — nothing resolved means nothing is deleted."""

        resolution = RESOLVER.resolve_retraction(
            _retraction("a fact never stored"), (self._pen(),)
        )

        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW
        assert resolution.reason == "retraction_target_ambiguous_or_missing"
        assert resolution.targets == ()

    def test_a_retraction_cannot_be_built_without_a_hint(self) -> None:
        """COR-32b — the hintless case is unreachable, one layer up.

        The plan asked what happens with "no hint at all".  It cannot happen:
        `old_value_hint` is required with `min_length=1`, so the contract refuses
        the proposal before any resolver sees it.  Asserting that here is more
        honest than a resolver test that could never have been reached.
        """

        with pytest.raises(ValidationError):
            _retraction("")

    def test_retraction_matches_across_memory_types_when_unhinted(self) -> None:
        """COR-34 — a value the user names is retracted wherever it is stored.

        The user says "forget the fineliner pen"; they do not know whether it was
        filed as knowledge or as a preference.  With no type hint, the value
        decides.
        """

        as_preference = _snapshot(
            "fineliner pen",
            memory_type=MemoryType.PREFERENCE,
            domain="art",
            slot="preference:art:tool",
        )

        resolution = RESOLVER.resolve_retraction(_retraction("fineliner pen"), (as_preference,))

        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert resolution.targets == (as_preference,)

    def test_a_type_hint_narrows_the_eligible_set(self) -> None:
        """COR-34b — and when the model does supply a type, it is respected."""

        resolution = RESOLVER.resolve_retraction(
            _retraction("fineliner pen", memory_type=MemoryType.GOAL),
            (self._pen(),),
        )

        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW

    def test_every_stored_copy_of_a_named_value_is_retracted(self) -> None:
        """One fact stored twice must not survive its own deletion."""

        first, second = self._pen(), self._pen()

        resolution = RESOLVER.resolve_retraction(_retraction("fineliner pen"), (first, second))

        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert set(resolution.targets) == {first, second}

    def test_a_fragment_of_a_longer_match_is_left_alone(self) -> None:
        """Only maximal matches are kept, so a delete never reaches past the ask."""

        long_form = _snapshot(
            "fineliner pen and pocket watercolour set",
            memory_type=MemoryType.KNOWLEDGE,
            domain="art",
            slot="knowledge:art:item:y",
            cardinality=Cardinality.ADDITIVE,
        )
        short_form = self._pen()

        resolution = RESOLVER.resolve_retraction(
            _retraction("forget the fineliner pen and pocket watercolour set"),
            (long_form, short_form),
        )

        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert resolution.targets == (long_form,)


class TestRefineIsNeverReturnedHere:
    """COR-18 — corrected: `resolve` never returns REFINE, and should not.

    The plan expected a refined value on an exclusive slot to resolve to REFINE
    rather than CREATE.  `CorrectionResolutionKind.REFINE` exists in the enum but
    is returned by nothing in this module — refinement is a *planning* decision,
    made against the stored record in `planner.py` (`MemoryOutcome.REFINED`,
    pinned by PLN-03), not a resolution decision made against a snapshot list.

    That leaves `REFINE` as an unused enum member. Flagged rather than removed:
    it is pre-existing, and deleting it is a source change this suite has no
    mandate to make.
    """

    def test_a_refinement_of_the_slot_occupant_resolves_to_review_not_refine(self) -> None:
        candidate = _candidate(
            "I want to create YouTube videos about urban sketching",
            "create YouTube videos about urban sketching",
            slot_hint="current_primary_goal",
        )
        occupant = _snapshot("create YouTube videos")

        resolution = RESOLVER.resolve(candidate, (occupant,))

        assert resolution.kind is not CorrectionResolutionKind.REFINE
        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW
        assert resolution.targets == (occupant,)

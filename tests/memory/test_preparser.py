"""Tier 2 — the deterministic preparser (plan section PRE).

Before any model call, the preparser reads the turn with plain regexes and
decides one of three things: this is a fact I can extract myself, this needs the
model, or this contains nothing to store.  Every turn it handles deterministically
is a local model call that never happens — and, more importantly, a fact that
gets stored identically every single time rather than however the model felt.

These tests pin what the preparser *actually* classifies rather than what its
pattern names suggest.  Several patterns exist but hand off to the model anyway,
carrying a hint rather than a finished answer; that distinction is invisible from
the source and easy to break, so it is written down here.
"""

from __future__ import annotations

import pytest

from app.services.memory.extraction_contracts import (
    ConversationRole,
    ExtractionMode,
    ExtractionRequest,
    LifecycleHint,
    PreparseKind,
    TrustedConversationMessage,
)
from app.services.memory.preparser import (
    _has_multiple_statements,
    _video_verb,
    deterministic_model_response,
    preparse,
)
from tests.memory.conftest import OWNER_ID


def _request(
    message: str,
    *,
    message_id: str = "m1",
    explicit: bool = True,
    window: tuple[TrustedConversationMessage, ...] = (),
):
    return ExtractionRequest(
        request_id="request-1",
        owner_id=OWNER_ID,
        conversation_id="c1",
        session_id="s1",
        message_id=message_id,
        user_message=message,
        explicit_memory_intent=explicit,
        mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
        supporting_window=window,
        source_content_hash=ExtractionRequest.content_hash(message),
    )


def _parse(message: str, *, window: tuple[TrustedConversationMessage, ...] = ()):
    return preparse(_request(message, window=window))


def _prior_user_turn(content: str, *, message_id: str = "m0") -> TrustedConversationMessage:
    """One earlier user message, for the patterns that refer back to one."""

    return TrustedConversationMessage(
        message_id=message_id,
        role=ConversationRole.USER,
        content=content,
    )


class TestIdentityFacts:
    """The facts a personal assistant is asked for most, handled without a model."""

    @pytest.mark.parametrize(
        ("message", "value"),
        [
            ("My name is Soham.", "Soham"),
            ("My name is Soham Chawla.", "Soham Chawla"),
            ("My full name is Soham Chawla.", "Soham Chawla"),
            ("I am called Soham.", "Soham"),
        ],
    )
    def test_a_name_is_extracted_deterministically(self, message: str, value: str) -> None:
        """PRE-01"""

        result = _parse(message)
        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert [span.normalized_value for span in result.assertions] == [value]
        assert result.assertions[0].memory_type_hint == "identity"

    @pytest.mark.parametrize(
        "message",
        [
            "Call me Soham.",
            "Please call me Soham.",
            "You can call me Soham.",
            "I'm Soham.",
            "I am Soham.",
        ],
    )
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known gap: policy._MEMORY_COMMAND lists 'call me' as an explicit "
            "memory instruction, but preparser._DIRECT_NAME only matches "
            "'My name is X' and 'I am called X'. So 'Call me Soham' passes the "
            "extraction gate and then falls through to the model. Remove this "
            "xfail when _DIRECT_NAME covers the call-me and bare-copula forms."
        ),
    )
    def test_other_natural_name_phrasings_are_currently_left_to_the_model(
        self, message: str
    ) -> None:
        """PRE-01b — a gap found while writing PRE-01, recorded not patched.

        Two parts of the system disagree about "call me X".

        ``policy._MEMORY_COMMAND`` lists ``call me`` alongside ``remember`` and
        ``forget`` as a verb that definitely signals a memory instruction — and
        ``test_extraction_gate.py`` already asserts that "Call me Soham." opens
        the gate.  But ``preparser._DIRECT_NAME`` matches only ``My name is X``
        and ``I am called X``, so the turn passes the gate and then falls
        through to the local model.

        That means the most direct way a person states their name depends on the
        model being available and getting it right, when a two-branch regex
        already handles the less common phrasing deterministically.  A name is
        the single identity fact a personal assistant is asked for most, so this
        is the worst place to depend on a model call.
        """

        result = _parse(message)
        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.assertions[0].normalized_value == "Soham"

    def test_an_age_is_extracted(self) -> None:
        """PRE-02"""

        result = _parse("I am 21 years old.")
        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.assertions[0].normalized_value == "21"

    def test_an_origin_is_extracted(self) -> None:
        """PRE-03"""

        result = _parse("I'm from Delhi.")
        assert result.assertions[0].normalized_value == "Delhi"

    def test_an_employer_is_extracted(self) -> None:
        """PRE-04"""

        result = _parse("I work at Acme Corp.")
        assert result.assertions[0].normalized_value == "Acme Corp"

    def test_an_occupation_is_extracted(self) -> None:
        """PRE-05"""

        result = _parse("I work as a designer.")
        assert result.assertions[0].normalized_value == "designer"

    def test_a_current_location_is_extracted(self) -> None:
        """PRE-21"""

        result = _parse("I live in Berlin.")
        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.assertions[0].normalized_value == "Berlin"


class TestPreferences:
    def test_a_global_response_style_is_deterministic(self) -> None:
        """PRE-11 — "always answer me X" is an instruction, not a topic."""

        result = _parse("Always answer me briefly.")
        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.assertions[0].memory_type_hint == "preference"

    def test_a_bare_preference_carries_a_hint_but_still_asks_the_model(self) -> None:
        """PRE-09 — pinning a distinction the pattern names hide.

        ``_DIRECT_PREFERENCE`` matches "I prefer X" and produces an assertion
        span, but the result is still ``MODEL_REQUIRED``.  The preparser is
        saying "here is what I think this is, but decide the domain yourself" —
        a bare preference has no topic, and guessing one is exactly the failure
        the taxonomy refuses to make.
        """

        result = _parse("I prefer concise answers.")
        assert result.kind is PreparseKind.MODEL_REQUIRED
        assert [span.normalized_value for span in result.assertions] == ["concise answers"]


class TestExplicitInstructions:
    def test_a_remember_instruction_is_deterministic(self) -> None:
        """PRE-13"""

        result = _parse("Remember that the studio closes at 6pm.")
        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.assertions[0].normalized_value == "the studio closes at 6pm"

    def test_a_forget_instruction_becomes_a_retraction(self) -> None:
        """PRE-27"""

        result = _parse("Forget that I use a fineliner pen.")
        assert result.kind is PreparseKind.EXPLICIT_LIFECYCLE
        assert result.lifecycle_hint is LifecycleHint.FORGET
        assert len(result.retractions) == 1
        assert result.assertions == ()

    def test_a_forget_names_what_is_to_go(self) -> None:
        """PRE-27b — the retraction has to carry enough to resolve a target."""

        result = _parse("Forget that I use a fineliner pen.")
        assert "fineliner pen" in result.retractions[0].normalized_value


class TestNonFacts:
    """Turns that state nothing storable, and must not become memories."""

    def test_a_hypothetical_is_ignored(self) -> None:
        """PRE-24"""

        result = _parse("If I were a musician I'd play jazz.")
        assert result.kind is PreparseKind.IGNORE
        assert result.assertions == ()

    def test_a_question_is_ignored(self) -> None:
        """PRE-30b — the regression that created three records from one question."""

        result = _parse("What do you remember about my goals?")
        assert result.kind is PreparseKind.IGNORE
        assert result.assertions == ()

    @pytest.mark.parametrize(
        "message",
        [
            "My brother likes jazz.",
            "I'm in Paris this week.",
            "That is my favourite.",
        ],
    )
    def test_ambiguous_turns_are_handed_to_the_model_not_stored(self, message: str) -> None:
        """PRE-22 / PRE-25 / PRE-26 — pinning where the boundary actually sits.

        Third-party statements, transient locations and ambiguous pronouns are
        all shapes that must never be stored as durable facts about the user.
        The preparser does not itself refuse them — it declines to answer and
        routes them to the model, which then has to ground them.  The safety
        property is held downstream by grounding and subject checks, not here.

        Worth writing down because the pattern names (``_THIRD_PARTY``,
        ``_TRANSIENT_LOCATION``, ``_AMBIGUOUS_PRONOUN``) read as though the
        preparser rejects these outright, and it does not.
        """

        result = _parse(message)
        assert result.kind is PreparseKind.MODEL_REQUIRED
        assert result.assertions == ()

    def test_an_empty_message_is_refused_at_the_contract(self) -> None:
        """PRE-30 — an empty turn never reaches the preparser at all."""

        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _request("")


class TestDeterminism:
    @pytest.mark.parametrize(
        "message",
        [
            "My name is Soham.",
            "Always answer me briefly.",
            "Forget that I use a fineliner pen.",
            "What do you remember about my goals?",
            "I work at Acme Corp.",
        ],
    )
    def test_preparsing_is_deterministic(self, message: str) -> None:
        """PRE-31 — the whole point of the deterministic path."""

        first = _parse(message)
        second = _parse(message)
        assert first == second

    @pytest.mark.parametrize(
        "message",
        [
            "My name is Soham.",
            "I am 21 years old.",
            "I work at Acme Corp.",
            "Always answer me briefly.",
            "Remember that the studio closes at 6pm.",
            "I live in Berlin.",
        ],
    )
    def test_every_span_indexes_the_real_message(self, message: str) -> None:
        """PRE-29 — offsets are provenance, so they must actually be right.

        A span whose offsets do not select its own quoted text would produce a
        content hash for the wrong excerpt, and the grounding check downstream
        would either reject the fact or, worse, attest to the wrong words.
        """

        result = _parse(message)
        for span in (*result.assertions, *result.retractions):
            assert message[span.start : span.end] == span.quoted_text


class TestDeterministicModelResponse:
    def test_a_deterministic_result_converts_to_a_valid_model_response(self) -> None:
        """PRE-32 — the deterministic path reuses the model's own contract.

        Rather than a second code path, a preparsed result is turned into the
        same ``ModelProposalResponse`` the model would have produced, so every
        downstream check applies identically to both.
        """

        result = _parse("My name is Soham.")
        response = deterministic_model_response(result)
        assert len(response.assertions) == 1
        assert response.assertions[0].typed_value == "Soham"

    def test_the_converted_response_carries_its_source_spans(self) -> None:
        """PRE-32b — without spans it could not be grounded."""

        response = deterministic_model_response(_parse("My name is Soham."))
        assert response.assertions[0].source_spans

    def test_a_deterministic_assertion_grounds_against_its_own_message(self) -> None:
        """PRE-32c — the deterministic path must satisfy the same gate.

        This is the test that ties the preparser to the security boundary: what
        it produces has to pass ``ground_assertion`` exactly as model output
        does, with no exemption.
        """

        from app.services.memory.grounding import ground_assertion

        message = "My name is Soham."
        request = _request(message)
        response = deterministic_model_response(preparse(request))
        decision = ground_assertion(request, response.assertions[0])
        assert decision.accepted is True, decision.reason

    def test_a_retraction_converts_and_grounds(self) -> None:
        """PRE-32d"""

        from app.services.memory.grounding import ground_retraction

        message = "Forget that I use a fineliner pen."
        request = _request(message)
        response = deterministic_model_response(preparse(request))
        assert response.retractions
        decision = ground_retraction(request, response.retractions[0])
        assert decision.accepted is True, decision.reason

    def test_an_ignored_turn_converts_to_an_empty_response(self) -> None:
        """PRE-32e"""

        response = deterministic_model_response(_parse("What do you remember?"))
        assert response.assertions == ()
        assert response.retractions == ()


class TestExplicitIntent:
    def test_explicit_intent_is_carried_through(self) -> None:
        """PRE-13b — an explicit instruction is treated differently downstream."""

        result = _parse("Remember that the studio closes at 6pm.")
        assert result.explicit_memory_intent is True

    def test_every_result_carries_a_reason(self) -> None:
        """PRE-33b — the classification has to be explainable in diagnostics."""

        for message in (
            "My name is Soham.",
            "What do you remember?",
            "If I were a musician I'd play jazz.",
            "My brother likes jazz.",
        ):
            assert _parse(message).reason


class TestProjectsAndGoals:
    """The patterns that decide which *field* a fact lands in.

    Project is not just another type: the projects field controls which chats can
    read a memory, so a fact misfiled as a project changes who sees it.  That is
    why `_DIRECT_PROJECT` carries a negative lookahead and why it is tested from
    both sides.
    """

    def test_a_named_project_is_extracted(self) -> None:
        """PRE-06"""

        result = _parse("I am working on a project called Neo.")

        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.reason == "durable_named_project"
        assert [span.normalized_value for span in result.assertions] == ["Neo"]
        assert result.assertions[0].memory_type_hint == "project"

    def test_a_possessive_pursuit_is_not_a_project(self) -> None:
        """PRE-06b — the lookahead that keeps "my fitness" out of the projects field.

        "I am working on my fitness" is a personal pursuit, not a named body of
        work.  Since the projects field is a read-permission boundary, the
        ambiguous phrasing is handed to the model rather than forced into it.
        """

        result = _parse("I am working on my fitness.")

        assert result.kind is PreparseKind.MODEL_REQUIRED
        assert result.assertions == ()

    def test_a_direct_goal_in_a_known_domain_is_deterministic(self) -> None:
        """PRE-07"""

        result = _parse("I want to make YouTube videos.")

        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.reason == "durable_first_person_goal"
        assert result.deterministic is True
        span = result.assertions[0]
        assert span.memory_type_hint == "goal"
        assert span.domain_hint == "video_creation"
        assert span.slot_hint == "current_primary_goal"

    def test_a_direct_goal_outside_a_known_domain_carries_a_hint_only(self) -> None:
        """PRE-07b — same pattern, but `deterministic` is False.

        The pattern still matches and still produces an assertion, but with no
        domain it cannot pick a slot, so the result is not marked deterministic
        and the model is still consulted.  The `kind` is identical either way,
        which makes this distinction invisible unless it is asserted directly.
        """

        result = _parse("I want to travel more.")

        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.deterministic is False
        span = result.assertions[0]
        assert span.domain_hint is None
        assert span.slot_hint is None

    def test_a_now_goal_is_ambiguous_rather_than_a_correction(self) -> None:
        """PRE-08 — "Now I want …" reads as a correction and is not treated as one.

        The phrasing implies something is being superseded, but the turn names no
        predecessor, so there is nothing to retract against.  Guessing would
        supersede whatever happened to occupy the slot, so it is classified
        AMBIGUOUS and handed on with a hint.  `ambiguous_subject` stays False —
        the *subject* is clear, it is the *target* that isn't.
        """

        result = _parse("Now I want to make short films.")

        assert result.kind is PreparseKind.AMBIGUOUS
        assert result.reason == "unlinked_current_goal_assertion"
        assert result.retractions == ()
        assert result.ambiguous_subject is False
        assert result.assertions[0].memory_type_hint == "goal"

    def test_a_now_goal_does_not_normalise_its_verb(self) -> None:
        """PRE-08b — pinning an inconsistency rather than fixing it.

        Every other goal path runs its value through `_video_verb`, so
        "make videos" is stored as "create videos" and matches a later
        restatement.  This path does not, so the same goal arriving via
        "Now I want to make X" is stored under a different string.  Left as-is
        because it is only reachable on the AMBIGUOUS path, where the model gets
        the final say anyway — but written down so a future change is a decision.
        """

        result = _parse("Now I want to make short films.")

        assert result.assertions[0].normalized_value == "make short films"

    def test_additive_goals_produce_two_independent_goals(self) -> None:
        """PRE-14 — "still … and also" is the one phrasing that never replaces."""

        result = _parse("I still want to grow my channel, and I also want to learn Rust.")

        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.reason == "explicit_additive_language"
        assert [span.normalized_value for span in result.assertions] == [
            "to grow my channel",
            "to learn Rust",
        ]
        assert all(span.additive for span in result.assertions)
        assert result.retractions == ()


class TestDomainScopedPreferences:
    def test_a_domain_preference_is_scoped_to_its_domain(self) -> None:
        """PRE-10 — the same words mean different things in different domains.

        "Keep it brief" as a global style and "keep it brief" for video-editing
        advice are two separate memories; storing the second globally would
        silently change every answer.
        """

        result = _parse("For video-editing advice, keep it brief.")

        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.reason == "domain_specific_preference"
        span = result.assertions[0]
        assert span.memory_type_hint == "preference"
        assert span.domain_hint == "video_creation"
        assert span.slot_hint == "practice_advice_format"

    def test_a_goal_and_a_global_style_yield_two_memories(self) -> None:
        """PRE-12 — one turn, two unrelated facts, and neither may absorb the other.

        Collapsing these into one memory would scope the answering style to video
        work, or the video goal to everything.  They go to different domains and
        different slots.
        """

        result = _parse("I want to make YouTube videos. Always answer me briefly.")

        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.reason == "separate_goal_and_global_preference"
        assert len(result.assertions) == 2
        goal, preference = result.assertions
        assert (goal.memory_type_hint, goal.domain_hint) == ("goal", "video_creation")
        assert (preference.memory_type_hint, preference.domain_hint) == ("preference", "global")
        assert preference.slot_hint == "verbosity"


class TestCorrections:
    """Every correction pairs a retraction with its replacement.

    The pairing is what makes it a correction rather than two unrelated edits:
    both spans carry the same `correction_group`, so the downstream planner knows
    to apply them together or not at all.
    """

    def test_an_implicit_goal_correction_pairs_both_halves(self) -> None:
        """PRE-15"""

        result = _parse("I no longer want to make YouTube videos. I want to write a book.")

        assert result.kind is PreparseKind.DETERMINISTIC_CORRECTION
        assert result.reason == "linked_goal_retraction_assertion"
        assert [span.normalized_value for span in result.assertions] == ["write a book"]
        assert [span.normalized_value for span in result.retractions] == ["create YouTube videos"]
        assert (
            result.assertions[0].correction_group
            == result.retractions[0].correction_group
            == "foreground-correction-1"
        )

    def test_the_retracted_goal_is_normalised_to_match_what_was_stored(self) -> None:
        """PRE-15b — the retraction must name the value as it was *written*.

        The goal was stored through `_video_verb`, so "make YouTube videos"
        became "create YouTube videos".  A retraction that named the user's
        original wording would match no stored record and the correction would be
        silently dropped.
        """

        result = _parse("I no longer want to make YouTube videos. I want to write a book.")

        assert result.retractions[0].normalized_value == "create YouTube videos"

    def test_an_explicit_replace_carries_an_old_value_hint(self) -> None:
        """PRE-16 — "Correction: replace X with Y" names its own target."""

        result = _parse("Correction: replace urban sketching with oil painting.")

        assert result.kind is PreparseKind.DETERMINISTIC_CORRECTION
        assert result.reason == "explicit_replace_grammar"
        assert [span.normalized_value for span in result.retractions] == ["urban sketching"]
        assert [span.normalized_value for span in result.assertions] == ["oil painting"]
        assert result.retractions[0].correction_group == result.assertions[0].correction_group

    def test_an_explicit_replace_does_not_span_sentences(self) -> None:
        """PRE-16b — the bounded character class, from the comment above the pattern.

        An earlier `.+?` let this grammar latch onto a "with" several sentences
        later, producing a retraction target that matched nothing — so the
        correction was dropped rather than applied.  Both halves are now confined
        to one statement.
        """

        result = _parse(
            "Correction: replace urban sketching with oil painting. "
            "I am experimenting with watercolour."
        )

        # `fullmatch` against a bounded character class means a second sentence
        # defeats the grammar outright: the turn goes to the model rather than
        # being extracted with a target stitched together across sentences.
        assert result.kind is PreparseKind.MODEL_REQUIRED
        assert result.retractions == ()
        assert result.assertions == ()

    def test_a_preference_correction_pairs_both_halves(self) -> None:
        """PRE-17"""

        result = _parse("I do not prefer long answers anymore. I prefer short answers.")

        assert result.kind is PreparseKind.DETERMINISTIC_CORRECTION
        assert result.reason == "linked_preference_retraction_assertion"
        assert [span.normalized_value for span in result.retractions] == ["long answers"]
        assert [span.normalized_value for span in result.assertions] == ["short answers"]
        assert all(span.slot_hint == "verbosity" for span in result.assertions)

    def test_two_consecutive_corrections_stay_separate(self) -> None:
        """PRE-19 — a compound turn corrects two memories without merging them.

        The scanner composes only complete, adjacent known correction pairs.  The
        distinct `correction_group` values are the whole point: without them the
        goal retraction and the preference assertion could be applied as one
        edit, which is how a correction ends up pointing at the wrong record.
        """

        result = _parse(
            "I no longer want to make YouTube videos. I want to write a book. "
            "I do not prefer long answers anymore. I prefer short answers."
        )

        assert result.kind is PreparseKind.DETERMINISTIC_CORRECTION
        assert result.reason == "consecutive_explicit_corrections"
        assert len(result.assertions) == 2
        assert len(result.retractions) == 2
        assert [span.correction_group for span in result.assertions] == [
            "foreground-correction-1",
            "foreground-correction-2",
        ]
        assert [span.memory_type_hint for span in result.assertions] == ["goal", "preference"]

    def test_a_single_correction_is_not_treated_as_compound(self) -> None:
        """PRE-19b — the scanner requires two pairs before it claims the turn.

        With one pair it returns None and lets the single-correction grammars
        handle it, which is why PRE-15 sees `linked_goal_retraction_assertion`
        rather than `consecutive_explicit_corrections`.
        """

        result = _parse("I no longer want to make YouTube videos. I want to write a book.")

        assert result.reason == "linked_goal_retraction_assertion"

    def test_a_pure_location_retraction_retracts_without_asserting(self) -> None:
        """PRE-20 — "I no longer live in X" says nothing about where the user *is*.

        Inventing a replacement location would be fabrication, so this produces a
        retraction and no assertion at all.  It archives rather than forgets:
        the user moved, they were not misrecorded.
        """

        result = _parse("I no longer live in Berlin.")

        assert result.kind is PreparseKind.EXPLICIT_LIFECYCLE
        assert result.reason == "grounded_location_retraction"
        assert result.assertions == ()
        assert [span.normalized_value for span in result.retractions] == ["Berlin"]
        assert result.retractions[0].slot_hint == "current_location"
        assert result.lifecycle_hint is LifecycleHint.ARCHIVE


class TestCategoryCorrection:
    """PRE-18 — "that is a goal, not a preference" retargets an earlier fact.

    This is the only pattern that reaches back into the conversation window, so
    it is the only one whose result depends on a message other than this turn's.
    """

    def test_a_category_correction_retargets_the_referenced_goal(self) -> None:
        result = _parse(
            "That is a goal, not a preference.",
            window=(_prior_user_turn("I want to make YouTube videos."),),
        )

        assert result.kind is PreparseKind.DETERMINISTIC_ASSERTION
        assert result.reason == "bounded_recent_goal_category_correction"
        span = result.assertions[0]
        assert span.normalized_value == "create YouTube videos"
        assert span.memory_type_hint == "goal"
        assert span.explicit_type_change is True

    def test_the_correction_is_grounded_in_the_earlier_message(self) -> None:
        """PRE-18b — the span points at the prior turn, not at this one.

        The value being corrected was never written in this message, so citing
        this message as its source would be a grounding failure.  The current
        turn is attached as an *additional* span, which is what makes the
        correction auditable without misattributing the fact.
        """

        result = _parse(
            "That is a goal, not a preference.",
            window=(_prior_user_turn("I want to make YouTube videos."),),
        )

        span = result.assertions[0]
        assert span.message_id == "m0"
        assert len(span.additional_source_spans) == 1
        assert span.additional_source_spans[0].message_id == "m1"

    @pytest.mark.parametrize(
        "window",
        [
            (),
            (_prior_user_turn("The weather is nice."),),
        ],
        ids=["no_prior_turn", "unrelated_prior_turn"],
    )
    def test_an_unresolvable_reference_is_ambiguous_not_guessed(
        self, window: tuple[TrustedConversationMessage, ...]
    ) -> None:
        """PRE-18c — with nothing to point at, it refuses rather than picks.

        Retargeting the wrong memory's *category* is worse than not retargeting:
        it moves a fact into a slot that changes how it is recalled.
        """

        result = _parse("That is a goal, not a preference.", window=window)

        assert result.kind is PreparseKind.AMBIGUOUS
        assert result.reason == "category_reference_unresolved"
        assert result.ambiguous_subject is True


class TestTemporaryStatements:
    def test_a_temporary_state_is_not_durable(self) -> None:
        """PRE-23"""

        result = _parse("I have a headache right now.")

        assert result.kind is PreparseKind.IGNORE
        assert result.reason == "temporary_state"
        assert result.temporary is True
        assert result.assertions == ()

    def test_a_durable_phrasing_survives_temporary_wording(self) -> None:
        """PRE-23b — the exemption that keeps "right now" from eating a preference.

        "I prefer X right now" contains temporary wording but states a standing
        preference.  The exemption list rescues it: the turn is no longer ignored
        and reaches the model with its hint intact.
        """

        result = _parse("I prefer tea right now.")

        assert result.kind is not PreparseKind.IGNORE
        assert result.reason == "first_person_preference"
        assert result.assertions[0].memory_type_hint == "preference"


class TestMultipleStatements:
    """PRE-28 — why a whole-message ignore rule must not fire on mixed prose."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("one two", False),
            ("one. two", True),
            ("one? two", True),
            ("one! two", True),
            ("one.two", False),
            ("one.", False),
        ],
    )
    def test_statement_splitting(self, text: str, expected: bool) -> None:
        """A terminator alone isn't a split; it needs to be followed by more text."""

        assert _has_multiple_statements(text) is expected

    def test_a_single_hypothetical_statement_is_ignored(self) -> None:
        result = _parse("I might travel someday.")

        assert result.kind is PreparseKind.IGNORE
        assert result.reason == "hypothetical_language"

    def test_a_hypothetical_beside_a_real_fact_is_not_ignored(self) -> None:
        """The behaviour the helper exists for.

        Without the split check, one hedging word anywhere in the turn would
        discard the whole message — and the name stated in the second sentence
        would be lost with it.  The turn is handed to the model instead, which is
        the conservative outcome: nothing is stored deterministically, but
        nothing is silently dropped either.
        """

        result = _parse("I might travel someday. My name is Soham.")

        assert result.kind is not PreparseKind.IGNORE
        assert result.kind is PreparseKind.MODEL_REQUIRED


class TestVideoVerbNormalisation:
    """PRE-34 — one goal phrased two ways has to fold to one stored value."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("make YouTube videos", "create YouTube videos"),
            ("making reels", "create reels"),
            ("Make a short film", "create a short film"),
            ("create YouTube videos", "create YouTube videos"),
        ],
    )
    def test_leading_make_folds_to_create(self, value: str, expected: str) -> None:
        assert _video_verb(value) == expected

    def test_only_a_leading_verb_is_rewritten(self) -> None:
        """The anchor matters: rewriting mid-sentence would corrupt the value.

        "I want to make videos" is the raw turn, not the extracted value; folding
        its interior "make" would produce a stored value that no longer quotes
        what the user wrote.
        """

        assert _video_verb("I want to make videos") == "I want to make videos"

    def test_the_value_is_cleaned_as_well_as_folded(self) -> None:
        assert _video_verb("  make  a film. ") == "create a film"

    def test_a_goal_and_its_correction_fold_to_the_same_value(self) -> None:
        """The reason this function exists, asserted end to end.

        The goal is stored from "I want to make YouTube videos" and retracted by
        "I no longer want to make YouTube videos".  If the two paths normalised
        differently, the retraction would match nothing.
        """

        stored = _parse("I want to make YouTube videos.")
        retracted = _parse("I no longer want to make YouTube videos. I want to write a book.")

        assert stored.assertions[0].normalized_value == retracted.retractions[0].normalized_value

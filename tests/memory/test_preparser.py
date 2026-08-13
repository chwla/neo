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
    ExtractionMode,
    ExtractionRequest,
    LifecycleHint,
    PreparseKind,
)
from app.services.memory.preparser import deterministic_model_response, preparse
from tests.memory.conftest import OWNER_ID


def _request(message: str, *, message_id: str = "m1", explicit: bool = True):
    return ExtractionRequest(
        request_id="request-1",
        owner_id=OWNER_ID,
        conversation_id="c1",
        session_id="s1",
        message_id=message_id,
        user_message=message,
        explicit_memory_intent=explicit,
        mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
        source_content_hash=ExtractionRequest.content_hash(message),
    )


def _parse(message: str):
    return preparse(_request(message))


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

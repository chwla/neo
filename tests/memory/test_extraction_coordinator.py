"""Tier 2 — the extraction coordinator (plan section EXC).

This is where untrusted model output meets the store.  Everything before it is
analysis; everything after it is a durable row in the user's database.  So the
tests here are less about "does it extract" and more about the seams:

* the gate, which must refuse to run at all when memory is off, the turn is
  incognito, or the request does not belong to the context it arrived with;
* degradation, because the local model is an ``ollama`` process that may simply
  not be running, and a missing model must never turn into a failed chat turn;
* the caps, which bound how much one turn can write;
* the override, which decides what the *current* reply is allowed to say about
  memories this turn just changed.

The model is scripted (see ``doubles.py``); everything underneath it is real.
An accepted candidate here really is written to a real SQLite file, which is why
the idempotency and duplicate tests can process the same turn twice and look at
what actually landed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.memory.contracts import Sensitivity
from app.services.memory.extraction_contracts import (
    CandidateAction,
    ExtractionMode,
    ExtractionRequest,
    ExtractionStatus,
)
from app.services.memory.extraction_coordinator import (
    REDACTED_SENSITIVE_ASSERTION,
    CurrentTurnOverrideBuilder,
)
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID
from tests.memory.doubles import (
    RecordingModel,
    StaticDuplicateFinder,
    UnavailableModel,
    assertion,
    model_output,
    retraction,
    scripted_model,
)

PYTHON_MESSAGE = "I use Python for work."


def request_for(
    message: str,
    *,
    mode: ExtractionMode = ExtractionMode.POST_TURN_AUTOMATIC,
    message_id: str = "m1",
    **overrides,
) -> ExtractionRequest:
    fields = {
        "request_id": "request-1",
        "owner_id": OWNER_ID,
        "conversation_id": "c1",
        "session_id": "s1",
        "message_id": message_id,
        "user_message": message,
        "mode": mode,
        "source_content_hash": ExtractionRequest.content_hash(message),
    }
    fields.update(overrides)
    return ExtractionRequest(**fields)


def python_model(message: str = PYTHON_MESSAGE, **extra):
    """The standard accepted case: one durable, confident, first-person fact."""

    return scripted_model(
        {
            message: model_output(
                assertions=[
                    assertion(
                        message,
                        "Python",
                        memory_type="knowledge",
                        domain_hint="software_development",
                        **extra,
                    )
                ]
            )
        }
    )


class TestTheGate:
    """Nothing runs until the request is shown to belong where it arrived."""

    def test_memory_disabled_short_circuits(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-01 — the off switch has to work before anything is read."""

        model = python_model()
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(
            request_for(PYTHON_MESSAGE, memory_enabled=False), adapter_context
        )
        assert result.status is ExtractionStatus.DISABLED
        assert model.call_count == 0
        assert result.diagnostic.reason_codes == ("memory_disabled_extraction",)

    def test_disabled_by_the_context_not_only_the_request(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-01b — the request cannot re-enable what the profile turned off.

        Two places carry the flag: the request, and the execution context built
        from the profile.  If only the request were consulted, a caller that
        constructed its own request would bypass the user's setting entirely.
        """

        model = python_model()
        coordinator = extraction_coordinator_factory(model)
        context = replace(
            adapter_context,
            execution=replace(adapter_context.execution, memory_enabled=False),
        )
        result = coordinator.process(request_for(PYTHON_MESSAGE), context)
        assert result.status is ExtractionStatus.DISABLED
        assert model.call_count == 0

    def test_incognito_short_circuits(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-02 — an incognito turn must leave no trace."""

        model = python_model()
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(request_for(PYTHON_MESSAGE, incognito=True), adapter_context)
        assert result.status is ExtractionStatus.DISABLED
        assert result.diagnostic.reason_codes == ("incognito_extraction_disabled",)
        assert model.call_count == 0

    def test_incognito_from_the_context_also_short_circuits(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-02b"""

        model = python_model()
        coordinator = extraction_coordinator_factory(model)
        context = replace(
            adapter_context,
            execution=replace(adapter_context.execution, is_incognito=True),
        )
        result = coordinator.process(request_for(PYTHON_MESSAGE), context)
        assert result.status is ExtractionStatus.DISABLED
        assert model.call_count == 0

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("owner_id", OTHER_OWNER_ID, "request_owner_context_mismatch"),
            ("message_id", "other-message", "request_message_context_mismatch"),
            ("conversation_id", "other-conversation", "request_conversation_context_mismatch"),
            ("session_id", "other-session", "request_session_context_mismatch"),
        ],
    )
    def test_a_request_that_does_not_match_its_context_is_rejected(
        self, extraction_coordinator_factory, adapter_context, field, value, reason
    ) -> None:
        """EXC-28 — the boundary that stops one profile writing into another.

        A request carries an owner, and so does the execution context that
        supplies the database.  If those ever disagree the write would land in
        whichever database the context happened to point at.  This is checked
        before anything else, and a mismatch is ``REJECTED`` rather than
        ``DISABLED``: it is not a setting, it is a violation.
        """

        model = python_model()
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(request_for(PYTHON_MESSAGE, **{field: value}), adapter_context)
        assert result.status is ExtractionStatus.REJECTED
        assert result.diagnostic.reason_codes == (reason,)
        assert model.call_count == 0

    def test_extraction_disabled_by_flag(self, coordinator_with_flags, adapter_context) -> None:
        """EXC-01c — extraction can be turned off without turning memory off."""

        model = python_model()
        coordinator = coordinator_with_flags(model, extraction_enabled=False)
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert result.status is ExtractionStatus.DISABLED
        assert result.diagnostic.reason_codes == ("memory_extraction_disabled",)
        assert model.call_count == 0

    def test_post_turn_extraction_can_be_disabled_alone(
        self, coordinator_with_flags, adapter_context
    ) -> None:
        """EXC-01d — the automatic path is separately switchable.

        The deterministic foreground path is cheap and predictable; the
        model-backed post-turn path is neither.  Being able to keep the first
        while switching off the second is the setting a cautious user wants.
        """

        coordinator = coordinator_with_flags(python_model(), post_turn_extraction_enabled=False)
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert result.status is ExtractionStatus.DISABLED
        assert result.diagnostic.reason_codes == ("post_turn_extraction_disabled",)

    def test_the_foreground_path_survives_the_post_turn_switch(
        self, coordinator_with_flags, adapter_context
    ) -> None:
        """EXC-01d2 — the other half of the claim above."""

        coordinator = coordinator_with_flags(None, post_turn_extraction_enabled=False)
        result = coordinator.process(
            request_for("My name is Soham.", mode=ExtractionMode.FOREGROUND_DETERMINISTIC),
            adapter_context,
        )
        assert result.status is ExtractionStatus.APPLIED

    def test_an_oversized_message_is_refused(self, coordinator_with_flags, adapter_context) -> None:
        """EXC-01e — a bound on how much text one turn can send to the model."""

        # Stripped, because the contract strips whitespace before hashing the
        # message and an unstripped fixture would fail on the hash instead.
        message = ("I use Python for work. " * 40).strip()
        coordinator = coordinator_with_flags(
            scripted_model({message: model_output()}), extraction_max_input_chars=500
        )
        result = coordinator.process(request_for(message), adapter_context)
        assert result.status is ExtractionStatus.DISABLED
        assert result.diagnostic.reason_codes == ("extraction_input_too_large",)

    def test_an_unsupported_transport_is_a_programming_error(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-01f — this one raises, deliberately.

        Every other refusal in this class is a *result*, because it describes
        something about the user's turn.  An unknown transport describes a
        caller bug, and returning a tidy result for it would let the mistake
        ship.
        """

        coordinator = extraction_coordinator_factory(python_model())
        with pytest.raises(ValueError, match="unsupported_chat_transport"):
            coordinator.process(
                request_for(PYTHON_MESSAGE), adapter_context, transport="carrier-pigeon"
            )


class TestNothingToExtract:
    def test_a_question_returns_no_action_without_calling_the_model(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-03 — the regression that created three records from one question."""

        message = "What do you remember about my goals?"
        model = scripted_model({message: model_output()})
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(request_for(message), adapter_context)
        assert result.status is ExtractionStatus.NO_ACTION
        assert model.call_count == 0
        assert result.decisions == ()

    def test_a_hypothetical_returns_no_action(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-03b"""

        message = "If I were a musician I'd play jazz."
        coordinator = extraction_coordinator_factory(scripted_model({message: model_output()}))
        result = coordinator.process(request_for(message), adapter_context)
        assert result.status is ExtractionStatus.NO_ACTION


class TestTheDeterministicPath:
    def test_a_deterministic_preparse_never_calls_the_model(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-04 — the whole point of the preparser.

        "My name is X" is handled by a regex, so the model is not consulted at
        all.  ``called=False`` in the summary is what tells diagnostics — and
        anyone debugging later — that this fact did not depend on a model.
        """

        message = "My name is Soham."
        model = scripted_model({message: model_output()})
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(request_for(message), adapter_context)
        assert model.call_count == 0
        assert result.model_summary.called is False
        assert result.status is ExtractionStatus.APPLIED

    def test_the_deterministic_path_still_records_a_content_hash(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-04b — provenance does not depend on a model being involved."""

        result = extraction_coordinator_factory(None).process(
            request_for("My name is Soham."), adapter_context
        )
        assert result.model_summary.raw_output_hash is not None

    def test_the_deterministic_path_works_with_no_model_configured(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-04c — a name is stored even with ollama uninstalled.

        This is the property that keeps the memory layer usable on a machine
        with no local model at all.
        """

        coordinator = extraction_coordinator_factory(None)
        result = coordinator.process(request_for("My name is Soham."), adapter_context)
        assert result.status is ExtractionStatus.APPLIED
        assert [item.action for item in result.decisions] == [CandidateAction.CREATE]

    def test_a_model_turn_without_a_model_fails_cleanly(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-05c — and a turn that *does* need the model says so plainly."""

        coordinator = extraction_coordinator_factory(None)
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert result.status is ExtractionStatus.FAILED
        assert result.diagnostic.reason_codes == ("extraction_model_not_configured",)

    def test_the_foreground_mode_refuses_to_call_a_model(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-26b — foreground means deterministic, by construction.

        The foreground path runs while the user waits for a reply, so it is
        allowed only structure it can resolve itself.  Anything else stops with
        ``foreground_structure_not_deterministic`` rather than blocking the
        turn on a model call.
        """

        model = python_model()
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(
            request_for(PYTHON_MESSAGE, mode=ExtractionMode.FOREGROUND_DETERMINISTIC),
            adapter_context,
        )
        assert model.call_count == 0
        assert result.diagnostic.reason_codes == ("foreground_structure_not_deterministic",)


class TestModelDegradation:
    """A missing or broken local model must never break a chat turn."""

    def test_a_provider_failure_degrades_to_failed(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-05"""

        model = UnavailableModel("provider_unreachable")
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert result.status is ExtractionStatus.FAILED
        assert result.diagnostic.reason_codes == ("provider_unreachable",)
        assert result.decisions == ()

    def test_a_timeout_degrades_and_records_the_stage(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-06 — a slow model is diagnosable, not just "failed"."""

        model = UnavailableModel("provider_timeout", timeout=True)
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert result.status is ExtractionStatus.FAILED
        assert result.diagnostic.provider_timeout_stage == "response"

    def test_a_timeout_is_not_retried(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-06b — retrying a timeout doubles the wait for the same answer."""

        model = UnavailableModel("provider_timeout", timeout=True)
        extraction_coordinator_factory(model).process(request_for(PYTHON_MESSAGE), adapter_context)
        assert model.call_count == 1

    def test_malformed_output_is_retried_once(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-05b — a garbled response is worth exactly one more attempt.

        Local models drop a stray token often enough that one retry pays for
        itself; a second would only lengthen the turn.
        """

        model = scripted_model({PYTHON_MESSAGE: ["not json at all", "still not json"]})
        result = extraction_coordinator_factory(model).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert model.call_count == 2
        assert result.status is ExtractionStatus.FAILED

    def test_a_retry_that_succeeds_is_used(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-05d — the retry is real, not decorative."""

        good = model_output(
            assertions=[assertion(PYTHON_MESSAGE, "Python", domain_hint="software_development")]
        )
        model = scripted_model({PYTHON_MESSAGE: ["{oops", good]})
        result = extraction_coordinator_factory(model).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert model.call_count == 2
        assert result.status is ExtractionStatus.APPLIED

    def test_a_failure_still_emits_a_diagnostic(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXD-03 — the failing turns are the ones worth diagnosing."""

        coordinator = extraction_coordinator_factory(UnavailableModel())
        coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert coordinator.diagnostics.snapshot()

    def test_a_failure_never_raises_to_the_caller(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-05e — stated as its own test because it is the whole contract.

        Extraction runs after the user's reply has been produced.  If it raised,
        a memory-layer problem would surface as a broken chat turn.
        """

        coordinator = extraction_coordinator_factory(UnavailableModel("kaboom"))
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert result.status is ExtractionStatus.FAILED


class TestCandidateCaps:
    def test_automatic_candidates_are_capped(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-07 — one turn cannot write an unbounded number of memories.

        A model asked to find facts will find them.  Without a cap, one chatty
        paragraph could add a dozen rows the user never asked for and now has to
        find and delete.
        """

        message = "I use Python, Rust, Go, Elixir, Swift and Zig for work."
        model = scripted_model(
            {
                message: model_output(
                    assertions=[
                        assertion(
                            message,
                            language,
                            proposal_id=f"p{index}",
                            domain_hint="software_development",
                            confidence=0.99 - index / 100,
                        )
                        for index, language in enumerate(
                            ["Python", "Rust", "Go", "Elixir", "Swift", "Zig"]
                        )
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(model).process(
            request_for(message), adapter_context
        )
        assert result.model_summary.capped_count == 2
        assert len(result.decisions) == 4

    def test_the_cap_drops_the_least_confident_proposal(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-07b — which four survive is a decision, not an accident.

        Dropping by position would discard whatever the model happened to list
        last.  The least confident proposal is listed *first* here, so a
        position-based cap would keep it and this test would fail.
        """

        message = "I use Python, Rust, Go, Elixir, Swift and Zig for work."
        model = scripted_model(
            {
                message: model_output(
                    assertions=[
                        assertion(
                            message,
                            "Python",
                            proposal_id="low",
                            confidence=0.86,
                            domain_hint="software_development",
                        ),
                        *[
                            assertion(
                                message,
                                language,
                                proposal_id=f"high{index}",
                                confidence=0.99,
                                domain_hint="software_development",
                            )
                            for index, language in enumerate(["Rust", "Go", "Elixir", "Swift"])
                        ],
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(model).process(
            request_for(message), adapter_context
        )
        assert result.model_summary.capped_count == 1
        stored = {item.display_text for item in chat_adapter.list_active_memories(adapter_context)}
        assert stored == {"Rust", "Go", "Elixir", "Swift"}

    def test_the_cap_prefers_a_durable_fact_over_a_confident_temporary_one(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-07d — durability outranks confidence, and it should.

        A model can be very sure about something that will not be true next
        week.  When only four slots are available, a fact that stays true is
        worth more than a confident description of today.
        """

        message = "I use Python, Rust, Go, Elixir, Swift and Zig for work."
        model = scripted_model(
            {
                message: model_output(
                    assertions=[
                        assertion(
                            message,
                            "Python",
                            proposal_id="temporary-but-certain",
                            confidence=0.99,
                            durability="temporary",
                            domain_hint="software_development",
                        ),
                        *[
                            assertion(
                                message,
                                language,
                                proposal_id=f"durable{index}",
                                confidence=0.90,
                                domain_hint="software_development",
                            )
                            for index, language in enumerate(["Rust", "Go", "Elixir", "Swift"])
                        ],
                    ]
                )
            }
        )
        extraction_coordinator_factory(model).process(request_for(message), adapter_context)
        stored = {item.display_text for item in chat_adapter.list_active_memories(adapter_context)}
        assert stored == {"Rust", "Go", "Elixir", "Swift"}

    def test_the_cap_is_reported_not_silent(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-09 — dropping proposals silently would be undebuggable."""

        message = "I use Python, Rust, Go, Elixir, Swift and Zig for work."
        model = scripted_model(
            {
                message: model_output(
                    assertions=[
                        assertion(
                            message,
                            language,
                            proposal_id=f"p{index}",
                            domain_hint="software_development",
                        )
                        for index, language in enumerate(
                            ["Python", "Rust", "Go", "Elixir", "Swift", "Zig"]
                        )
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(model).process(
            request_for(message), adapter_context
        )
        assert "automatic_candidate_cap_applied" in result.diagnostic.reason_codes

    def test_an_automatic_request_cannot_ask_for_more_than_the_policy_allows(self) -> None:
        """EXC-07c — the cap is in the contract, not only in the coordinator.

        Enforcing it in the request model means no caller can raise its own
        limit, whatever it passes.
        """

        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="automatic_candidate_limit_exceeds_policy"):
            request_for(PYTHON_MESSAGE, maximum_candidates=10)

    def test_an_explicit_batch_may_ask_for_more(self) -> None:
        """EXC-08 — a user pasting a list of facts is a different situation.

        The automatic limit exists because the user did not ask for anything.
        An explicit batch is the user asking, so the bound is looser — but still
        bounded, at fifty.
        """

        request = request_for(
            PYTHON_MESSAGE, mode=ExtractionMode.EXPLICIT_BATCH, maximum_candidates=50
        )
        assert request.maximum_candidates == 50

    def test_no_mode_may_exceed_the_batch_ceiling(self) -> None:
        """EXC-08b"""

        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            request_for(PYTHON_MESSAGE, mode=ExtractionMode.EXPLICIT_BATCH, maximum_candidates=51)


class TestGrounding:
    """Model output is untrusted until it is tied to the user's own words."""

    def test_an_invented_quote_is_rejected(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-30 — the defence against a model making a fact up.

        The span cites text that is not in the message.  Nothing about the
        proposal is otherwise wrong — subject, type and confidence are all fine
        — which is exactly the point: grounding is what stops it.
        """

        model = RecordingModel(
            model_output(
                assertions=[
                    {
                        **assertion(PYTHON_MESSAGE, "Python"),
                        "source_spans": [
                            {
                                "message_id": "m1",
                                "start": 0,
                                "end": 7,
                                "quoted_text": "Haskell",
                            }
                        ],
                        "typed_value": "Haskell",
                        "display_hint": "Haskell",
                    }
                ]
            )
        )
        result = extraction_coordinator_factory(model).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert [item.accepted for item in result.grounding] == [False]
        assert result.status is not ExtractionStatus.APPLIED

    def test_a_span_citing_an_unknown_message_is_rejected(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-29 — a message id the request never authorized."""

        model = RecordingModel(
            model_output(
                assertions=[
                    {
                        **assertion(PYTHON_MESSAGE, "Python"),
                        "source_spans": [
                            {
                                "message_id": "some-other-message",
                                "start": 6,
                                "end": 12,
                                "quoted_text": "Python",
                            }
                        ],
                    }
                ]
            )
        )
        result = extraction_coordinator_factory(model).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert [item.accepted for item in result.grounding] == [False]

    def test_an_ungrounded_proposal_is_never_persisted(
        self, extraction_coordinator_factory, adapter_context, session_factory
    ) -> None:
        """EXC-11 — the check has to stop the write, not just flag it."""

        model = RecordingModel(
            model_output(
                assertions=[
                    {
                        **assertion(PYTHON_MESSAGE, "Python"),
                        "source_spans": [
                            {
                                "message_id": "m1",
                                "start": 0,
                                "end": 7,
                                "quoted_text": "Haskell",
                            }
                        ],
                    }
                ]
            )
        )
        coordinator = extraction_coordinator_factory(model)
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert not any(
            item.action in {CandidateAction.CREATE, CandidateAction.REPLACE}
            for item in result.decisions
        )

    def test_the_model_never_sees_the_owner_id(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-29b — the model input is a deliberately narrowed view.

        ``ModelExtractionInput`` exists so the model cannot echo back an owner
        id, memory id or lifecycle state it was shown and have it believed.
        """

        model = RecordingModel(model_output())
        extraction_coordinator_factory(model).process(request_for(PYTHON_MESSAGE), adapter_context)
        assert "owner_id" not in model.inputs[0].model_dump()


class TestSensitivity:
    def test_a_prohibited_turn_is_rejected_before_the_model(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-15 — prohibited content never reaches a model call at all.

        Classification happens on the raw message, before extraction begins, so
        the text is not sent anywhere — not even to a local process.
        """

        message = "My credit card number is 4111 1111 1111 1111."
        model = RecordingModel(model_output())
        result = extraction_coordinator_factory(model).process(
            request_for(message), adapter_context
        )
        assert result.status is ExtractionStatus.REJECTED
        assert model.call_count == 0
        assert result.diagnostic.reason_codes == ("prohibited_source_rejected_before_model",)

    def test_a_prohibited_turn_leaves_nothing_in_the_result(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-15b — the result travels; it must not carry the value.

        The extraction result is handed to the prompt builder and to
        diagnostics.  For prohibited content every field that could hold the
        text — spans, assertions, the raw output hash — is emptied.
        """

        message = "My credit card number is 4111 1111 1111 1111."
        result = extraction_coordinator_factory(RecordingModel(model_output())).process(
            request_for(message), adapter_context
        )
        override = result.current_turn_override
        assert override.positive_current_assertion is None
        assert override.redacted_current_assertion is None
        assert result.preparse.assertions == ()
        assert result.model_summary.raw_output_hash is None

    def test_a_sensitive_value_is_redacted_in_the_override(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-16 — the reply may acknowledge the fact, never restate it.

        The override is what the current turn's prompt is allowed to say.  For a
        sensitive memory it carries a placeholder, so the assistant can say "I
        have noted that" without echoing the value back into the transcript.
        """

        message = "Remember that my home address is 12 Baker Street."
        model = scripted_model(
            {
                message: model_output(
                    assertions=[
                        assertion(
                            message,
                            "12 Baker Street",
                            memory_type="identity",
                            domain_hint="contact",
                            sensitivity_hint="sensitive",
                        )
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(model).process(
            request_for(message, explicit_memory_intent=True), adapter_context
        )
        override = result.current_turn_override
        assert result.status is ExtractionStatus.APPLIED
        assert override.sensitivity is Sensitivity.SENSITIVE
        assert override.positive_current_assertion is None
        assert override.redacted_current_assertion == REDACTED_SENSITIVE_ASSERTION
        assert "Baker" not in str(override.model_dump())

    def test_a_sensitive_turn_drops_the_raw_output_hash(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-17b — the hash is of text that includes the sensitive value."""

        message = "Remember that my home address is 12 Baker Street."
        model = scripted_model(
            {
                message: model_output(
                    assertions=[
                        assertion(
                            message,
                            "12 Baker Street",
                            memory_type="identity",
                            domain_hint="contact",
                            sensitivity_hint="sensitive",
                        )
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(model).process(
            request_for(message, explicit_memory_intent=True), adapter_context
        )
        assert result.model_summary.raw_output_hash is None


class TestIdempotencyAndDuplicates:
    def test_the_same_turn_processed_twice_writes_once(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-24 — a retried turn must not double the user's memories.

        Post-turn extraction runs in a background thread.  A retry, a restart,
        or two workers racing the same message would otherwise each write their
        own copy.
        """

        coordinator = extraction_coordinator_factory(python_model())
        first = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        second = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert first.status is ExtractionStatus.APPLIED
        assert second.status is ExtractionStatus.APPLIED
        records = chat_adapter.list_active_memories(adapter_context)
        assert len(records) == 1

    def test_the_replay_is_recognised_as_a_replay(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-24b — and it is labelled, so it is visible in diagnostics.

        The second pass reports ``IDEMPOTENT_REPLAY``, not a second ``CREATE``
        that happened to collide.  The distinction matters: a replay means the
        earlier write is known to have succeeded, which is what makes it safe to
        report ``APPLIED`` without writing anything.
        """

        coordinator = extraction_coordinator_factory(python_model())
        coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        second = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert [item.action for item in second.decisions] == [CandidateAction.IDEMPOTENT_REPLAY]

    def test_the_same_fact_from_a_different_message_reconfirms(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-25 — saying something twice is not two memories.

        A different message id means idempotency cannot help: this has to be
        caught by the resolver recognising the same canonical fact.
        """

        coordinator = extraction_coordinator_factory(python_model())
        coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)

        second_context = replace(adapter_context, message_id="m2")
        model = scripted_model(
            {
                PYTHON_MESSAGE: model_output(
                    assertions=[
                        assertion(
                            PYTHON_MESSAGE,
                            "Python",
                            proposal_id="p2",
                            message_id="m2",
                            domain_hint="software_development",
                        )
                    ]
                )
            }
        )
        second = extraction_coordinator_factory(model).process(
            request_for(PYTHON_MESSAGE, message_id="m2"), second_context
        )
        assert second.status is ExtractionStatus.APPLIED
        assert len(chat_adapter.list_active_memories(adapter_context)) == 1

    def test_the_duplicate_finder_is_not_consulted_for_the_first_memory(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-14b — with nothing to compare against, no comparison happens.

        Worth pinning because the finder is the expensive part: it embeds text.
        Calling it on an empty store would be pure cost.
        """

        finder = StaticDuplicateFinder()
        coordinator = extraction_coordinator_factory(python_model(), duplicate_finder=finder)
        coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        assert finder.calls == []

    def test_a_failing_duplicate_finder_does_not_lose_the_memory(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-14c — the failure mode is chosen deliberately.

        If the embedding model is down, the choice is between losing the fact
        and possibly storing a near-duplicate.  A missed duplicate leaves the
        store exactly as it would have been without the check at all; a lost
        memory is a fact the user asked for and did not get.
        """

        finder = StaticDuplicateFinder(raises=RuntimeError("embedding model down"))
        coordinator = extraction_coordinator_factory(python_model(), duplicate_finder=finder)
        coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)

        second = scripted_model(
            {
                "I write Python at work.": model_output(
                    assertions=[
                        assertion(
                            "I write Python at work.",
                            "Python",
                            proposal_id="p9",
                            message_id="m2",
                            domain_hint="software_development",
                        )
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(second, duplicate_finder=finder).process(
            request_for("I write Python at work.", message_id="m2"),
            replace(adapter_context, message_id="m2"),
        )
        assert result.status is ExtractionStatus.APPLIED
        assert chat_adapter.list_active_memories(adapter_context)


class TestRetractions:
    def test_a_forget_removes_the_matching_memory(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-18 — the user's most important control over their own store.

        Note this runs with no model at all: "Forget that X" is handled by the
        deterministic preparser, so a user can always delete a memory even when
        the local model is unavailable.  Being unable to add a fact is an
        inconvenience; being unable to remove one is not acceptable.
        """

        extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert len(chat_adapter.list_active_memories(adapter_context)) == 1

        result = extraction_coordinator_factory(None).process(
            request_for("Forget that I use Python.", message_id="m2"),
            replace(adapter_context, message_id="m2"),
        )
        assert result.status is ExtractionStatus.APPLIED
        assert [item.action for item in result.decisions] == [CandidateAction.FORGET]
        assert [item.outcome for item in result.decisions] == ["forgotten"]
        assert chat_adapter.list_active_memories(adapter_context) == ()

    def test_a_superseded_fact_is_archived_rather_than_forgotten(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-18b — "I no longer X" and "forget X" are different requests.

        Both stop the fact being recalled.  Only the second is a deletion.
        "I no longer use Python" is a change in the world, and the record is
        archived so the history of what was true stays intact; "Forget that I
        use Python" is the user asking for it to be gone, which forgets and
        tombstones it.  Collapsing the two would either destroy history the user
        never asked to lose, or fail to honour a deletion request.
        """

        extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        message = "I no longer use Python."
        model = scripted_model(
            {
                message: model_output(
                    retractions=[
                        retraction(message, "Python", message_id="m2", old_value_hint="Python")
                    ]
                )
            }
        )
        result = extraction_coordinator_factory(model).process(
            request_for(message, message_id="m2"),
            replace(adapter_context, message_id="m2"),
        )
        assert [item.action for item in result.decisions] == [CandidateAction.RETRACT]
        assert [item.outcome for item in result.decisions] == ["archived"]
        assert chat_adapter.list_active_memories(adapter_context) == ()

    def test_a_retraction_suppresses_the_value_in_the_same_turn(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-18c — the reply must not repeat what was just removed.

        Recall for this turn was gathered before extraction ran, so the removed
        memory is still in the prompt context.  The override carries the id
        forward so it is dropped, and the user is not told about a fact they
        just deleted.
        """

        extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        result = extraction_coordinator_factory(None).process(
            request_for("Forget that I use Python.", message_id="m2"),
            replace(adapter_context, message_id="m2"),
        )
        assert result.current_turn_override.suppressed_memory_ids
        assert result.current_turn_override.contradiction_deterministic is True

    def test_an_unresolved_retraction_becomes_a_review_item(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-20 — deleting the wrong memory is worse than asking.

        With nothing matching what the user named, the only safe outcomes are
        "do nothing" or "ask".  Guessing at the closest record is not among them.
        """

        message = "Forget that I use a fineliner pen."
        result = extraction_coordinator_factory(None).process(request_for(message), adapter_context)
        assert result.status is ExtractionStatus.NEEDS_REVIEW
        assert any(item.review_required for item in result.decisions)

    def test_an_unresolved_retraction_deletes_nothing(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-20b — the property that actually matters."""

        coordinator = extraction_coordinator_factory(python_model())
        coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)

        extraction_coordinator_factory(None).process(
            request_for("Forget that I use a fineliner pen.", message_id="m2"),
            replace(adapter_context, message_id="m2"),
        )
        assert len(chat_adapter.list_active_memories(adapter_context)) == 1


class TestTheCurrentTurnOverride:
    """What this turn's reply is allowed to say about what just changed."""

    def test_the_override_is_bound_to_the_owner_and_message(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-22"""

        result = extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert result.current_turn_override.owner_id == OWNER_ID
        assert result.current_turn_override.source_message_id == "m1"

    def test_a_review_item_cannot_suppress_anything(self) -> None:
        """EXC-21 — ambiguity is not authority.

        Suppression hides an existing memory from the reply.  If a candidate is
        going to review precisely *because* it is unclear which memory it
        contradicts, letting it suppress would hide a memory on the strength of
        a guess the system already admitted it could not make.
        """

        from app.services.memory.extraction_contracts import CurrentTurnOverride

        builder = CurrentTurnOverrideBuilder(owner_id=OWNER_ID, source_message_id="m1")
        override = builder.build(
            status=ExtractionStatus.NEEDS_REVIEW,
            sensitivity=Sensitivity.NORMAL,
            positive_current_assertion="Python",
            confidence=0.5,
        )
        assert isinstance(override, CurrentTurnOverride)
        assert override.suppressed_memory_ids == ()
        assert override.review_required is True

    def test_a_no_action_turn_publishes_nothing(self) -> None:
        """EXC-21b — only applied or under-review turns say anything at all."""

        builder = CurrentTurnOverrideBuilder(owner_id=OWNER_ID, source_message_id="m1")
        override = builder.build(
            status=ExtractionStatus.NO_ACTION,
            sensitivity=Sensitivity.NORMAL,
            positive_current_assertion="Python",
            confidence=0.9,
        )
        assert override.positive_current_assertion is None

    def test_repeated_targets_are_de_duplicated(self) -> None:
        """EXC-23 — the same memory recorded twice is still one memory."""

        from uuid import uuid4

        from app.services.memory.contracts import CanonicalMemorySnapshot

        memory_id = uuid4()
        snapshot = _snapshot(memory_id)
        builder = CurrentTurnOverrideBuilder(owner_id=OWNER_ID, source_message_id="m1")
        builder.record_candidate_targets((snapshot, snapshot))
        builder.record_candidate_targets((snapshot,))
        override = builder.build(
            status=ExtractionStatus.APPLIED,
            sensitivity=Sensitivity.NORMAL,
            positive_current_assertion=None,
            confidence=0.9,
        )
        assert isinstance(snapshot, CanonicalMemorySnapshot)
        assert override.candidate_target_memory_ids == (memory_id,)

    def test_the_aliases_always_match_the_explicit_fields(self) -> None:
        """EXC-21c — two names for one thing, kept honest by the contract.

        ``contradicted_memory_ids`` exists for older callers.  The validator
        refuses any override where the alias and the real field disagree, so the
        compatibility name can never become a second, quietly different source
        of suppression authority.
        """

        from uuid import uuid4

        from pydantic import ValidationError

        from app.services.memory.extraction_contracts import CurrentTurnOverride

        with pytest.raises(ValidationError, match="must_alias_explicit_suppression"):
            CurrentTurnOverride(
                owner_id=OWNER_ID,
                source_message_id="m1",
                contradicted_memory_ids=(uuid4(),),
            )

    def test_a_prohibited_override_is_empty(self) -> None:
        """EXC-15c — nothing about a prohibited turn travels onward."""

        builder = CurrentTurnOverrideBuilder(owner_id=OWNER_ID, source_message_id="m1")
        builder.record_unresolved_hint("something")
        override = builder.build(
            status=ExtractionStatus.APPLIED,
            sensitivity=Sensitivity.PROHIBITED,
            positive_current_assertion="something",
            confidence=0.9,
        )
        assert override.unresolved_target_hints == ()
        assert override.positive_current_assertion is None
        assert override.redacted_current_assertion is None


class TestTimingAndStatus:
    def test_an_automatic_turn_is_timed_as_post_turn(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-26 — the automatic path never blocks the reply."""

        result = extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert result.timing == "after_turn"

    def test_an_explicit_instruction_is_timed_in_turn(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXC-26c — "remember this" has to take effect before the reply.

        A user who says "remember X" and is then told "I don't know X" in the
        same breath has been given a memory system that visibly does not work.
        """

        message = "Remember that the studio closes at 6pm."
        result = extraction_coordinator_factory(None).process(
            request_for(message, mode=ExtractionMode.FOREGROUND_DETERMINISTIC),
            adapter_context,
        )
        assert result.timing == "before_response"

    def test_the_status_reflects_what_was_persisted(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-27 — no optimistic reporting.

        ``APPLIED`` is claimed only when a write actually completed, so the
        status can be trusted by everything reading it downstream.
        """

        result = extraction_coordinator_factory(python_model()).process(
            request_for(PYTHON_MESSAGE), adapter_context
        )
        assert result.status is ExtractionStatus.APPLIED
        assert len(chat_adapter.list_active_memories(adapter_context)) == 1

    def test_a_rejected_turn_persisted_nothing(
        self, extraction_coordinator_factory, adapter_context, chat_adapter
    ) -> None:
        """EXC-27b — the same claim from the other direction."""

        model = RecordingModel(
            model_output(
                assertions=[
                    {
                        **assertion(PYTHON_MESSAGE, "Python"),
                        "source_spans": [
                            {
                                "message_id": "m1",
                                "start": 0,
                                "end": 7,
                                "quoted_text": "Haskell",
                            }
                        ],
                    }
                ]
            )
        )
        extraction_coordinator_factory(model).process(request_for(PYTHON_MESSAGE), adapter_context)
        assert chat_adapter.list_active_memories(adapter_context) == ()


class TestDiagnostics:
    def test_a_diagnostic_records_counts_and_outcome(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXD-01"""

        coordinator = extraction_coordinator_factory(python_model())
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        diagnostic = result.diagnostic
        assert diagnostic.parse_outcome == ExtractionStatus.APPLIED.value
        assert diagnostic.proposal_count == 1
        assert diagnostic.accepted_count == 1
        assert diagnostic.extractor_version

    def test_a_diagnostic_never_contains_the_message_text(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXD-02 — diagnostics are the most likely thing to be logged.

        Anything in here can end up in a log file, so it carries hashes, counts
        and codes — never the user's words or the values extracted from them.
        """

        coordinator = extraction_coordinator_factory(python_model())
        result = coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        serialized = result.diagnostic.model_dump_json()
        assert "Python" not in serialized
        assert "I use Python for work" not in serialized

    def test_diagnostics_accumulate_across_turns(
        self, extraction_coordinator_factory, adapter_context
    ) -> None:
        """EXD-01b — the sink is per-coordinator, so a turn can be traced."""

        coordinator = extraction_coordinator_factory(python_model())
        coordinator.process(request_for(PYTHON_MESSAGE), adapter_context)
        coordinator.process(request_for("What do you remember?", message_id="m1"), adapter_context)
        assert len(coordinator.diagnostics.snapshot()) == 2


def _snapshot(memory_id):
    """A minimal active-record snapshot, for the override-builder unit tests."""

    from app.services.memory.contracts import CanonicalMemorySnapshot, MemoryLifecycleState
    from app.services.memory.taxonomy import Cardinality, MemoryType

    return CanonicalMemorySnapshot(
        memory_id=memory_id,
        owner_id=OWNER_ID,
        subject_key="user",
        memory_type=MemoryType.KNOWLEDGE,
        domain_key="software_development",
        slot_key="knowledge:software_development:tool",
        cardinality=Cardinality.ADDITIVE,
        scope_type="global",
        scope_project_id=None,
        canonical_value="Python",
        display_text="Python",
        sensitivity=Sensitivity.NORMAL,
        status=MemoryLifecycleState.ACTIVE,
        revision=1,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

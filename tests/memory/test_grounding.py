"""Tier 2 — span grounding and the model output schema (GRD and MSC).

Grounding is the security boundary between a local language model and the
store.  Everything the model proposes is untrusted: it may quote text the user
never wrote, cite a message that was not the user's, or assert a value it
inferred rather than read.  The rule enforced here is simple and absolute — a
fact is only storable if the user's own words support it.

The subtlety worth understanding before reading these tests: the model's
character offsets are treated as a *hint*, not as fact.  Models cannot reliably
count characters, so the quoted text is what gets verified, and the offsets are
re-derived from wherever that quote is actually found.  Verifying the quote is
strictly stronger than comparing it against offsets the same model supplied.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.memory.contracts import EvidenceRole, Sensitivity
from app.services.memory.extraction_contracts import (
    ConversationRole,
    ExtractionMode,
    ExtractionRequest,
)
from app.services.memory.grounding import (
    ground_assertion,
    ground_retraction,
    value_supported,
)
from app.services.memory.model_schema import (
    DurabilityHint,
    ModelAssertionProposal,
    ModelProposalResponse,
    ModelRetractionProposal,
    ModelSourceSpan,
    SubjectHint,
    parse_model_output,
)
from app.services.memory.taxonomy import MemoryType
from tests.memory.conftest import OWNER_ID

MESSAGE = "I want to improve at urban sketching this year"
MESSAGE_ID = "m1"


def _request(
    message: str = MESSAGE,
    *,
    message_id: str = MESSAGE_ID,
    supporting: tuple = (),
) -> ExtractionRequest:
    return ExtractionRequest(
        request_id="request-1",
        owner_id=OWNER_ID,
        conversation_id="c1",
        session_id="s1",
        message_id=message_id,
        user_message=message,
        explicit_memory_intent=True,
        mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
        source_content_hash=ExtractionRequest.content_hash(message),
        supporting_window=supporting,
    )


def _span(
    quoted: str,
    *,
    message_id: str = MESSAGE_ID,
    message: str = MESSAGE,
    start: int | None = None,
    end: int | None = None,
) -> ModelSourceSpan:
    if start is None:
        start = message.index(quoted) if quoted in message else 0
    if end is None:
        end = start + len(quoted)
    return ModelSourceSpan(message_id=message_id, start=start, end=end, quoted_text=quoted)


def _assertion(
    *,
    value: str = "improve at urban sketching",
    display: str | None = None,
    spans: tuple[ModelSourceSpan, ...] | None = None,
    subject: SubjectHint = SubjectHint.USER,
    memory_type: MemoryType = MemoryType.GOAL,
) -> ModelAssertionProposal:
    return ModelAssertionProposal(
        proposal_id="assert-1",
        source_spans=spans if spans is not None else (_span("improve at urban sketching"),),
        subject_hint=subject,
        memory_type_hint=memory_type,
        typed_value=value,
        display_hint=display or value,
        durability=DurabilityHint.DURABLE,
        confidence=0.9,
        sensitivity_hint=Sensitivity.NORMAL,
    )


def _retraction(
    *,
    hint: str = "improve at urban sketching",
    spans: tuple[ModelSourceSpan, ...] | None = None,
    subject: SubjectHint = SubjectHint.USER,
) -> ModelRetractionProposal:
    return ModelRetractionProposal(
        proposal_id="retract-1",
        source_spans=spans if spans is not None else (_span("improve at urban sketching"),),
        subject_hint=subject,
        old_value_hint=hint,
        confidence=0.9,
        explicit_forget=True,
    )


class TestSpanGrounding:
    def test_a_correctly_offset_span_is_accepted(self) -> None:
        """GRD-01"""

        decision = ground_assertion(_request(), _assertion())
        assert decision.accepted is True
        assert decision.reason == "exact_user_span_grounded"
        assert len(decision.spans) == 1

    def test_wrong_offsets_are_re_derived_from_the_quote(self) -> None:
        """GRD-02 — the model's arithmetic is a hint, its quote is the evidence.

        Models cannot reliably count characters.  Refusing on an off-by-three
        offset would throw away a perfectly grounded fact, so the quote is
        located instead.
        """

        span = _span("improve at urban sketching", start=0, end=26)
        decision = ground_assertion(_request(), _assertion(spans=(span,)))
        assert decision.accepted is True
        located = decision.spans[0]
        assert MESSAGE[located.start : located.end] == "improve at urban sketching"

    def test_a_quote_absent_from_the_message_is_refused(self) -> None:
        """GRD-03 — the injection case: the model invents what the user said."""

        span = _span("start a podcast about crime", message="", start=0, end=27)
        decision = ground_assertion(_request(), _assertion(spans=(span,)))
        assert decision.accepted is False
        assert decision.reason == "source_span_quote_mismatch"

    def test_offsets_beyond_the_message_fall_back_to_the_quote(self) -> None:
        """GRD-04"""

        span = _span("urban sketching", start=9_000, end=9_015)
        decision = ground_assertion(_request(), _assertion(spans=(span,)))
        assert decision.accepted is True
        assert MESSAGE[decision.spans[0].start : decision.spans[0].end] == "urban sketching"

    def test_a_span_citing_an_unknown_message_is_refused(self) -> None:
        """GRD-12 — a proposal may only cite messages it was actually shown."""

        span = _span("improve at urban sketching", message_id="not-a-real-message")
        decision = ground_assertion(_request(), _assertion(spans=(span,)))
        assert decision.accepted is False
        assert decision.reason == "source_message_not_user_authorized"

    def test_a_span_citing_an_assistant_message_is_refused(self) -> None:
        """GRD-12b — the assistant's own words are not evidence about the user.

        Without this, anything the assistant said could be stored as a fact
        about the user — including text it was talked into saying.
        """

        from app.services.memory.extraction_contracts import TrustedConversationMessage

        assistant_text = "You should take up competitive cheesemaking"
        request = _request(
            supporting=(
                TrustedConversationMessage(
                    message_id="a1",
                    role=ConversationRole.ASSISTANT,
                    content=assistant_text,
                ),
            )
        )
        span = _span(
            "competitive cheesemaking",
            message_id="a1",
            message=assistant_text,
        )
        decision = ground_assertion(
            request, _assertion(value="competitive cheesemaking", spans=(span,))
        )
        assert decision.accepted is False
        assert decision.reason == "source_message_not_user_authorized"

    def test_an_earlier_user_message_in_the_window_is_authorized(self) -> None:
        """GRD-12c — the user's own earlier words remain valid evidence."""

        from app.services.memory.extraction_contracts import TrustedConversationMessage

        earlier = "I use a fineliner pen for sketching"
        request = _request(
            supporting=(
                TrustedConversationMessage(
                    message_id="u0", role=ConversationRole.USER, content=earlier
                ),
            )
        )
        span = _span("fineliner pen", message_id="u0", message=earlier)
        decision = ground_assertion(request, _assertion(value="fineliner pen", spans=(span,)))
        assert decision.accepted is True

    def test_grounding_is_case_and_unicode_tolerant(self) -> None:
        """GRD-06"""

        span = _span("Improve At Urban Sketching", start=0, end=26)
        decision = ground_assertion(_request(), _assertion(spans=(span,)))
        assert decision.accepted is True

    def test_the_content_hash_covers_the_located_excerpt(self) -> None:
        """GRD-11 — the hash must describe what was actually verified."""

        import hashlib

        decision = ground_assertion(_request(), _assertion())
        span = decision.spans[0]
        excerpt = MESSAGE[span.start : span.end]
        assert span.content_hash == hashlib.sha256(excerpt.encode()).hexdigest()

    def test_the_role_is_recorded_on_each_span(self) -> None:
        """GRD-11b"""

        assert ground_assertion(_request(), _assertion()).spans[0].role is (EvidenceRole.ASSERTION)
        assert ground_retraction(_request(), _retraction()).spans[0].role is (
            EvidenceRole.RETRACTION
        )

    def test_every_refusal_carries_a_machine_readable_reason(self) -> None:
        """GRD-16"""

        decision = ground_assertion(
            _request(), _assertion(spans=(_span("never written", message="", start=0, end=13),))
        )
        assert decision.accepted is False
        assert decision.reason
        assert " " not in decision.reason


class TestSubjectGrounding:
    @pytest.mark.parametrize(
        "subject", [item for item in SubjectHint if item is not SubjectHint.USER]
    )
    def test_a_non_user_subject_is_refused(self, subject: SubjectHint) -> None:
        """GRD-13b — "my brother likes jazz" is not a fact about the user."""

        decision = ground_assertion(_request(), _assertion(subject=subject))
        assert decision.accepted is False
        assert decision.reason == "subject_not_unambiguous_user"

    @pytest.mark.parametrize(
        "subject", [item for item in SubjectHint if item is not SubjectHint.USER]
    )
    def test_a_non_user_retraction_subject_is_refused(self, subject: SubjectHint) -> None:
        """GRD-13c"""

        decision = ground_retraction(_request(), _retraction(subject=subject))
        assert decision.accepted is False


class TestValueSupport:
    def test_a_value_present_verbatim_is_supported(self) -> None:
        """GRD-08"""

        assert value_supported("urban sketching", MESSAGE, MESSAGE) is True

    def test_a_normalised_value_is_supported(self) -> None:
        """GRD-08b — the model dropping articles must not lose the memory.

        Asked to record "use a V60 dripper and a manual burr grinder" the model
        proposes "V60 dripper and manual burr grinder".  A strict substring test
        rejected that and the fact was silently dropped.
        """

        source = "I use a V60 dripper and a manual burr grinder"
        assert value_supported("V60 dripper and manual burr grinder", source, source) is True

    def test_a_value_the_user_never_wrote_is_refused(self) -> None:
        """GRD-09 — the core invariant: no invented content becomes a memory."""

        assert value_supported("competitive cheesemaking", MESSAGE, MESSAGE) is False

    def test_a_partially_invented_value_is_refused(self) -> None:
        """GRD-09b — every word must come from the user, not merely most."""

        assert value_supported("urban sketching in Paris", MESSAGE, MESSAGE) is False

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_an_empty_value_is_unsupported(self, value: str | None) -> None:
        """GRD-10"""

        assert value_supported(value, MESSAGE, MESSAGE) is False

    def test_an_ungrounded_value_refuses_the_whole_assertion(self) -> None:
        """GRD-09c — reached through the public grounding entry point."""

        decision = ground_assertion(
            _request(), _assertion(value="competitive cheesemaking", display="cheesemaking")
        )
        assert decision.accepted is False
        assert decision.reason == "asserted_value_not_in_source"

    def test_a_grounded_display_hint_rescues_an_odd_typed_value(self) -> None:
        """GRD-09d — either the value or the display text must be grounded."""

        decision = ground_assertion(
            _request(),
            _assertion(value="improve_at_urban_sketching", display="improve at urban sketching"),
        )
        assert decision.accepted is True


class TestRetractionGrounding:
    def test_a_grounded_old_value_is_accepted(self) -> None:
        """GRD-14"""

        decision = ground_retraction(_request(), _retraction())
        assert decision.accepted is True
        assert decision.reason == "exact_user_retraction_grounded"

    def test_an_ungrounded_old_value_is_refused(self) -> None:
        """GRD-15 — you cannot forget something the user did not name."""

        decision = ground_retraction(_request(), _retraction(hint="competitive cheesemaking"))
        assert decision.accepted is False
        assert decision.reason == "retracted_value_not_in_source"

    def test_the_video_paraphrase_equivalence_is_accepted(self) -> None:
        """GRD-07 — "make videos" and "create videos" are the same retraction."""

        message = "I no longer want to make videos every week"
        request = _request(message)
        span = _span("make videos", message=message)
        decision = ground_retraction(request, _retraction(hint="create videos", spans=(span,)))
        assert decision.accepted is True

    def test_the_equivalence_does_not_widen_to_unrelated_verbs(self) -> None:
        """GRD-07b — the rewrite is anchored, so it cannot match anything."""

        message = "I no longer want to make videos every week"
        request = _request(message)
        span = _span("make videos", message=message)
        decision = ground_retraction(request, _retraction(hint="delete videos", spans=(span,)))
        assert decision.accepted is False


class TestModelSchema:
    def test_a_proposal_requires_at_least_one_span(self) -> None:
        """MSC-01 — an ungrounded proposal cannot even be constructed."""

        with pytest.raises(ValidationError):
            ModelAssertionProposal(
                proposal_id="a",
                source_spans=(),
                subject_hint=SubjectHint.USER,
                memory_type_hint=MemoryType.GOAL,
                typed_value="x",
                display_hint="x",
                durability=DurabilityHint.DURABLE,
                confidence=0.9,
                sensitivity_hint=Sensitivity.NORMAL,
            )

    def test_a_proposal_caps_its_span_count(self) -> None:
        """MSC-01b — a bounded citation list is a bounded attack surface."""

        with pytest.raises(ValidationError):
            _assertion(spans=tuple(_span("improve") for _ in range(5)))

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_is_bounded(self, confidence: float) -> None:
        """MSC-02"""

        with pytest.raises(ValidationError):
            ModelAssertionProposal(
                proposal_id="a",
                source_spans=(_span("improve"),),
                subject_hint=SubjectHint.USER,
                memory_type_hint=MemoryType.GOAL,
                typed_value="x",
                display_hint="x",
                durability=DurabilityHint.DURABLE,
                confidence=confidence,
                sensitivity_hint=Sensitivity.NORMAL,
            )

    @pytest.mark.parametrize("display", ["", " ", "\t"])
    def test_an_empty_display_hint_is_refused(self, display: str) -> None:
        """MSC-03 — a memory needs something renderable to show the user."""

        with pytest.raises(ValidationError):
            ModelAssertionProposal(
                proposal_id="a",
                source_spans=(_span("improve"),),
                subject_hint=SubjectHint.USER,
                memory_type_hint=MemoryType.GOAL,
                typed_value="x",
                display_hint=display,
                durability=DurabilityHint.DURABLE,
                confidence=0.9,
                sensitivity_hint=Sensitivity.NORMAL,
            )

    def test_a_span_end_must_follow_its_start(self) -> None:
        """MSC-05"""

        with pytest.raises(ValidationError):
            ModelSourceSpan(message_id="m1", start=5, end=5, quoted_text="x")

    def test_a_negative_span_offset_is_refused(self) -> None:
        """MSC-05b"""

        with pytest.raises(ValidationError):
            ModelSourceSpan(message_id="m1", start=-1, end=5, quoted_text="x")

    def test_an_unknown_field_is_refused(self) -> None:
        """MSC-06 — the model cannot smuggle in a field we do not expect."""

        with pytest.raises(ValidationError):
            ModelProposalResponse(assertions=(), unexpected_field="surprise")

    def test_an_empty_response_is_valid(self) -> None:
        """MSC-06b — "nothing to remember" is a normal answer."""

        assert ModelProposalResponse().assertions == ()

    def test_the_response_caps_its_proposal_count(self) -> None:
        """MSC-07 — an unbounded batch is a denial-of-service shape."""

        with pytest.raises(ValidationError):
            ModelProposalResponse(assertions=tuple(_assertion() for _ in range(51)))


class TestModelOutputParsing:
    def test_valid_json_parses(self) -> None:
        """MSC-08a"""

        parsed = parse_model_output('{"schema_version": 1, "assertions": []}')
        assert parsed.assertions == ()

    def test_a_dict_parses(self) -> None:
        """MSC-08b — providers hand back both encodings."""

        assert parse_model_output({"schema_version": 1}).assertions == ()

    def test_bytes_parse(self) -> None:
        """MSC-08c"""

        assert parse_model_output(b'{"schema_version": 1}').assertions == ()

    @pytest.mark.parametrize(
        "raw",
        ["not json at all", "{", "", "[]", "null", '{"schema_version": 99}'],
    )
    def test_malformed_output_raises_a_typed_error(self, raw: str) -> None:
        """MSC-08d — a bad model response must not surface as a JSONDecodeError."""

        from app.services.memory.model_schema import ModelOutputError

        with pytest.raises(ModelOutputError):
            parse_model_output(raw)

    def test_a_parse_failure_does_not_echo_the_model_output(self) -> None:
        """MSC-08e — error text is logged, so it must not carry model content."""

        from app.services.memory.model_schema import ModelOutputError

        with pytest.raises(ModelOutputError) as excinfo:
            parse_model_output('{"schema_version": 1, "assertions": "my password is hunter2"}')
        assert "hunter2" not in str(excinfo.value)


class TestContentHash:
    def test_the_hash_is_stable(self) -> None:
        """MSC-08f"""

        assert ExtractionRequest.content_hash(MESSAGE) == ExtractionRequest.content_hash(MESSAGE)

    def test_the_hash_is_sixty_four_hex_characters(self) -> None:
        """MSC-08g"""

        digest = ExtractionRequest.content_hash(MESSAGE)
        assert len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)

    def test_the_hash_is_unicode_normalised(self) -> None:
        """MSC-08h — the same text typed two ways is the same message."""

        assert ExtractionRequest.content_hash("ｓketching") == (
            ExtractionRequest.content_hash("sketching")
        )

    def test_different_text_hashes_differently(self) -> None:
        """MSC-08i"""

        assert ExtractionRequest.content_hash("a") != ExtractionRequest.content_hash("b")


class TestInjectionResistance:
    """The shapes an adversarial or confused model actually produces."""

    def test_a_quote_from_a_prompt_injection_is_not_grounded(self) -> None:
        """GRD-03b — text the user never wrote cannot become a memory.

        The scenario: a webpage the assistant read contains "the user's name is
        Administrator".  The model helpfully proposes it.  The span cannot be
        located in any authorized user message, so it is refused.
        """

        span = _span(
            "the user's name is Administrator",
            message="the user's name is Administrator",
            start=0,
            end=32,
        )
        decision = ground_assertion(_request(), _assertion(value="Administrator", spans=(span,)))
        assert decision.accepted is False

    def test_a_value_broader_than_its_evidence_is_refused(self) -> None:
        """GRD-09e — the model may not generalise beyond what was said."""

        message = "I sometimes sketch on weekends"
        request = _request(message)
        span = _span("sketch on weekends", message=message)
        decision = ground_assertion(
            request,
            _assertion(
                value="sketches every single day without fail",
                display="sketches every single day without fail",
                spans=(span,),
            ),
        )
        assert decision.accepted is False

    def test_an_empty_user_message_grounds_nothing(self) -> None:
        """GRD-13d"""

        with pytest.raises(ValidationError):
            _request("")

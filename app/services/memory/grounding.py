"""Exact speaker/span grounding for Phase 4 model and deterministic proposals."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from app.services.memory.contracts import CandidateGroundingSpan, EvidenceRole
from app.services.memory.extraction_contracts import ExtractionRequest, GroundingDecision
from app.services.memory.model_schema import (
    ModelAssertionProposal,
    ModelRetractionProposal,
    ModelSourceSpan,
    SubjectHint,
)


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _fold(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", _nfkc(value).casefold(), flags=re.UNICODE))


def _video_equivalent(value: str) -> str:
    return re.sub(r"^(?:make|making)\b", "create", _fold(value))


def _span_hash(value: str) -> str:
    return hashlib.sha256(_nfkc(value).encode("utf-8")).hexdigest()


def _locate_quote(source: str, quoted_text: str) -> tuple[int, int] | None:
    """Find the model's quoted evidence inside the authorized user message.

    Grounding must prove the quoted evidence genuinely came from the user, but a
    model cannot reliably count characters, so its offsets are treated as a hint
    rather than as fact.  Verifying the quote by locating it is strictly stronger
    than comparing it against offsets the same model supplied: the quote itself
    must appear in the user's message, or the proposal is refused.
    """

    normalized_source = _nfkc(source)
    normalized_quote = _nfkc(quoted_text).strip()
    if not normalized_quote:
        return None
    index = normalized_source.find(normalized_quote)
    if index < 0:
        lowered = normalized_source.casefold().find(normalized_quote.casefold())
        if lowered < 0:
            return None
        index = lowered
    return index, index + len(normalized_quote)


def _ground_spans(
    request: ExtractionRequest,
    spans: tuple[ModelSourceSpan, ...],
    *,
    role: EvidenceRole,
) -> tuple[tuple[CandidateGroundingSpan, ...], str | None]:
    authorized = request.authorized_user_messages()
    grounded: list[CandidateGroundingSpan] = []
    for span in spans:
        source = authorized.get(span.message_id)
        if source is None:
            return (), "source_message_not_user_authorized"
        start, end = span.start, span.end
        in_bounds = 0 <= start < end <= len(source)
        if not in_bounds or _nfkc(source[start:end]) != _nfkc(span.quoted_text):
            # The offsets disagree with the quote.  Trust the quote and re-derive
            # the offsets; refuse only when the quote is absent from the message.
            located = _locate_quote(source, span.quoted_text)
            if located is None:
                return (), "source_span_quote_mismatch"
            start, end = located
        excerpt = source[start:end]
        grounded.append(
            CandidateGroundingSpan(
                message_id=span.message_id,
                role=role,
                start=start,
                end=end,
                content_hash=_span_hash(excerpt),
            )
        )
    return tuple(grounded), None


def _value_supported(value: str | None, evidence: str, cited: str) -> bool:
    """Return whether the value appears in the cited span or its own message."""

    if not value:
        return False
    folded = _fold(value)
    if not folded:
        return False
    return folded in _fold(evidence) or folded in _fold(cited)


def ground_assertion(
    request: ExtractionRequest,
    proposal: ModelAssertionProposal,
) -> GroundingDecision:
    if proposal.subject_hint is not SubjectHint.USER:
        return GroundingDecision(
            proposal_id=proposal.proposal_id,
            accepted=False,
            reason="subject_not_unambiguous_user",
        )
    spans, error = _ground_spans(request, proposal.source_spans, role=EvidenceRole.ASSERTION)
    if error:
        return GroundingDecision(
            proposal_id=proposal.proposal_id,
            accepted=False,
            reason=error,
        )
    authorized = request.authorized_user_messages()
    # Use the grounded spans, not the model's raw offsets, so the value check
    # sees the same verified evidence the candidate will be built from.
    evidence = " ".join(authorized[item.message_id][item.start : item.end] for item in spans)
    value = (
        proposal.typed_value
        if isinstance(proposal.typed_value, str)
        else json.dumps(proposal.typed_value, ensure_ascii=False, sort_keys=True)
    )
    # The invariant is that the user actually said the asserted value, not that
    # the model picked the tightest span around it.  Widen the check to the
    # authorized user messages the spans already cite, so a fact is not dropped
    # merely because the quoted excerpt stopped just short of the value.
    cited = " ".join(dict.fromkeys(authorized[item.message_id] for item in spans))
    if not _value_supported(value, evidence, cited) and not _value_supported(
        proposal.display_hint, evidence, cited
    ):
        return GroundingDecision(
            proposal_id=proposal.proposal_id,
            accepted=False,
            reason="asserted_value_not_in_source",
        )
    return GroundingDecision(
        proposal_id=proposal.proposal_id,
        accepted=True,
        reason="exact_user_span_grounded",
        spans=spans,
    )


def ground_retraction(
    request: ExtractionRequest,
    proposal: ModelRetractionProposal,
) -> GroundingDecision:
    if proposal.subject_hint is not SubjectHint.USER:
        return GroundingDecision(
            proposal_id=proposal.proposal_id,
            accepted=False,
            reason="subject_not_unambiguous_user",
        )
    spans, error = _ground_spans(request, proposal.source_spans, role=EvidenceRole.RETRACTION)
    if error:
        return GroundingDecision(
            proposal_id=proposal.proposal_id,
            accepted=False,
            reason=error,
        )
    authorized = request.authorized_user_messages()
    evidence = " ".join(authorized[item.message_id][item.start : item.end] for item in spans)
    if _fold(proposal.old_value_hint) not in _fold(evidence) and _video_equivalent(
        proposal.old_value_hint
    ) not in _video_equivalent(evidence):
        return GroundingDecision(
            proposal_id=proposal.proposal_id,
            accepted=False,
            reason="retracted_value_not_in_source",
        )
    return GroundingDecision(
        proposal_id=proposal.proposal_id,
        accepted=True,
        reason="exact_user_retraction_grounded",
        spans=spans,
    )

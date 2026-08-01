"""Exact speaker/span grounding for Phase 4 model and deterministic proposals."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from app.services.memory_v2.contracts import CandidateGroundingSpan, EvidenceRole
from app.services.memory_v2.extraction_contracts import ExtractionRequest, GroundingDecision
from app.services.memory_v2.model_schema import (
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
        if span.start < 0 or span.end > len(source) or span.end <= span.start:
            return (), "source_span_offsets_invalid"
        excerpt = source[span.start : span.end]
        if _nfkc(excerpt) != _nfkc(span.quoted_text):
            return (), "source_span_quote_mismatch"
        grounded.append(
            CandidateGroundingSpan(
                message_id=span.message_id,
                role=role,
                start=span.start,
                end=span.end,
                content_hash=_span_hash(excerpt),
            )
        )
    return tuple(grounded), None


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
    evidence = " ".join(
        authorized[item.message_id][item.start : item.end] for item in proposal.source_spans
    )
    value = (
        proposal.typed_value
        if isinstance(proposal.typed_value, str)
        else json.dumps(proposal.typed_value, ensure_ascii=False, sort_keys=True)
    )
    if _fold(value) not in _fold(evidence) and _fold(proposal.display_hint) not in _fold(evidence):
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
    evidence = " ".join(
        authorized[item.message_id][item.start : item.end] for item in proposal.source_spans
    )
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

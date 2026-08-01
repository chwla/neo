"""Deterministic taxonomy normalization and owner-bound correction resolution."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.memory_v2.contracts import (
    CandidateIntent,
    CandidateTargetHints,
    CanonicalMemorySnapshot,
    EvidenceRole,
    EvidenceSpan,
    Sensitivity,
    ValidatedCandidateProposal,
)
from app.services.memory_v2.extraction_contracts import (
    ExtractionRequest,
    GroundingDecision,
    NormalizedExtractionCandidate,
)
from app.services.memory_v2.model_schema import (
    DurabilityHint,
    ModelAssertionProposal,
    ModelRetractionProposal,
)
from app.services.memory_v2.policy import classify_sensitivity
from app.services.memory_v2.taxonomy import (
    Cardinality,
    MemoryType,
    TaxonomyError,
    build_slot,
    resolve_domain,
)


class CorrectionResolutionKind(StrEnum):
    CREATE = "create"
    RECONFIRM = "reconfirm"
    REFINE = "refine"
    REPLACE = "replace"
    RETRACT = "retract"
    NEEDS_REVIEW = "needs_review"
    REJECT = "reject"


@dataclass(frozen=True)
class CandidateBuildResult:
    candidate: NormalizedExtractionCandidate | None
    reason: str
    review_required: bool = False


@dataclass(frozen=True)
class CorrectionResolution:
    kind: CorrectionResolutionKind
    reason: str
    targets: tuple[CanonicalMemorySnapshot, ...] = ()


def _fold(value: object) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    normalized = unicodedata.normalize("NFKC", value).casefold()
    words = re.findall(r"[\w]+", normalized, flags=re.UNICODE)
    if words and words[0] in {"make", "making"}:
        words[0] = "create"
    return " ".join(words)


def _candidate_uuid(request: ExtractionRequest, proposal_id: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"neo.memory.phase4:{request.owner_id}:{request.message_id}:{proposal_id}",
    )


def _sensitivity(
    request: ExtractionRequest,
    proposal: ModelAssertionProposal,
    evidence_text: str,
) -> tuple[Sensitivity | None, str | None]:
    detected = classify_sensitivity(
        f"{proposal.display_hint}\n{json.dumps(proposal.typed_value, ensure_ascii=False)}\n"
        f"{evidence_text}\n{request.user_message}"
    )
    order = {Sensitivity.NORMAL: 0, Sensitivity.SENSITIVE: 1, Sensitivity.PROHIBITED: 2}
    effective = max((detected, proposal.sensitivity_hint), key=order.__getitem__)
    if effective is Sensitivity.PROHIBITED:
        return None, "prohibited_content_not_persisted"
    if effective is Sensitivity.SENSITIVE and not request.explicit_memory_intent:
        return None, "sensitive_requires_explicit_request"
    return effective, None


def _domain_for(proposal: ModelAssertionProposal, source_text: str) -> str:
    topic_specific_video = bool(
        re.search(r"\b(?:video[- ]editing|video|youtube|instagram reels)\b", source_text, re.I)
    )
    global_style = bool(
        re.search(r"\b(?:always|all topics|every response|globally)\b", source_text, re.I)
    )
    explicit_domain = proposal.domain_hint
    if (
        proposal.memory_type_hint is MemoryType.PREFERENCE
        and explicit_domain == "global"
        and topic_specific_video
        and not global_style
    ):
        explicit_domain = "video_creation"
    return resolve_domain(source_text, explicit_domain=explicit_domain).key


def _slot_for(
    proposal: ModelAssertionProposal,
    *,
    domain: str,
    candidate_id: UUID,
    source_text: str,
) -> tuple[str, Cardinality]:
    memory_type = proposal.memory_type_hint
    if memory_type is MemoryType.GOAL:
        role = proposal.slot_hint
        if role in {"current_primary_goal", "primary_output"}:
            return_tuple = build_slot(memory_type, domain, goal_role=role)
        elif proposal.correction_group and not proposal.additive:
            return_tuple = build_slot(memory_type, domain, goal_role="current_primary_goal")
        else:
            return_tuple = build_slot(memory_type, domain, entity_id=candidate_id)
        return return_tuple.slot_key, return_tuple.cardinality
    if memory_type is MemoryType.PREFERENCE:
        dimension = proposal.slot_hint
        if not dimension and domain == "global" and re.search(r"concis", source_text, re.I):
            dimension = "verbosity"
        if not dimension and domain == "video_creation":
            dimension = "practice_advice_format"
        if not dimension and domain == "learning":
            dimension = "learning_format"
        definition = build_slot(memory_type, domain, preference_dimension=dimension)
        return definition.slot_key, definition.cardinality
    if memory_type is MemoryType.IDENTITY:
        identity_key = proposal.slot_hint or "profile_fact"
        definition = build_slot(memory_type, "global", identity_key=identity_key)
        return definition.slot_key, definition.cardinality
    definition = build_slot(memory_type, domain, entity_id=candidate_id)
    return definition.slot_key, definition.cardinality


def build_candidate(
    request: ExtractionRequest,
    proposal: ModelAssertionProposal,
    grounding: GroundingDecision,
    retractions: tuple[ModelRetractionProposal, ...],
) -> CandidateBuildResult:
    if not grounding.accepted:
        return CandidateBuildResult(None, grounding.reason)
    if proposal.durability is DurabilityHint.TEMPORARY:
        return CandidateBuildResult(None, "temporary_candidate_ignored")
    if proposal.durability is DurabilityHint.UNCERTAIN:
        return CandidateBuildResult(None, "uncertain_durability_requires_review", True)
    authorized = request.authorized_user_messages()
    evidence_text = " ".join(
        authorized[item.message_id][item.start : item.end] for item in proposal.source_spans
    )
    sensitivity, sensitivity_error = _sensitivity(request, proposal, evidence_text)
    if sensitivity_error:
        return CandidateBuildResult(None, sensitivity_error)
    candidate_id = _candidate_uuid(request, proposal.proposal_id)
    try:
        domain = _domain_for(proposal, evidence_text)
        slot_key, cardinality = _slot_for(
            proposal,
            domain=domain,
            candidate_id=candidate_id,
            source_text=evidence_text,
        )
    except (TaxonomyError, ValueError) as exc:
        return CandidateBuildResult(None, str(exc), True)
    group_retractions = tuple(
        item
        for item in retractions
        if proposal.correction_group and item.correction_group == proposal.correction_group
    )
    old_values = tuple(item.old_value_hint for item in group_retractions)
    evidence = tuple(
        EvidenceSpan(
            role=EvidenceRole.ASSERTION,
            text=authorized[item.message_id][item.start : item.end],
            start=item.start,
            end=item.end,
        )
        for item in proposal.source_spans
    ) + tuple(
        EvidenceSpan(
            role=EvidenceRole.RETRACTION,
            text=authorized[span.message_id][span.start : span.end],
            start=span.start,
            end=span.end,
        )
        for retraction in group_retractions
        for span in retraction.source_spans
    )
    candidate = ValidatedCandidateProposal(
        proposal_id=candidate_id,
        intent=CandidateIntent.REPLACE if group_retractions else CandidateIntent.ASSERT,
        subject_key="user",
        memory_type=proposal.memory_type_hint,
        domain_key=domain,
        slot_key=slot_key,
        cardinality=cardinality,
        canonical_value=proposal.typed_value,
        display_text=proposal.display_hint,
        sensitivity=sensitivity or Sensitivity.NORMAL,
        confidence=proposal.confidence,
        importance=7,
        explicit_user_request=request.explicit_memory_intent,
        target_hints=CandidateTargetHints(
            old_value_phrases=old_values,
            explicit_domain_change=proposal.explicit_domain_change,
            explicit_slot_change=proposal.explicit_slot_change,
        ),
        evidence=evidence,
    )
    return CandidateBuildResult(
        NormalizedExtractionCandidate(
            proposal=candidate,
            grounding_spans=grounding.spans,
            correction_group=proposal.correction_group,
            old_value_hints=old_values,
            explicit_type_change=proposal.explicit_type_change,
            explicit_domain_change=proposal.explicit_domain_change,
            explicit_slot_change=proposal.explicit_slot_change,
            expires_at=proposal.expires_at,
        ),
        "candidate_normalized",
    )


class DeterministicCorrectionResolver:
    def resolve(
        self,
        candidate: NormalizedExtractionCandidate,
        records: tuple[CanonicalMemorySnapshot, ...],
    ) -> CorrectionResolution:
        proposal = candidate.proposal
        duplicates = tuple(
            item
            for item in records
            if item.subject_key == proposal.subject_key
            and item.memory_type is proposal.memory_type
            and item.domain_key == proposal.domain_key
            and item.slot_key == proposal.slot_key
            and _fold(item.canonical_value) == _fold(proposal.canonical_value)
        )
        if duplicates:
            return CorrectionResolution(
                CorrectionResolutionKind.RECONFIRM,
                "exact_active_duplicate",
                (duplicates[0],),
            )

        if candidate.explicit_type_change:
            category_matches = tuple(
                item
                for item in records
                if item.subject_key == proposal.subject_key
                and _fold(item.canonical_value) == _fold(proposal.canonical_value)
                and item.memory_type is not proposal.memory_type
            )
            if len(category_matches) == 1:
                return CorrectionResolution(
                    CorrectionResolutionKind.REPLACE,
                    "grounded_category_change",
                    category_matches,
                )
            if len(category_matches) > 1:
                return CorrectionResolution(
                    CorrectionResolutionKind.NEEDS_REVIEW,
                    "grounded_category_target_ambiguous",
                    category_matches,
                )

        if candidate.old_value_hints:
            matches = tuple(
                item
                for item in records
                if item.subject_key == proposal.subject_key
                and any(
                    _fold(item.canonical_value) == _fold(old) for old in candidate.old_value_hints
                )
            )
            if matches:
                primary = matches[0]
                same_slot = tuple(
                    item
                    for item in records
                    if item.subject_key == primary.subject_key
                    and item.memory_type is primary.memory_type
                    and item.domain_key == primary.domain_key
                    and item.slot_key == primary.slot_key
                )
                return CorrectionResolution(
                    CorrectionResolutionKind.REPLACE,
                    "grounded_predecessor_match",
                    same_slot or matches,
                )
            return CorrectionResolution(
                CorrectionResolutionKind.NEEDS_REVIEW,
                "grounded_retraction_target_not_found",
            )

        occupied = tuple(
            item
            for item in records
            if item.subject_key == proposal.subject_key
            and item.memory_type is proposal.memory_type
            and item.domain_key == proposal.domain_key
            and item.slot_key == proposal.slot_key
        )
        if proposal.cardinality is Cardinality.EXCLUSIVE and occupied:
            return CorrectionResolution(
                CorrectionResolutionKind.NEEDS_REVIEW,
                "unlinked_exclusive_slot_conflict",
                occupied,
            )
        return CorrectionResolution(CorrectionResolutionKind.CREATE, "independent_assertion")

    def resolve_retraction(
        self,
        proposal: ModelRetractionProposal,
        records: tuple[CanonicalMemorySnapshot, ...],
    ) -> CorrectionResolution:
        matches = tuple(
            item
            for item in records
            if _fold(item.canonical_value) == _fold(proposal.old_value_hint)
            and (proposal.memory_type_hint is None or item.memory_type is proposal.memory_type_hint)
            and (proposal.domain_hint is None or item.domain_key == proposal.domain_hint)
            and (proposal.slot_hint is None or item.slot_key.endswith(f":{proposal.slot_hint}"))
        )
        if len(matches) == 1:
            return CorrectionResolution(
                CorrectionResolutionKind.RETRACT,
                "exact_retraction_target",
                matches,
            )
        return CorrectionResolution(
            CorrectionResolutionKind.NEEDS_REVIEW,
            "retraction_target_ambiguous_or_missing",
            matches,
        )

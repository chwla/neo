"""Deterministic taxonomy normalization and owner-bound correction resolution."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.memory.contracts import (
    CandidateIntent,
    CandidateTargetHints,
    CanonicalMemorySnapshot,
    EvidenceRole,
    EvidenceSpan,
    Sensitivity,
    ValidatedCandidateProposal,
)
from app.services.memory.extraction_contracts import (
    ExtractionRequest,
    GroundingDecision,
    NormalizedExtractionCandidate,
)
from app.services.memory.grounding import value_supported
from app.services.memory.model_schema import (
    DurabilityHint,
    ModelAssertionProposal,
    ModelRetractionProposal,
)
from app.services.memory.policy import classify_sensitivity
from app.services.memory.taxonomy import (
    Cardinality,
    InitialDomain,
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
    # Underscores separate words rather than joining them.  A model asked for a
    # canonical value answers "improve at urban sketching" on one turn and
    # "improve_at_urban_sketching" on the next; with ``\w`` the slug folds to a
    # single opaque token, so the two never compare equal and the same fact is
    # stored twice and can never be found again by a delete.
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    if words and words[0] in {"make", "making"}:
        words[0] = "create"
    # Global-style assertions are often stored as an imperative ("answer me
    # with detailed explanations") but corrected as a preference ("I don't
    # prefer detailed explanations").  This framing is not the value itself
    # and must not prevent an explicitly grounded replacement from finding its
    # predecessor.
    while words[:2] == ["always", "answer"]:
        words.pop(0)
    if words[:2] == ["answer", "me"]:
        del words[:2]
    if words[:1] == ["with"]:
        words.pop(0)
    return " ".join(words)


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Return whether the folded needle appears in the haystack on word bounds."""

    if not needle or not haystack:
        return False
    return f" {needle} " in f" {haystack} "


def _candidate_uuid(request: ExtractionRequest, proposal_id: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"neo.memory.extraction:{request.owner_id}:{request.message_id}:{proposal_id}",
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
    try:
        return resolve_domain(source_text, explicit_domain=explicit_domain).key
    except TaxonomyError:
        pass
    # The model named a domain that is neither a known alias nor a phrase present
    # in the message (for example "demographics" for "i am 21 years old").  A
    # domain is only an organising facet, so fall back to what the message itself
    # supports rather than discarding a durable fact over the label.  The value
    # still has to be grounded in the user's own words; that check is enforced
    # separately by ``ground_assertion``.
    try:
        return resolve_domain(source_text).key
    except TaxonomyError:
        return InitialDomain.GLOBAL.value


_SLOT_TOKEN_ALPHABET = "abcdefghijklmnop"


def _entity_token(value: UUID) -> str:
    """Encode an identifier as letters only, for embedding inside a slot key.

    A raw hex identifier occasionally contains a Luhn-valid run of 13 or more
    digits, which the repository's content guard reads as a card number and
    refuses to persist.  Mapping each nibble onto a letter keeps the token
    unique and stable while making that false positive impossible.
    """

    return "".join(_SLOT_TOKEN_ALPHABET[int(char, 16)] for char in value.hex)


def _value_entity_id(
    proposal: ModelAssertionProposal,
    *,
    domain: str,
    candidate_id: UUID,
) -> UUID:
    """Derive an additive slot's entity component from the remembered value.

    An additive slot needs a component saying *which* item it holds.  That
    component used to be the candidate id, which mixes in the message id, so the
    same fact restated on a later turn produced a different slot, therefore a
    different canonical fingerprint, and therefore a brand new record instead of
    a reconfirmation of the existing one.  Every re-extraction of an earlier
    message — including the ones triggered by merely asking what is remembered —
    appended another copy, and a delete could then never resolve to a single
    target.

    Deriving the entity from the folded value makes the slot stable across
    turns: one fact, one slot.  A value that folds to nothing keeps the
    candidate id, so unrelated empty values cannot collapse onto one slot.
    """

    material = _fold(proposal.typed_value) or _fold(proposal.display_hint)
    if not material:
        return candidate_id
    return uuid5(
        NAMESPACE_URL,
        f"neo.memory.entity:{proposal.memory_type_hint.value}:{domain}:{material}",
    )


def _slot_for(
    proposal: ModelAssertionProposal,
    *,
    domain: str,
    candidate_id: UUID,
    source_text: str,
) -> tuple[str, Cardinality]:
    memory_type = proposal.memory_type_hint
    entity_id = _value_entity_id(proposal, domain=domain, candidate_id=candidate_id)
    if memory_type is MemoryType.GOAL:
        role = proposal.slot_hint
        if role in {"current_primary_goal", "primary_output"}:
            return_tuple = build_slot(memory_type, domain, goal_role=role)
        elif proposal.correction_group and not proposal.additive:
            return_tuple = build_slot(memory_type, domain, goal_role="current_primary_goal")
        else:
            return_tuple = build_slot(memory_type, domain, entity_id=entity_id)
        return return_tuple.slot_key, return_tuple.cardinality
    if memory_type is MemoryType.PREFERENCE:
        dimension = proposal.slot_hint
        if not dimension and domain == "global" and re.search(r"concis", source_text, re.I):
            dimension = "verbosity"
        if not dimension and domain == "video_creation":
            dimension = "practice_advice_format"
        if not dimension and domain == "learning":
            dimension = "learning_format"
        if not dimension:
            # Preference slots are single-valued and must name a dimension, but
            # only these few dimensions were ever recognised.  Every other
            # preference failed slot construction with
            # "preference_dimension_required" and was dropped into review
            # instead of being stored.  Fall back to a dimension derived from
            # this candidate so an unrecognised preference is still saved, and
            # so two different preferences do not overwrite one another by
            # sharing a single generic slot.  The dimension is derived from the
            # value rather than the message, so restating the same preference
            # reconfirms the existing record instead of adding another copy.
            dimension = f"item_{_entity_token(entity_id)}"
        definition = build_slot(memory_type, domain, preference_dimension=dimension)
        return definition.slot_key, definition.cardinality
    if memory_type is MemoryType.IDENTITY:
        identity_key = proposal.slot_hint or "profile_fact"
        definition = build_slot(memory_type, "global", identity_key=identity_key)
        return definition.slot_key, definition.cardinality
    definition = build_slot(memory_type, domain, entity_id=entity_id)
    return definition.slot_key, definition.cardinality


def _display_text_for(
    request: ExtractionRequest,
    proposal: ModelAssertionProposal,
    evidence_text: str,
) -> str:
    """Return display text the user actually said, preferring the model's phrasing.

    A proposal is accepted when either its value or its display hint is grounded,
    but only the display hint was ever stored — so a model that grounded the
    value and then summarised it in its own words ("preference for structured
    sketching practice sessions", where the user never wrote "structured") had
    that summary saved.  The invention is what the memory list shows the user and
    what the vector index embeds, so two recordings of one preference compared at
    0.62 instead of the 0.98 their real values share, and the duplicate stood.

    Fall back to the grounded value whenever the hint is not itself supported.
    """

    authorized = request.authorized_user_messages()
    cited = " ".join(
        dict.fromkeys(
            authorized[item.message_id]
            for item in proposal.source_spans
            if item.message_id in authorized
        )
    )
    if value_supported(proposal.display_hint, evidence_text, cited):
        return proposal.display_hint
    if isinstance(proposal.typed_value, str) and proposal.typed_value.strip():
        return proposal.typed_value
    return proposal.display_hint


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
        scope_type=("project" if request.active_project_id and proposal.memory_type_hint is MemoryType.PROJECT else "global"),
        scope_project_id=(request.active_project_id if request.active_project_id and proposal.memory_type_hint is MemoryType.PROJECT else None),
        domain_key=domain,
        slot_key=slot_key,
        cardinality=cardinality,
        canonical_value=proposal.typed_value,
        display_text=_display_text_for(request, proposal, evidence_text),
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

        # The same fact can arrive under a different slot than the one it was
        # first stored in: a model names an unrecognised preference dimension
        # ("urban_sketching_practice" one turn, "practice_session_format" the
        # next), so a slot comparison alone sees two unrelated memories and
        # stores the identical sentence twice.  Meaning, not slot, decides
        # whether this is already remembered, and the existing record stays
        # authoritative so a delete still has one target to find.
        restated = tuple(
            item
            for item in records
            if item.subject_key == proposal.subject_key
            and item.memory_type is proposal.memory_type
            and item.domain_key == proposal.domain_key
            and item.scope_type == proposal.scope_type
            and item.scope_project_id == proposal.scope_project_id
            and _fold(item.canonical_value) == _fold(proposal.canonical_value)
        )
        if restated:
            return CorrectionResolution(
                CorrectionResolutionKind.RECONFIRM,
                "restated_active_value",
                (restated[0],),
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
        hint = _fold(proposal.old_value_hint)
        eligible = tuple(
            item
            for item in records
            if (proposal.memory_type_hint is None or item.memory_type is proposal.memory_type_hint)
            and (proposal.domain_hint is None or item.domain_key == proposal.domain_hint)
            and (proposal.slot_hint is None or item.slot_key.endswith(f":{proposal.slot_hint}"))
        )
        # A user names the value inside a sentence — "forget that I use a
        # fineliner pen and a pocket watercolor set for urban sketching" — while
        # the stored value is the bare phrase.  Requiring the two to be equal
        # meant an explicit delete resolved to no target at all and silently
        # became a review item, so accept a stored value the user's own words
        # fully contain.  Containment only ever runs in this direction: the user
        # must name at least the whole stored value, so naming one word of a
        # remembered phrase can never delete it.
        exact = tuple(item for item in eligible if _fold(item.canonical_value) == hint)
        matches = exact or tuple(
            item for item in eligible if _contains_phrase(hint, _fold(item.canonical_value))
        )
        if not matches:
            return CorrectionResolution(
                CorrectionResolutionKind.NEEDS_REVIEW,
                "retraction_target_ambiguous_or_missing",
            )
        # Every match is a value the user's own sentence spelled out, so all of
        # them were named and all of them go.  "Forget that I use a fineliner pen
        # and a pocket watercolor set" must clear both when they were stored as
        # two facts, and must clear every copy when one fact was stored twice.
        #
        # Only maximal matches are kept: when one stored value sits inside
        # another that also matched, the shorter one is a fragment of the longer
        # rather than a separate fact the user named, and retracting it would
        # reach past what was asked.
        maximal = tuple(
            item
            for item in matches
            if not any(
                other is not item
                and _contains_phrase(
                    _fold(other.canonical_value), _fold(item.canonical_value)
                )
                for other in matches
            )
        )
        return CorrectionResolution(
            CorrectionResolutionKind.RETRACT,
            "exact_retraction_target",
            maximal or matches,
        )

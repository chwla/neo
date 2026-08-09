"""Deterministic, versioned normalization for memory mutation inputs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue

from app.core.identifiers import canonical_uuid
from app.services.memory.contracts import (
    CONTRACT_VERSION,
    POLICY_VERSION,
    TAXONOMY_VERSION,
    CandidateIntent,
    EvidenceRole,
    MemoryCommandBase,
    MemorySource,
    Sensitivity,
    ValidatedCandidateProposal,
)
from app.services.memory.crypto import KeyedFingerprintProvider
from app.services.memory.taxonomy import (
    EXCLUSIVE_GOAL_ROLES,
    Cardinality,
    MemoryIdentity,
    MemoryType,
    inherit_predecessor_identity,
    validate_domain_key,
)

NORMALIZATION_VERSION = "neo.memory.normalization.v1"
SUPPORTED_VALUE_SCHEMA_VERSIONS = frozenset({1})
ALLOWED_METADATA_KEYS = frozenset({"tags", "user_label", "review_note"})
MAX_METADATA_JSON_BYTES = 4_096
MAX_EVIDENCE_TEXT_CHARS = 1_000


class MemoryNormalizationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NormalizedEvidence:
    role: EvidenceRole
    text: str
    start: int | None
    end: int | None


@dataclass(frozen=True)
class NormalizedSource:
    kind: str
    source_id: str | None
    conversation_id: str | None
    session_id: str | None
    message_id: str | None
    observed_at: datetime | None
    evidence: tuple[NormalizedEvidence, ...]


@dataclass(frozen=True)
class NormalizedCandidate:
    candidate_id: str
    owner_id: str
    subject_key: str
    memory_type: MemoryType
    scope_type: str
    scope_project_id: str | None
    domain_key: str
    slot_key: str
    cardinality: Cardinality
    sensitivity: Sensitivity
    intent: CandidateIntent
    canonical_value: JsonValue
    canonical_json: bytes
    display_text: str
    canonical_fingerprint: str
    confidence: float
    importance: int
    explicit_user_request: bool
    value_schema_version: int
    target_ids: tuple[str, ...]
    target_hints: dict[str, Any]
    evidence: tuple[NormalizedEvidence, ...]
    normalization_version: str = NORMALIZATION_VERSION


@dataclass(frozen=True)
class NormalizedRecordValue:
    canonical_value: JsonValue
    canonical_json: bytes
    display_text: str
    sensitivity: Sensitivity
    canonical_fingerprint: str


def normalize_text(value: str, *, code: str, limit: int | None = None) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise MemoryNormalizationError(code)
    if limit is not None and len(normalized) > limit:
        raise MemoryNormalizationError(f"{code}_too_long")
    return normalized


def _normalize_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return normalize_text(value, code="canonical_value_required")
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        return {
            normalize_text(str(key), code="canonical_object_key_required", limit=200): (
                _normalize_json(item)
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if value is None:
        raise MemoryNormalizationError("positive_canonical_value_required")
    return value


def canonical_json_bytes(value: JsonValue) -> bytes:
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint_material(
    *,
    subject_key: str,
    memory_type: MemoryType,
    scope_type: str,
    scope_project_id: str | None,
    domain_key: str,
    slot_key: str,
    canonical_value: JsonValue,
) -> bytes:
    normalized_value = _normalize_json(canonical_value)

    def identity_fold(value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return value.casefold()
        if isinstance(value, list):
            return [identity_fold(item) for item in value]
        if isinstance(value, dict):
            return {key: identity_fold(item) for key, item in value.items()}
        return value

    material = {
        "domain_key": domain_key,
        "memory_type": memory_type.value,
        "scope_type": scope_type,
        "scope_project_id": scope_project_id,
        "slot_key": slot_key,
        "subject_key": subject_key.casefold(),
        "value": identity_fold(normalized_value),
        "version": NORMALIZATION_VERSION,
    }
    return json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_fingerprint(
    *,
    owner_id: str,
    subject_key: str,
    memory_type: MemoryType,
    domain_key: str,
    slot_key: str,
    canonical_value: JsonValue,
    sensitivity: Sensitivity,
    keyed_provider: KeyedFingerprintProvider,
    scope_type: str = "global",
    scope_project_id: str | None = None,
) -> str:
    material = _fingerprint_material(
        subject_key=subject_key,
        memory_type=memory_type,
        scope_type=scope_type,
        scope_project_id=scope_project_id,
        domain_key=domain_key,
        slot_key=slot_key,
        canonical_value=canonical_value,
    )
    if sensitivity is Sensitivity.SENSITIVE:
        keyed = keyed_provider.fingerprint(material, owner_id=owner_id)
        return f"keyed:{keyed.key_version}:{keyed.digest}"
    if sensitivity is Sensitivity.PROHIBITED:
        raise MemoryNormalizationError("prohibited_content_not_persisted")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _validate_positive_text(value: str) -> str:
    normalized = normalize_text(value, code="positive_display_text_required", limit=4_000)
    check = re.sub(r"\bnot only\b", "", normalized, flags=re.IGNORECASE)
    if re.search(
        r"\b(?:no longer|do not want|don't want|did not want|used to|stop(?:ped)? wanting)\b",
        check,
        flags=re.IGNORECASE,
    ):
        raise MemoryNormalizationError("positive_current_fact_required")
    return normalized


def _validate_slot(identity: MemoryIdentity) -> None:
    parts = identity.slot_key.split(":")
    if len(parts) < 3 or parts[0] != identity.memory_type.value or parts[1] != identity.domain_key:
        raise MemoryNormalizationError("invalid_slot_identity")

    if identity.memory_type is MemoryType.GOAL:
        if identity.cardinality is Cardinality.EXCLUSIVE:
            if len(parts) != 3 or parts[2] not in EXCLUSIVE_GOAL_ROLES:
                raise MemoryNormalizationError("invalid_exclusive_goal_slot")
        elif len(parts) != 4 or parts[2] != "independent":
            raise MemoryNormalizationError("invalid_additive_goal_slot")
        else:
            canonical_uuid(parts[3])
        return

    if identity.memory_type is MemoryType.PREFERENCE:
        if identity.cardinality is not Cardinality.EXCLUSIVE or len(parts) != 3:
            raise MemoryNormalizationError("invalid_preference_slot")
        return

    if identity.memory_type is MemoryType.IDENTITY:
        if (
            identity.domain_key != "global"
            or identity.cardinality is not Cardinality.EXCLUSIVE
            or len(parts) != 3
        ):
            raise MemoryNormalizationError("invalid_identity_slot")
        return

    if identity.cardinality is Cardinality.ADDITIVE:
        if len(parts) != 4 or parts[2] != "item":
            raise MemoryNormalizationError("invalid_additive_slot")
        canonical_uuid(parts[3])
        return

    if (
        identity.memory_type not in {MemoryType.EDUCATION, MemoryType.EMPLOYMENT}
        or len(parts) != 3
        or parts[2] != "current_status"
    ):
        raise MemoryNormalizationError("invalid_exclusive_slot")


def normalize_source(source: MemorySource) -> NormalizedSource:
    evidence = tuple(
        NormalizedEvidence(
            role=item.role,
            text=normalize_text(
                item.text,
                code="source_evidence_required",
                limit=MAX_EVIDENCE_TEXT_CHARS,
            ),
            start=item.start,
            end=item.end,
        )
        for item in source.evidence
    )
    observed = source.observed_at
    if observed is not None and observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return NormalizedSource(
        kind=source.kind.value,
        source_id=source.source_id,
        conversation_id=source.conversation_id,
        session_id=source.session_id,
        message_id=source.message_id,
        observed_at=observed,
        evidence=evidence,
    )


def normalize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    material = dict(metadata or {})
    unknown = set(material) - ALLOWED_METADATA_KEYS
    if unknown:
        raise MemoryNormalizationError("metadata_key_not_allowed")
    encoded = json.dumps(material, sort_keys=True, default=str, separators=(",", ":")).encode()
    if len(encoded) > MAX_METADATA_JSON_BYTES:
        raise MemoryNormalizationError("metadata_too_large")
    return json.loads(encoded)


def validate_command_versions(command: MemoryCommandBase) -> None:
    if command.contract_version != CONTRACT_VERSION:
        raise MemoryNormalizationError("unsupported_contract_version")
    if command.policy_version != POLICY_VERSION:
        raise MemoryNormalizationError("unsupported_policy_version")
    if command.taxonomy_version != TAXONOMY_VERSION:
        raise MemoryNormalizationError("unsupported_taxonomy_version")


def normalize_candidate(
    proposal: ValidatedCandidateProposal,
    *,
    owner_id: str,
    keyed_provider: KeyedFingerprintProvider,
    predecessor: MemoryIdentity | None = None,
) -> NormalizedCandidate:
    owner = canonical_uuid(owner_id)
    if proposal.value_schema_version not in SUPPORTED_VALUE_SCHEMA_VERSIONS:
        raise MemoryNormalizationError("unsupported_value_schema_version")
    if proposal.scope_type not in {"global", "project"} or (
        proposal.scope_type == "global" and proposal.scope_project_id is not None
    ) or (proposal.scope_type == "project" and not proposal.scope_project_id):
        raise MemoryNormalizationError("invalid_memory_scope")
    try:
        proposed_domain = validate_domain_key(proposal.domain_key)
    except ValueError as exc:
        raise MemoryNormalizationError("invalid_domain_key") from exc
    if proposed_domain.startswith("topic."):
        grounded_phrase = proposed_domain.removeprefix("topic.").replace("_", " ")
        grounding_material = " ".join(
            (
                str(proposal.canonical_value),
                proposal.display_text,
                *(item.text for item in proposal.evidence),
            )
        ).casefold()
        if grounded_phrase.casefold() not in grounding_material:
            raise MemoryNormalizationError("unknown_domain_must_be_grounded")

    if predecessor is not None and proposal.memory_type is predecessor.memory_type:
        try:
            identity = inherit_predecessor_identity(
                predecessor,
                proposed_domain=(
                    proposed_domain if proposal.target_hints.explicit_domain_change else None
                ),
                proposed_slot=(
                    proposal.slot_key if proposal.target_hints.explicit_slot_change else None
                ),
                explicit_domain_change=proposal.target_hints.explicit_domain_change,
                explicit_slot_change=proposal.target_hints.explicit_slot_change,
            )
        except ValueError as exc:
            raise MemoryNormalizationError(str(exc)) from exc
    else:
        identity = MemoryIdentity(
            memory_type=proposal.memory_type,
            domain_key=proposed_domain,
            slot_key=proposal.slot_key,
            cardinality=proposal.cardinality,
        )

    _validate_slot(identity)
    canonical_value = _normalize_json(proposal.canonical_value)
    display_text = _validate_positive_text(proposal.display_text)
    fingerprint = canonical_fingerprint(
        owner_id=owner,
        subject_key=normalize_text(proposal.subject_key, code="subject_required", limit=160),
        memory_type=identity.memory_type,
        scope_type=proposal.scope_type,
        scope_project_id=proposal.scope_project_id,
        domain_key=identity.domain_key,
        slot_key=identity.slot_key,
        canonical_value=canonical_value,
        sensitivity=proposal.sensitivity,
        keyed_provider=keyed_provider,
    )
    target_ids = tuple(
        canonical_uuid(str(identifier)) for identifier in proposal.target_hints.target_memory_ids
    )
    evidence = tuple(
        NormalizedEvidence(
            role=item.role,
            text=normalize_text(
                item.text,
                code="candidate_evidence_required",
                limit=MAX_EVIDENCE_TEXT_CHARS,
            ),
            start=item.start,
            end=item.end,
        )
        for item in proposal.evidence
    )
    return NormalizedCandidate(
        candidate_id=str(proposal.proposal_id),
        owner_id=owner,
        subject_key=normalize_text(proposal.subject_key, code="subject_required", limit=160),
        memory_type=identity.memory_type,
        scope_type=proposal.scope_type,
        scope_project_id=proposal.scope_project_id,
        domain_key=identity.domain_key,
        slot_key=identity.slot_key,
        cardinality=identity.cardinality,
        sensitivity=proposal.sensitivity,
        intent=proposal.intent,
        canonical_value=canonical_value,
        canonical_json=canonical_json_bytes(canonical_value),
        display_text=display_text,
        canonical_fingerprint=fingerprint,
        confidence=proposal.confidence,
        importance=proposal.importance,
        explicit_user_request=proposal.explicit_user_request,
        value_schema_version=proposal.value_schema_version,
        target_ids=target_ids,
        target_hints=proposal.target_hints.model_dump(mode="json"),
        evidence=evidence,
    )


def normalize_record_value(
    *,
    owner_id: str,
    subject_key: str,
    memory_type: MemoryType,
    domain_key: str,
    slot_key: str,
    canonical_value: JsonValue,
    display_text: str,
    sensitivity: Sensitivity,
    value_schema_version: int,
    keyed_provider: KeyedFingerprintProvider,
    scope_type: str = "global",
    scope_project_id: str | None = None,
) -> NormalizedRecordValue:
    if value_schema_version not in SUPPORTED_VALUE_SCHEMA_VERSIONS:
        raise MemoryNormalizationError("unsupported_value_schema_version")
    identity = MemoryIdentity(
        memory_type=memory_type,
        domain_key=validate_domain_key(domain_key),
        slot_key=slot_key,
        cardinality=(
            Cardinality.ADDITIVE
            if ":independent:" in slot_key or ":item:" in slot_key
            else Cardinality.EXCLUSIVE
        ),
    )
    _validate_slot(identity)
    normalized_value = _normalize_json(canonical_value)
    positive_display = _validate_positive_text(display_text)
    fingerprint = canonical_fingerprint(
        owner_id=canonical_uuid(owner_id),
        subject_key=subject_key,
        memory_type=memory_type,
        scope_type=scope_type,
        scope_project_id=scope_project_id,
        domain_key=domain_key,
        slot_key=slot_key,
        canonical_value=normalized_value,
        sensitivity=sensitivity,
        keyed_provider=keyed_provider,
    )
    return NormalizedRecordValue(
        canonical_value=normalized_value,
        canonical_json=canonical_json_bytes(normalized_value),
        display_text=positive_display,
        sensitivity=sensitivity,
        canonical_fingerprint=fingerprint,
    )


def compatible_refinement(existing: JsonValue, proposed: JsonValue) -> bool:
    """Return deterministic structural compatibility; no similarity is consulted."""

    old = _normalize_json(existing)
    new = _normalize_json(proposed)
    if old == new:
        return True
    if isinstance(old, str) and isinstance(new, str):
        old_folded = old.casefold()
        new_folded = new.casefold()
        return old_folded in new_folded and len(new_folded) > len(old_folded)
    if isinstance(old, dict) and isinstance(new, dict):
        return all(key in new and new[key] == value for key, value in old.items())
    return False


def operation_request_hash(
    command: MemoryCommandBase,
    *,
    keyed_provider: KeyedFingerprintProvider,
    sensitivity: Sensitivity,
) -> str:
    validate_command_versions(command)
    encoded = json.dumps(
        command.model_dump(mode="json", exclude_none=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if sensitivity is Sensitivity.NORMAL:
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    digest = keyed_provider.fingerprint(encoded, owner_id=canonical_uuid(command.owner_id))
    return f"keyed:{digest.key_version}:{digest.digest}"

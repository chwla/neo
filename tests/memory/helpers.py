from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.services.memory.contracts import (
    ActorKind,
    CandidateIntent,
    CandidateTargetHints,
    EvidenceRole,
    EvidenceSpan,
    MemoryActor,
    MemorySource,
    Sensitivity,
    SourceKind,
    ValidatedCandidateProposal,
)
from app.services.memory.crypto import EncryptedValue, KeyedDigest
from app.services.memory.taxonomy import Cardinality, MemoryType

OWNER_A = "00000000-0000-4000-8000-000000000001"
OWNER_B = "00000000-0000-4000-8000-000000000002"
DATABASE_IDENTITY = "phase2-test-profile"


@dataclass(frozen=True)
class DeterministicTestCrypto:
    """Reversible authenticated test provider; never used by application runtime."""

    _encryption_key: bytes = b"memory-test-encryption-material"
    _fingerprint_key: bytes = b"memory-test-fingerprint-material"
    _tombstone_key: bytes = b"memory-test-tombstone-material"

    @staticmethod
    def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(
                hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            )
            counter += 1
        return bytes(output[:length])

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptedValue:
        nonce = hmac.new(
            self._encryption_key,
            associated_data + b"\0" + plaintext,
            hashlib.sha256,
        ).digest()[:12]
        stream = self._stream(self._encryption_key, nonce, len(plaintext))
        body = bytes(left ^ right for left, right in zip(plaintext, stream, strict=True))
        tag = hmac.new(
            self._encryption_key,
            associated_data + nonce + body,
            hashlib.sha256,
        ).digest()[:16]
        return EncryptedValue(
            ciphertext=body + tag,
            algorithm="test-xor-hmac-v1",
            key_version="test-encryption-v1",
            nonce=nonce,
            associated_data=associated_data,
        )

    def decrypt(self, payload: EncryptedValue, *, associated_data: bytes) -> bytes:
        body, tag = payload.ciphertext[:-16], payload.ciphertext[-16:]
        expected = hmac.new(
            self._encryption_key,
            associated_data + payload.nonce + body,
            hashlib.sha256,
        ).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("test_ciphertext_authentication_failed")
        stream = self._stream(self._encryption_key, payload.nonce, len(body))
        return bytes(left ^ right for left, right in zip(body, stream, strict=True))

    def fingerprint(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        digest = hmac.new(
            self._fingerprint_key,
            owner_id.encode() + b"\0" + material,
            hashlib.sha256,
        ).hexdigest()
        return KeyedDigest(digest=digest, key_version="test-fingerprint-v1")

    def create(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        digest = hmac.new(
            self._tombstone_key,
            owner_id.encode() + b"\0" + material,
            hashlib.sha256,
        ).hexdigest()
        return KeyedDigest(digest=digest, key_version="test-tombstone-v1")

    def verify(
        self,
        material: bytes,
        *,
        owner_id: str,
        digest: str,
        key_version: str,
    ) -> bool:
        expected = self.create(material, owner_id=owner_id)
        return key_version == expected.key_version and hmac.compare_digest(digest, expected.digest)

    def current_encryption_key_version(self) -> str:
        return "test-encryption-v1"

    def current_fingerprint_key_version(self) -> str:
        return "test-fingerprint-v1"

    def current_tombstone_key_version(self) -> str:
        return "test-tombstone-v1"


def actor() -> MemoryActor:
    return MemoryActor(kind=ActorKind.USER, actor_id="phase2-test-user")


def source(*, include_correction_evidence: bool = False) -> MemorySource:
    evidence = ()
    if include_correction_evidence:
        evidence = (
            EvidenceSpan(
                role=EvidenceRole.RETRACTION,
                text="no longer pursue the prior video goal",
            ),
            EvidenceSpan(
                role=EvidenceRole.ASSERTION,
                text="create short Instagram reels clearly",
            ),
        )
    return MemorySource(
        kind=SourceKind.DIRECT_COMMAND,
        source_id=f"source-{uuid4()}",
        conversation_id="phase2-conversation",
        message_id=f"message-{uuid4()}",
        evidence=evidence,
    )


def candidate(
    value: object,
    *,
    display: str | None = None,
    memory_type: MemoryType = MemoryType.GOAL,
    domain: str = "video_creation",
    slot: str = "goal:video_creation:current_primary_goal",
    cardinality: Cardinality = Cardinality.EXCLUSIVE,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    intent: CandidateIntent = CandidateIntent.ASSERT,
    explicit: bool = False,
    targets: tuple[UUID, ...] = (),
    explicit_domain_change: bool = False,
    explicit_slot_change: bool = False,
) -> ValidatedCandidateProposal:
    rendered = display if display is not None else str(value)
    return ValidatedCandidateProposal(
        intent=intent,
        memory_type=memory_type,
        domain_key=domain,
        slot_key=slot,
        cardinality=cardinality,
        canonical_value=value,
        display_text=rendered,
        sensitivity=sensitivity,
        confidence=0.95,
        importance=7,
        explicit_user_request=explicit,
        target_hints=CandidateTargetHints(
            target_memory_ids=targets,
            explicit_domain_change=explicit_domain_change,
            explicit_slot_change=explicit_slot_change,
        ),
    )

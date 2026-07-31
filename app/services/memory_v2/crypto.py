"""Injectable cryptographic boundaries for the isolated memory-v2 kernel.

Phase 2 defines representations and fail-closed interfaces only. Production key
configuration remains deliberately unavailable until a later runtime phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class MemoryCryptoUnavailable(RuntimeError):
    """Fixed, non-secret-bearing failure raised when no provider is configured."""


@dataclass(frozen=True)
class KeyedDigest:
    digest: str
    key_version: str


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    algorithm: str
    key_version: str
    nonce: bytes
    associated_data: bytes


@runtime_checkable
class SensitivePayloadProvider(Protocol):
    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptedValue: ...

    def decrypt(self, payload: EncryptedValue, *, associated_data: bytes) -> bytes: ...


@runtime_checkable
class KeyedFingerprintProvider(Protocol):
    def fingerprint(self, material: bytes, *, owner_id: str) -> KeyedDigest: ...


@runtime_checkable
class TombstoneHMACProvider(Protocol):
    def create(self, material: bytes, *, owner_id: str) -> KeyedDigest: ...

    def verify(
        self,
        material: bytes,
        *,
        owner_id: str,
        digest: str,
        key_version: str,
    ) -> bool: ...


@runtime_checkable
class KeyVersionResolver(Protocol):
    def current_encryption_key_version(self) -> str: ...

    def current_fingerprint_key_version(self) -> str: ...

    def current_tombstone_key_version(self) -> str: ...


class UnavailableSensitivePayloadProvider:
    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptedValue:
        del plaintext, associated_data
        raise MemoryCryptoUnavailable("sensitive_payload_provider_unavailable")

    def decrypt(self, payload: EncryptedValue, *, associated_data: bytes) -> bytes:
        del payload, associated_data
        raise MemoryCryptoUnavailable("sensitive_payload_provider_unavailable")


class UnavailableKeyedFingerprintProvider:
    def fingerprint(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        del material, owner_id
        raise MemoryCryptoUnavailable("keyed_fingerprint_provider_unavailable")


class UnavailableTombstoneHMACProvider:
    def create(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        del material, owner_id
        raise MemoryCryptoUnavailable("tombstone_hmac_provider_unavailable")

    def verify(
        self,
        material: bytes,
        *,
        owner_id: str,
        digest: str,
        key_version: str,
    ) -> bool:
        del material, owner_id, digest, key_version
        raise MemoryCryptoUnavailable("tombstone_hmac_provider_unavailable")


def build_associated_data(
    *,
    owner_id: str,
    memory_type: str,
    domain_key: str,
    slot_key: str,
    record_id: str,
    schema_version: int,
    key_version: str,
    purpose: str,
) -> bytes:
    """Build canonical AEAD metadata binding ciphertext to its semantic identity."""

    if schema_version < 1:
        raise ValueError("associated_data_schema_version_invalid")
    material = {
        "domain_key": domain_key,
        "key_version": key_version,
        "memory_type": memory_type,
        "owner_id": owner_id,
        "purpose": purpose,
        "record_id": record_id,
        "schema_version": schema_version,
        "slot_key": slot_key,
    }
    return json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")

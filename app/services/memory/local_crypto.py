"""Local owner-bound cryptography for memory payloads."""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.memory.crypto import EncryptedValue, KeyedDigest

#: The key version every record written before the keyring existed carries. Seeds of
#: this version were *derived* from the profile's stored password hash, so a password
#: change changed the key -- see ``keyring`` for how those seeds are now preserved.
LEGACY_KEY_VERSION = "local-memory-v1"


@dataclass(frozen=True)
class LocalMemoryCrypto:
    """An AES-GCM and keyed-digest provider with caller-supplied key material.

    ``key_version`` names the seed. It defaults to the legacy version so that a
    provider built the old way keeps stamping records exactly as before; the keyring
    passes its own version for the randomly generated active key.
    """

    seed: bytes
    key_version: str = LEGACY_KEY_VERSION

    def __post_init__(self) -> None:
        if len(self.seed) < 32:
            raise ValueError("memory_crypto_seed_too_short")

    def _key(self, purpose: bytes) -> bytes:
        return hmac.new(self.seed, purpose, hashlib.sha256).digest()

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptedValue:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key(b"encryption")).encrypt(
            nonce,
            plaintext,
            associated_data,
        )
        return EncryptedValue(
            ciphertext=ciphertext,
            algorithm="aes-256-gcm",
            key_version=self.key_version,
            nonce=nonce,
            associated_data=associated_data,
        )

    def decrypt(self, payload: EncryptedValue, *, associated_data: bytes) -> bytes:
        return AESGCM(self._key(b"encryption")).decrypt(
            payload.nonce,
            payload.ciphertext,
            associated_data,
        )

    def fingerprint(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        digest = hmac.new(
            self._key(b"fingerprint"),
            owner_id.encode() + b"\0" + material,
            hashlib.sha256,
        ).hexdigest()
        return KeyedDigest(digest=digest, key_version=self.key_version)

    def create(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        digest = hmac.new(
            self._key(b"tombstone"),
            owner_id.encode() + b"\0" + material,
            hashlib.sha256,
        ).hexdigest()
        return KeyedDigest(digest=digest, key_version=self.key_version)

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
        return self.key_version

    def current_fingerprint_key_version(self) -> str:
        return self.key_version

    def current_tombstone_key_version(self) -> str:
        return self.key_version

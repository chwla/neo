"""A crypto provider that writes with one key and reads with every key on the ring.

Writes always use the keyring's active key, so new records and new tombstones carry
the current version. Reads and digest verification try the active key first and then
every legacy key, which is what keeps records written under the old derived key
readable and keeps old tombstones suppressing the memories they forgot.

AES-GCM makes the read side exact rather than a guess: the authentication tag fails
for the wrong key, so trying keys in turn either returns the real plaintext or
exhausts the ring and raises. Nothing here can silently return the wrong plaintext.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from app.services.memory.crypto import EncryptedValue, KeyedDigest
from app.services.memory.keyring import Keyring, providers
from app.services.memory.local_crypto import LocalMemoryCrypto


class MemoryPayloadUndecryptable(RuntimeError):
    """No key on the ring could decrypt a payload.

    Raised instead of returning anything, and the caller must never respond by
    deleting or overwriting the row: the ciphertext is the only remaining copy of
    the user's data, and a key may still be recoverable from a registry backup.
    """


@dataclass(frozen=True)
class KeyringMemoryCrypto:
    """Implements the payload, fingerprint and tombstone provider protocols."""

    ring: Keyring

    @property
    def _active(self) -> LocalMemoryCrypto:
        return LocalMemoryCrypto(
            seed=self.ring.active.seed, key_version=self.ring.active.key_version
        )

    @property
    def _all(self) -> tuple[LocalMemoryCrypto, ...]:
        return providers(self.ring)

    # --- payloads ---------------------------------------------------------

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> EncryptedValue:
        return self._active.encrypt(plaintext, associated_data=associated_data)

    def decrypt(self, payload: EncryptedValue, *, associated_data: bytes) -> bytes:
        candidates = self._all
        named = [p for p in candidates if p.key_version == payload.key_version]
        others = [p for p in candidates if p.key_version != payload.key_version]
        last: Exception | None = None
        for provider in (*named, *others):
            try:
                return provider.decrypt(payload, associated_data=associated_data)
            except Exception as exc:  # AES-GCM tag mismatch for the wrong key
                last = exc
        raise MemoryPayloadUndecryptable(
            f"no key on the ring decrypts key_version={payload.key_version!r}"
        ) from last

    # --- keyed digests ----------------------------------------------------

    def fingerprint(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        return self._active.fingerprint(material, owner_id=owner_id)

    def create(self, material: bytes, *, owner_id: str) -> KeyedDigest:
        return self._active.create(material, owner_id=owner_id)

    def verify(
        self,
        material: bytes,
        *,
        owner_id: str,
        digest: str,
        key_version: str,
    ) -> bool:
        """True when any key on the ring reproduces ``digest``.

        The stored ``key_version`` is a hint, not a requirement: an entry recorded
        under a different version string can still hold the right seed (a seed
        captured from a backup, say), and refusing it would un-suppress a deletion.
        """

        for provider in self._all:
            expected = provider.create(material, owner_id=owner_id)
            if hmac.compare_digest(digest, expected.digest):
                return True
        return False

    def matches_fingerprint(
        self, material: bytes, *, owner_id: str, digest: str
    ) -> bool:
        """The same any-key comparison for fingerprints rather than tombstones."""

        for provider in self._all:
            expected = provider.fingerprint(material, owner_id=owner_id)
            if hmac.compare_digest(digest, expected.digest):
                return True
        return False

    # --- current versions -------------------------------------------------

    def current_encryption_key_version(self) -> str:
        return self.ring.active.key_version

    def current_fingerprint_key_version(self) -> str:
        return self.ring.active.key_version

    def current_tombstone_key_version(self) -> str:
        return self.ring.active.key_version


__all__ = ["KeyringMemoryCrypto", "MemoryPayloadUndecryptable"]

"""Owner-bound tombstone policy primitives for the Phase 2 mutation kernel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.identifiers import canonical_uuid
from app.services.memory.crypto import KeyedDigest, TombstoneHMACProvider
from app.services.memory.policy import FORGET_TOMBSTONE_DAYS


@dataclass(frozen=True)
class TombstoneSnapshot:
    id: str
    owner_id: str
    fingerprint_digest: str
    fingerprint_key_version: str
    memory_type: str
    domain_key: str | None
    slot_key: str | None
    created_at: datetime
    expires_at: datetime
    explicitly_reconfirmed: bool

    def is_active(self, now: datetime) -> bool:
        return self.expires_at > now and not self.explicitly_reconfirmed


def tombstone_digest(
    canonical_fingerprint: str,
    *,
    owner_id: str,
    provider: TombstoneHMACProvider,
) -> KeyedDigest:
    owner = canonical_uuid(owner_id)
    material = f"neo.memory.tombstone.v1\0{canonical_fingerprint}".encode()
    return provider.create(material, owner_id=owner)


def tombstone_matches(
    tombstone: TombstoneSnapshot,
    canonical_fingerprint: str,
    *,
    owner_id: str,
    now: datetime,
    provider: TombstoneHMACProvider,
) -> bool:
    owner = canonical_uuid(owner_id)
    if tombstone.owner_id != owner or not tombstone.is_active(now):
        return False
    material = f"neo.memory.tombstone.v1\0{canonical_fingerprint}".encode()
    return provider.verify(
        material,
        owner_id=owner,
        digest=tombstone.fingerprint_digest,
        key_version=tombstone.fingerprint_key_version,
    )


def resurrection_blocked(
    tombstones: tuple[TombstoneSnapshot, ...],
    canonical_fingerprint: str,
    *,
    owner_id: str,
    now: datetime,
    explicit_reconfirmation: bool,
    provider: TombstoneHMACProvider,
) -> TombstoneSnapshot | None:
    if explicit_reconfirmation:
        return None
    for item in tombstones:
        if tombstone_matches(
            item,
            canonical_fingerprint,
            owner_id=owner_id,
            now=now,
            provider=provider,
        ):
            return item
    return None


def tombstone_expiration(created_at: datetime) -> datetime:
    return created_at + timedelta(days=FORGET_TOMBSTONE_DAYS)

"""Per-profile memory key material, and the keys needed to read what came before.

Memory keys used to be *derived*: ``sha256("neo-memory:" + owner_id + ":" +
password_hash)``. Two things were wrong with that. The inputs both live in the profile
registry, so anything that could read the registry could rebuild the key; and changing
the password changed the key, which silently orphaned every encrypted record and every
tombstone on the profile.

This module replaces the derivation with a keyring file holding

* one **active** key: 32 random bytes, used for everything newly written; and
* zero or more **legacy** entries: the seeds of key versions that existing rows and
  tombstones were written under, kept so that those rows stay readable and -- crucially
  -- so that a tombstone keeps suppressing the memory it forgot.

A tombstone stores only an HMAC of the forgotten memory's fingerprint. It cannot be
recomputed under a new key without the original deleted content, and keeping that
content would defeat the point of forgetting it. So the *key* is preserved instead, and
verification tries every key on the ring. Encryption-key rotation and digest matching
are therefore deliberately separate concerns: new writes use the active key, while
matching stays able to answer for every key that ever wrote.

The legacy seed has to be captured **before** the password that derives it changes,
which is why ``ensure_keyring`` is called on the password-change path. For a profile
whose password changed before this existed, the derived seed is simply gone; nothing
here invents it, and :func:`recover_legacy_seeds` can only offer candidates read from
registry backups. Unreadable rows are never overwritten or deleted on that account.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.services.memory.local_crypto import LEGACY_KEY_VERSION, LocalMemoryCrypto

KEYRING_FILENAME = "memory-keyring.json"
KEYRING_VERSION = 1
#: Seed length for a generated key. The provider refuses anything shorter.
SEED_BYTES = 32


class KeyringError(RuntimeError):
    """The keyring could not be read or written. Never raised for "not found"."""


@dataclass(frozen=True)
class KeyEntry:
    key_version: str
    seed: bytes
    source: str
    captured_at: str


@dataclass(frozen=True)
class Keyring:
    active: KeyEntry
    legacy: tuple[KeyEntry, ...]

    def entries(self) -> tuple[KeyEntry, ...]:
        """Active key first, then legacy, which is the order reads should try."""

        return (self.active, *self.legacy)

    def has_version(self, key_version: str) -> bool:
        return any(entry.key_version == key_version for entry in self.entries())


def keyring_path(storage_dir: str | os.PathLike[str]) -> Path:
    return Path(storage_dir) / "keys" / KEYRING_FILENAME


def _encode(entry: KeyEntry) -> dict:
    return {
        "key_version": entry.key_version,
        "seed": base64.b64encode(entry.seed).decode("ascii"),
        "source": entry.source,
        "captured_at": entry.captured_at,
    }


def _decode(raw: dict) -> KeyEntry | None:
    try:
        seed = base64.b64decode(raw["seed"], validate=True)
        version = str(raw["key_version"])
    except Exception:
        return None
    if len(seed) < SEED_BYTES or not version:
        return None
    return KeyEntry(
        key_version=version,
        seed=seed,
        source=str(raw.get("source") or "unknown"),
        captured_at=str(raw.get("captured_at") or ""),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def load(storage_dir: str | os.PathLike[str]) -> Keyring | None:
    """Read the keyring, or ``None`` when the profile has none yet.

    A malformed or truncated file raises rather than being silently replaced: a
    missing key is a profile whose memory cannot be read, and quietly generating a
    fresh one would orphan every record it has.
    """

    path = keyring_path(storage_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KeyringError(f"memory_keyring_unreadable: {exc}") from exc
    try:
        data = json.loads(raw)
        active = _decode(data["active"])
        legacy_raw = data.get("legacy") or []
    except Exception as exc:
        raise KeyringError("memory_keyring_malformed") from exc
    if active is None:
        raise KeyringError("memory_keyring_missing_active_key")
    legacy: list[KeyEntry] = []
    seen = {active.key_version}
    for item in legacy_raw:
        entry = _decode(item) if isinstance(item, dict) else None
        if entry is not None and entry.key_version not in seen:
            legacy.append(entry)
            seen.add(entry.key_version)
    return Keyring(active=active, legacy=tuple(legacy))


def _write(storage_dir: str | os.PathLike[str], ring: Keyring) -> None:
    """Replace the keyring atomically, 0600, with the bytes on disk before the rename."""

    path = keyring_path(storage_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(
        {
            "version": KEYRING_VERSION,
            "active": _encode(ring.active),
            "legacy": [_encode(entry) for entry in ring.legacy],
        },
        indent=2,
        sort_keys=True,
    )
    tmp = path.with_name(path.name + f".tmp-{secrets.token_hex(6)}")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(payload.encode("utf-8"))
                stream.flush()
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        # fsync the directory too, so the rename itself survives a crash.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise KeyringError(f"memory_keyring_write_failed: {exc}") from exc


def new_active_entry() -> KeyEntry:
    return KeyEntry(
        key_version=f"keyring-v2:{secrets.token_hex(4)}",
        seed=secrets.token_bytes(SEED_BYTES),
        source="generated",
        captured_at=_now(),
    )


def ensure_keyring(
    storage_dir: str | os.PathLike[str],
    *,
    legacy_seed: bytes | None = None,
    legacy_source: str = "derived:password_hash",
) -> Keyring:
    """Return this profile's keyring, creating it on first use.

    ``legacy_seed`` is the old derived seed. On creation it is recorded so that
    everything already written under it stays readable and keeps matching; on an
    existing keyring it is folded in if it is new, which is what lets a seed be
    captured on the way through a password change.

    Idempotent, and safe to call on every request.
    """

    existing = load(storage_dir)
    if existing is None:
        legacy: tuple[KeyEntry, ...] = ()
        if legacy_seed:
            legacy = (
                KeyEntry(
                    key_version=LEGACY_KEY_VERSION,
                    seed=legacy_seed,
                    source=legacy_source,
                    captured_at=_now(),
                ),
            )
        ring = Keyring(active=new_active_entry(), legacy=legacy)
        _write(storage_dir, ring)
        return ring

    if legacy_seed and not any(entry.seed == legacy_seed for entry in existing.entries()):
        # A seed we have not seen. It belongs on the ring whatever its version
        # string, so give it a distinct one rather than colliding with an entry
        # that holds different material.
        version = LEGACY_KEY_VERSION
        if existing.has_version(version):
            version = f"{LEGACY_KEY_VERSION}+{secrets.token_hex(3)}"
        updated = Keyring(
            active=existing.active,
            legacy=(
                *existing.legacy,
                KeyEntry(
                    key_version=version,
                    seed=legacy_seed,
                    source=legacy_source,
                    captured_at=_now(),
                ),
            ),
        )
        _write(storage_dir, updated)
        return updated
    return existing


def add_legacy_seed(storage_dir: str | os.PathLike[str], seed: bytes, *, source: str) -> Keyring:
    """Record one more key that existing rows or tombstones may have been written under."""

    return ensure_keyring(storage_dir, legacy_seed=seed, legacy_source=source)


def legacy_seeds_from_registry_backups(
    *,
    profile_id: str,
    owner_id: str,
    registry_backups: tuple[str | os.PathLike[str], ...],
) -> tuple[tuple[bytes, str], ...]:
    """Read matching historical seeds without modifying any backup or keyring."""

    import hashlib

    found: list[tuple[bytes, str]] = []
    seen: set[bytes] = set()
    for backup in registry_backups:
        path = Path(backup).expanduser().resolve(strict=True)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT owner_id, password_hash FROM account_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[0] != owner_id or not row[1]:
            continue
        seed = hashlib.sha256(f"neo-memory:{row[0]}:{row[1]}".encode()).digest()
        if seed not in seen:
            found.append((seed, f"registry_backup:{path.name}"))
            seen.add(seed)
    return tuple(found)


def recover_legacy_seeds(
    storage_dir: str | os.PathLike[str],
    *,
    profile_id: str,
    owner_id: str,
    registry_backups: tuple[str | os.PathLike[str], ...],
) -> Keyring:
    """Capture historical password-derived seeds from explicit registry backups.

    Backups are read-only SQLite files supplied by the operator. A row must match
    both profile and owner identities before its password hash can yield a seed.
    The keyring retains every distinct seed for old ciphertext and tombstones.
    """

    ring = load(storage_dir)
    if ring is None:
        raise KeyringError("memory_keyring_required_for_recovery")
    for seed, source in legacy_seeds_from_registry_backups(
        profile_id=profile_id, owner_id=owner_id, registry_backups=registry_backups
    ):
        ring = add_legacy_seed(storage_dir, seed, source=source)
    return ring


def providers(ring: Keyring) -> tuple[LocalMemoryCrypto, ...]:
    return tuple(
        LocalMemoryCrypto(seed=entry.seed, key_version=entry.key_version)
        for entry in ring.entries()
    )


__all__ = [
    "KEYRING_FILENAME",
    "KeyEntry",
    "Keyring",
    "KeyringError",
    "add_legacy_seed",
    "ensure_keyring",
    "keyring_path",
    "legacy_seeds_from_registry_backups",
    "load",
    "new_active_entry",
    "providers",
    "recover_legacy_seeds",
]

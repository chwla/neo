"""Offline, backed-up rotation of legacy encrypted memory payloads.

The caller must stop Neo before applying this migration. Dry runs open the
database and keyring read-only and report counts without exposing plaintext.
Unreadable rows are left byte-for-byte intact so a recovered legacy key can be
added later. Legacy keys remain on the ring for tombstone matching.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex

from app.core.sqlite_backup import backup_sqlite_file
from app.services.memory.crypto import EncryptedValue
from app.services.memory.keyring import KeyEntry, Keyring, ensure_keyring, load, new_active_entry
from app.services.memory.keyring_crypto import KeyringMemoryCrypto, MemoryPayloadUndecryptable
from app.services.memory.local_crypto import LEGACY_KEY_VERSION
from app.services.memory.normalization import canonical_fingerprint
from app.services.memory.policy import Sensitivity
from app.services.memory.taxonomy import MemoryType

_PAIRS = (
    (
        "memory_records",
        "encrypted_canonical_payload",
        "canonical_nonce",
        "encrypted_display_payload",
        "display_nonce",
        "encryption_algorithm",
        "encryption_key_version",
        "encryption_aad",
    ),
    (
        "memory_candidates",
        "encrypted_canonical_payload",
        "canonical_nonce",
        "encrypted_display_payload",
        "display_nonce",
        "encryption_algorithm",
        "encryption_key_version",
        "encryption_aad",
    ),
)
_SINGLES = (
    (
        "memory_operations",
        "encrypted_command_payload",
        "encryption_nonce",
        "encryption_algorithm",
        "encryption_key_version",
        "encryption_aad",
    ),
    (
        "memory_sources",
        "encrypted_excerpt",
        "excerpt_nonce",
        "excerpt_encryption_algorithm",
        "excerpt_key_version",
        "excerpt_aad",
    ),
)


@dataclass(frozen=True)
class RotationResult:
    eligible: int
    rotated: int
    unreadable: int
    already_current: int
    backup_dir: str | None


def _temporary_ring(legacy_seed: bytes) -> Keyring:
    return Keyring(
        active=new_active_entry(),
        legacy=(KeyEntry(LEGACY_KEY_VERSION, legacy_seed, "dry_run", ""),),
    )


def _decrypt(
    crypto: KeyringMemoryCrypto,
    row: sqlite3.Row,
    *,
    payload: str,
    nonce: str,
    algorithm: str,
    version: str,
    aad: str,
) -> bytes:
    return crypto.decrypt(
        EncryptedValue(row[payload], row[algorithm], row[version], row[nonce], row[aad]),
        associated_data=row[aad],
    )


def _new_aad(old: bytes, version: str, *, row_id: str, owner_id: str) -> bytes:
    data = json.loads(old)
    if (
        not isinstance(data, dict)
        or data.get("record_id") != row_id
        or data.get("owner_id") != owner_id
    ):
        raise ValueError("memory_aad_identity_mismatch")
    data["key_version"] = version
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _record_fingerprint(
    row: sqlite3.Row, canonical: bytes, crypto: KeyringMemoryCrypto
) -> str | None:
    if row["status"] != "active" or row["sensitivity"] != "sensitive":
        return None
    value = json.loads(canonical)
    digest = canonical_fingerprint(
        owner_id=row["owner_id"],
        subject_key=row["subject_key"],
        memory_type=MemoryType(row["memory_type"]),
        domain_key=row["domain_key"],
        slot_key=row["slot_key"],
        canonical_value=value,
        sensitivity=Sensitivity.SENSITIVE,
        keyed_provider=crypto,
        scope_type=row["scope_type"],
        scope_project_id=row["scope_project_id"],
    )
    return digest


def _scan(conn: sqlite3.Connection, crypto: KeyringMemoryCrypto, *, apply: bool) -> Counter:
    counts: Counter = Counter()
    active = crypto.current_encryption_key_version()
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for (
        table,
        canonical,
        canonical_nonce,
        display,
        display_nonce,
        algorithm,
        version,
        aad,
    ) in _PAIRS:
        if table not in existing:
            continue
        rows = conn.execute(f"SELECT * FROM {table} WHERE {canonical} IS NOT NULL").fetchall()
        for row in rows:
            if row[version] == active:
                counts["already_current"] += 1
                continue
            counts["eligible"] += 1
            try:
                clear_canonical = _decrypt(
                    crypto,
                    row,
                    payload=canonical,
                    nonce=canonical_nonce,
                    algorithm=algorithm,
                    version=version,
                    aad=aad,
                )
                clear_display = _decrypt(
                    crypto,
                    row,
                    payload=display,
                    nonce=display_nonce,
                    algorithm=algorithm,
                    version=version,
                    aad=aad,
                )
                fingerprint = (
                    _record_fingerprint(row, clear_canonical, crypto)
                    if table == "memory_records"
                    else None
                )
                fresh_aad = _new_aad(row[aad], active, row_id=row["id"], owner_id=row["owner_id"])
            except (MemoryPayloadUndecryptable, ValueError, TypeError, KeyError, UnicodeError):
                counts["unreadable"] += 1
                continue
            if not apply:
                continue
            encrypted_canonical = crypto.encrypt(clear_canonical, associated_data=fresh_aad)
            encrypted_display = crypto.encrypt(clear_display, associated_data=fresh_aad)
            fields = {
                canonical: encrypted_canonical.ciphertext,
                canonical_nonce: encrypted_canonical.nonce,
                display: encrypted_display.ciphertext,
                display_nonce: encrypted_display.nonce,
                version: active,
                aad: fresh_aad,
            }
            if fingerprint is not None:
                fields["canonical_fingerprint"] = fingerprint
            assignments = ", ".join(f"{key} = ?" for key in fields)
            updated = conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ? AND {version} = ?",
                (*fields.values(), row["id"], row[version]),
            )
            if updated.rowcount != 1:
                raise RuntimeError("memory_rotation_row_changed")
            stored = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row["id"],)).fetchone()
            if (
                _decrypt(
                    crypto,
                    stored,
                    payload=canonical,
                    nonce=canonical_nonce,
                    algorithm=algorithm,
                    version=version,
                    aad=aad,
                )
                != clear_canonical
                or _decrypt(
                    crypto,
                    stored,
                    payload=display,
                    nonce=display_nonce,
                    algorithm=algorithm,
                    version=version,
                    aad=aad,
                )
                != clear_display
            ):
                raise RuntimeError("memory_rotation_verification_failed")
            counts["rotated"] += 1
    for table, payload, nonce, algorithm, version, aad in _SINGLES:
        if table not in existing:
            continue
        rows = conn.execute(f"SELECT * FROM {table} WHERE {payload} IS NOT NULL").fetchall()
        for row in rows:
            if row[version] == active:
                counts["already_current"] += 1
                continue
            counts["eligible"] += 1
            try:
                clear = _decrypt(
                    crypto,
                    row,
                    payload=payload,
                    nonce=nonce,
                    algorithm=algorithm,
                    version=version,
                    aad=aad,
                )
                fresh_aad = _new_aad(row[aad], active, row_id=row["id"], owner_id=row["owner_id"])
            except (MemoryPayloadUndecryptable, ValueError, TypeError, KeyError, UnicodeError):
                counts["unreadable"] += 1
                continue
            if not apply:
                continue
            encrypted = crypto.encrypt(clear, associated_data=fresh_aad)
            updated = conn.execute(
                f"UPDATE {table} SET {payload} = ?, {nonce} = ?, {version} = ?, {aad} = ? "
                f"WHERE id = ? AND {version} = ?",
                (encrypted.ciphertext, encrypted.nonce, active, fresh_aad, row["id"], row[version]),
            )
            if updated.rowcount != 1:
                raise RuntimeError("memory_rotation_row_changed")
            stored = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row["id"],)).fetchone()
            if (
                _decrypt(
                    crypto,
                    stored,
                    payload=payload,
                    nonce=nonce,
                    algorithm=algorithm,
                    version=version,
                    aad=aad,
                )
                != clear
            ):
                raise RuntimeError("memory_rotation_verification_failed")
            counts["rotated"] += 1
    return counts


def rotate_profile_memory(
    *,
    database: Path,
    storage_dir: Path,
    registry: Path,
    legacy_seed: bytes,
    historical_seeds: tuple[tuple[bytes, str], ...] = (),
    apply: bool = False,
) -> RotationResult:
    """Count or rotate one profile's encrypted rows; apply requires Neo stopped.

    Backups of the SQLite database, registry and keyring are made before the
    write transaction. The backup path is returned for recovery. A failed
    transaction leaves the original database and all backups intact.
    """
    database = Path(database).resolve(strict=True)
    storage_dir = Path(storage_dir).resolve(strict=True)
    registry = Path(registry).resolve(strict=True)
    if not database.is_file() or not registry.is_file():
        raise ValueError("memory_migration_sources_required")
    ring = load(storage_dir)
    if apply:
        ring = ensure_keyring(storage_dir, legacy_seed=legacy_seed)
        for seed, source in historical_seeds:
            ring = ensure_keyring(storage_dir, legacy_seed=seed, legacy_source=source)
    elif ring is None:
        ring = _temporary_ring(legacy_seed)
    if not apply:
        # Count all recoverable rows without writing to the keyring. A fresh
        # temporary version is only a hint; decryption tries every seed.
        for index, (seed, source) in enumerate(
            ((legacy_seed, "current_registry"), *historical_seeds)
        ):
            if any(entry.seed == seed for entry in ring.entries()):
                continue
            ring = Keyring(
                active=ring.active,
                legacy=(
                    *ring.legacy,
                    KeyEntry(f"{LEGACY_KEY_VERSION}+dry-{index}", seed, source, ""),
                ),
            )
    crypto = KeyringMemoryCrypto(ring)
    backup_dir: Path | None = None
    if apply:
        backup_dir = (
            storage_dir
            / "backups"
            / f"memory-rotation-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{token_hex(4)}"
        )
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        backup_sqlite_file(database, backup_dir / "memory.db")
        backup_sqlite_file(registry, backup_dir / "registry.db")
        keyring_bytes = (storage_dir / "keys" / "memory-keyring.json").read_bytes()
        fd = os.open(
            backup_dir / "memory-keyring.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(keyring_bytes)
                stream.flush()
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    uri = f"file:{database}?mode={'rw' if apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        counts = _scan(conn, crypto, apply=apply)
        if apply:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_key_rotation "
                "(version TEXT PRIMARY KEY, completed_at TEXT NOT NULL, "
                "rotated INTEGER NOT NULL, unreadable INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO memory_key_rotation VALUES (?, ?, ?, ?)",
                (
                    ring.active.key_version,
                    datetime.now(UTC).isoformat(),
                    counts["rotated"],
                    counts["unreadable"],
                ),
            )
            conn.commit()
    except BaseException:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()
    return RotationResult(
        counts["eligible"],
        counts["rotated"],
        counts["unreadable"],
        counts["already_current"],
        str(backup_dir) if backup_dir else None,
    )

"""Locked notes' keys, opened by the profile's own password.

Each key version is a random 256-bit data key (DEK). It is stored only
wrapped (AES key wrap, RFC 3394) by a key-encryption key (KEK) derived from
the profile password with its own PBKDF2-SHA256: 600,000 iterations, a random
16-byte salt per wrap, and a purpose prefix on that salt, so the derivation is
never the one sign-in uses. The profile's stored password hash is never read
here and never used as key material.

The browser derives the same KEK with WebCrypto and unwraps the DEK itself;
content is encrypted and decrypted only there. The server holds the password
(and briefly the DEK) only inside the request that sets up, unlocks, recovers
or changes it, as sign-in already does.

A key version can have two wraps for a moment while the password changes:

1. ``stage_password_change`` adds a ``pending`` wrap under the new password;
2. the registry's password hash changes (``profile_accounts``);
3. ``promote_pending`` makes the pending wrap ``current`` and deletes the old.

If the process dies between steps, the next unlock (with whichever password
is valid by then) keeps the wrap that password opens and deletes the other
wrap *of the same key version*. A wrap is never deleted for not matching
when no wrap of its version matches: those notes need the earlier password
(``rewrap``), and stay encrypted until then.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from app.services.notes import store

KDF = "pbkdf2-sha256"
ITERATIONS = 600_000
SALT_BYTES = 16
DEK_BYTES = 32
# Prefixed to every salt: a notes key is never what the same password derives elsewhere.
PURPOSE = b"neo-notes-kek:v1:"
# A pending wrap this young may belong to a password change still under way in
# another request: an unlock with the old password must not take it away.
PENDING_GRACE = timedelta(minutes=10)


class KeysNotSetUp(LookupError):
    """No key version opens with this password, and none was asked to be made."""


class EarlierPasswordWrong(ValueError):
    """The earlier password opens none of the keys the current one can't."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def derive_kek(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), PURPOSE + salt, iterations, 32)


def _open(wrap: sqlite3.Row | dict, password: str) -> bytes | None:
    """The DEK a wrap holds, if this password opens it."""

    if wrap["kdf"] != KDF:
        return None
    kek = derive_kek(password, _unb64(wrap["salt"]), int(wrap["iterations"]))
    try:
        return aes_key_unwrap(kek, _unb64(wrap["wrapped_dek"]))
    except InvalidUnwrap:
        return None


def _insert_wrap(
    conn: sqlite3.Connection, version: int, password: str, dek: bytes, status: str
) -> str:
    salt = os.urandom(SALT_BYTES)
    # The count used is the count stored: a wrap always opens with what it records.
    iterations = ITERATIONS
    wrap_id = store.new_id()
    conn.execute(
        "INSERT INTO note_key_wraps(id, key_version, kdf, salt, iterations, wrapped_dek, "
        "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            wrap_id,
            version,
            KDF,
            _b64(salt),
            iterations,
            _b64(aes_key_wrap(derive_kek(password, salt, iterations), dek)),
            status,
            store.now_iso(),
        ),
    )
    return wrap_id


def _public(wrap: sqlite3.Row | dict) -> dict[str, Any]:
    """What the browser needs to open a key version itself."""

    return {
        "key_version": wrap["key_version"],
        "kdf": wrap["kdf"],
        "salt": wrap["salt"],
        "iterations": wrap["iterations"],
        "wrapped_dek": wrap["wrapped_dek"],
    }


def _by_version(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM note_key_wraps ORDER BY key_version, created_at"):
        grouped.setdefault(row["key_version"], []).append(row)
    return grouped


def _has_table(conn: sqlite3.Connection) -> bool:
    return store._has_table(conn, "note_key_wraps")


def _reconcile(conn: sqlite3.Connection, password: str) -> dict[str, Any]:
    """Settle each key version on the wrap this password opens (inside a transaction).

    Returns the wraps it opens and the versions it can't open.
    """

    opened: list[dict] = []
    unavailable: list[int] = []
    for version, wraps in _by_version(conn).items():
        matching = [wrap for wrap in wraps if _open(wrap, password) is not None]
        if not matching:
            # Nothing of this version opens: it needs an earlier password. Keep every wrap.
            unavailable.append(version)
            continue
        keep = next((w for w in matching if w["status"] == "current"), matching[0])
        if keep["status"] != "current":
            conn.execute("UPDATE note_key_wraps SET status = 'current' WHERE id = ?", (keep["id"],))
        # The rest of this version are copies of the same key under another password.
        young = datetime.now(UTC) - PENDING_GRACE
        others = [
            w["id"]
            for w in wraps
            if w["id"] != keep["id"]
            and not (w["status"] == "pending" and datetime.fromisoformat(w["created_at"]) > young)
        ]
        conn.executemany("DELETE FROM note_key_wraps WHERE id = ?", [(i,) for i in others])
        opened.append(_public(keep))
    return {
        "wraps": opened,
        "unavailable": unavailable,
        "current_version": max((w["key_version"] for w in opened), default=None),
    }


def state() -> dict[str, Any]:
    """Whether locked notes have been set up on this profile (no password needed)."""

    conn = store._connect()
    try:
        if not _has_table(conn):
            return {"set_up": False, "versions": []}
        versions = [r[0] for r in conn.execute("SELECT DISTINCT key_version FROM note_key_wraps")]
        return {"set_up": bool(versions), "versions": sorted(versions)}
    finally:
        conn.close()


def unlock(password: str) -> dict[str, Any]:
    """Open every key version this (already checked) profile password opens."""

    conn = store._connect()
    try:
        with store._tx(conn):
            result = _reconcile(conn, password)
        if not result["wraps"] and not result["unavailable"]:
            raise KeysNotSetUp()
        return result
    finally:
        conn.close()


def setup(password: str) -> dict[str, Any]:
    """Make sure a key version opens with this (already checked) password.

    Idempotent: when one already does, nothing new is made. When the only
    versions there need an earlier password, a new version is made for new
    locks; the old ones are kept, untouched.
    """

    conn = store._connect()
    try:
        with store._tx(conn):
            result = _reconcile(conn, password)
            if result["current_version"] is None:
                version = 1 + max(
                    [r[0] for r in conn.execute("SELECT key_version FROM note_key_wraps")],
                    default=0,
                )
                _insert_wrap(conn, version, password, os.urandom(DEK_BYTES), "current")
                result = _reconcile(conn, password)
        return result
    finally:
        conn.close()


def rewrap(current_password: str, earlier_password: str) -> dict[str, Any]:
    """Re-wrap the key versions only an earlier password opens under the current one.

    The data keys, and so every locked note, stay exactly as they are.
    """

    conn = store._connect()
    try:
        with store._tx(conn):
            recovered: list[int] = []
            for version, wraps in _by_version(conn).items():
                if any(_open(wrap, current_password) is not None for wrap in wraps):
                    continue
                for wrap in wraps:
                    dek = _open(wrap, earlier_password)
                    if dek is None:
                        continue
                    _insert_wrap(conn, version, current_password, dek, "current")
                    conn.execute("DELETE FROM note_key_wraps WHERE id = ?", (wrap["id"],))
                    recovered.append(version)
                    break
            if not recovered:
                raise EarlierPasswordWrong()
            result = _reconcile(conn, current_password)
        result["recovered"] = recovered
        return result
    finally:
        conn.close()


# ------------------------------------------------------- password change --


def stage_password_change(current_password: str, new_password: str) -> list[str]:
    """Step 1: a pending wrap under the new password for each key the current one opens.

    Returns the pending wraps' ids. Raises (and adds nothing) on any failure,
    which must stop the password change.
    """

    conn = store._connect()
    try:
        if not _has_table(conn):
            return []
        staged: list[str] = []
        with store._tx(conn):
            for version, wraps in _by_version(conn).items():
                for wrap in wraps:
                    if wrap["status"] != "current":
                        continue
                    dek = _open(wrap, current_password)
                    if dek is not None:
                        staged.append(_insert_wrap(conn, version, new_password, dek, "pending"))
                        break
        return staged
    finally:
        conn.close()


def discard_pending(wrap_ids: list[str]) -> None:
    """After a failed step 2: the new password never took, so its wraps go."""

    if not wrap_ids:
        return
    conn = store._connect()
    try:
        with store._tx(conn):
            conn.executemany(
                "DELETE FROM note_key_wraps WHERE id = ? AND status = 'pending'",
                [(i,) for i in wrap_ids],
            )
    finally:
        conn.close()


def promote_pending(wrap_ids: list[str]) -> None:
    """Step 3: the new password's wraps become current; the old wraps of those versions go."""

    if not wrap_ids:
        return
    conn = store._connect()
    try:
        with store._tx(conn):
            for wrap_id in wrap_ids:
                row = conn.execute(
                    "SELECT key_version FROM note_key_wraps WHERE id = ? AND status = 'pending'",
                    (wrap_id,),
                ).fetchone()
                if row is None:
                    continue
                conn.execute(
                    "DELETE FROM note_key_wraps WHERE key_version = ? AND id != ?",
                    (row["key_version"], wrap_id),
                )
                conn.execute(
                    "UPDATE note_key_wraps SET status = 'current' WHERE id = ?", (wrap_id,)
                )
    finally:
        conn.close()

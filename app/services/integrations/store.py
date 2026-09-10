"""Connected accounts in the profile database.

The convention is ``skills/store.py``'s: tables in the active profile's SQLite
database, raw ``sqlite3``, created on demand. Being in the *profile* database is
what makes connected accounts per-person on a shared machine, and it is why
signing out of a profile takes its accounts with it.

Three tables, and the split between the first two is deliberate:

* ``integration_connections`` is the part that can be shown. Which account,
  which capabilities, whether it still works. Nothing here is a secret, so
  ``public_connection`` can hand a row to the API without filtering.
* ``integration_credentials`` is the part that cannot. One sealed blob per
  connection, opened only through the vault. Keeping it in its own table means
  the readable half can be selected, listed and joined without a query ever
  having the ciphertext in reach, and a leak has to be deliberate rather than a
  ``SELECT *`` somebody forgot about.
* ``integration_oauth_states`` is in-flight authorisation, single-use and
  short-lived.

The credential helpers live here rather than in ``vault.py`` because they touch
the database; the vault stays pure cryptography, which is what keeps it free of
an import cycle.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings
from app.services.integrations.vault import open_json, scoped_aad, seal_json

#: The only keys the HTTP layer may see for a connection. Stated as data rather
#: than built by omission: a test asserts the response shape equals this set, so
#: a column added later cannot reach the browser by simply existing.
PUBLIC_CONNECTION_FIELDS = frozenset(
    {"id", "provider", "account_email", "capabilities", "status", "sync_enabled", "expires_at"}
)


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_integration_tables() -> None:
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS integration_connections (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            account_email TEXT NOT NULL,
            account_subject TEXT NOT NULL DEFAULT '',
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            client_mode TEXT NOT NULL DEFAULT 'builtin',
            sync_enabled INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'connected',
            last_error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_integration_connections_account
            ON integration_connections(provider, account_email);

        CREATE TABLE IF NOT EXISTS integration_credentials (
            connection_id TEXT PRIMARY KEY
                REFERENCES integration_connections(id) ON DELETE CASCADE,
            secret_nonce TEXT NOT NULL,
            secret_ciphertext TEXT NOT NULL,
            expires_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS integration_oauth_states (
            state_hash TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            session_hash TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            connection_id TEXT,
            verifier_nonce TEXT NOT NULL,
            verifier_ciphertext TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            return_origin TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_integration_oauth_states_expiry
            ON integration_oauth_states(expires_at);

        CREATE TABLE IF NOT EXISTS integration_oauth_clients (
            provider TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            secret_nonce TEXT,
            secret_ciphertext TEXT,
            updated_at TEXT NOT NULL
        );
        """)


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------


def _connection(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["capabilities"] = tuple(json.loads(item.pop("capabilities_json") or "[]"))
    item["sync_enabled"] = bool(item["sync_enabled"])
    return item


def public_connection(row: dict, *, expires_at: str | None = None) -> dict:
    """The shape the API returns. Built by allowlist, never by removing keys.

    Composed here rather than in the route so there is one definition of what is
    safe to show, and so ``expires_at`` -- which lives on the credential row --
    is joined in one place instead of being fetched ad hoc by each caller.
    """

    return {
        "id": row["id"],
        "provider": row["provider"],
        "account_email": row["account_email"],
        "capabilities": list(row.get("capabilities") or ()),
        "status": row["status"],
        "sync_enabled": bool(row.get("sync_enabled")),
        "expires_at": expires_at,
    }


def list_connections() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM integration_connections ORDER BY provider, account_email"
        ).fetchall()
    return [item for item in (_connection(row) for row in rows) if item is not None]


def get_connection(connection_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM integration_connections WHERE id=?", (connection_id,)
        ).fetchone()
    return _connection(row)


def find_connection(provider: str, account_email: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM integration_connections WHERE provider=? AND account_email=?",
            (provider, account_email),
        ).fetchone()
    return _connection(row)


def upsert_connection(
    *,
    connection_id: str,
    provider: str,
    account_email: str,
    account_subject: str,
    capabilities: tuple[str, ...],
    client_mode: str,
) -> dict:
    """Create the connection, or re-authorise the one this account already has.

    Re-connecting the same mailbox must update rather than insert: a second row
    for the same address would mean two sets of tokens, one of them stale, and no
    way for the user to tell which their tools were using.
    """

    timestamp = now_iso()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM integration_connections WHERE provider=? AND account_email=?",
            (provider, account_email),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """UPDATE integration_connections
                   SET account_subject=?, capabilities_json=?, client_mode=?,
                       status='connected', last_error_category=NULL, updated_at=?
                   WHERE id=?""",
                (
                    account_subject,
                    json.dumps(list(capabilities)),
                    client_mode,
                    timestamp,
                    existing["id"],
                ),
            )
            connection_id = existing["id"]
        else:
            conn.execute(
                """INSERT INTO integration_connections
                   (id, provider, account_email, account_subject, capabilities_json,
                    client_mode, sync_enabled, status, last_error_category,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,0,'connected',NULL,?,?)""",
                (
                    connection_id,
                    provider,
                    account_email,
                    account_subject,
                    json.dumps(list(capabilities)),
                    client_mode,
                    timestamp,
                    timestamp,
                ),
            )
    result = get_connection(connection_id)
    assert result is not None
    return result


def set_connection_status(
    connection_id: str, status: str, *, error_category: str | None = None
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE integration_connections
               SET status=?, last_error_category=?, updated_at=? WHERE id=?""",
            (status, error_category, now_iso(), connection_id),
        )


def set_sync_enabled(connection_id: str, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE integration_connections SET sync_enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now_iso(), connection_id),
        )


def delete_connection(connection_id: str) -> bool:
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM integration_connections WHERE id=?", (connection_id,)
        )
        return cursor.rowcount > 0


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def _credential_aad(connection_id: str) -> str:
    return scoped_aad(f"connection:{connection_id}")


def write_credential(
    *,
    connection_id: str,
    secret: dict,
    expires_at: str | None,
    expected_updated_at: str | None = None,
) -> str:
    """Seal and store a connection's secrets. Returns the new ``updated_at``.

    ``expected_updated_at`` makes a refresh a compare-and-swap. Two threads that
    both notice an expired token will both ask the provider for a new one; the
    loser must not be able to write its now-superseded rotation over the
    winner's, because the provider may already have invalidated it. Losing the
    race is not an error the caller has to handle by retrying the HTTP call --
    it means the credential in the database is newer than the one in hand.
    """

    nonce, ciphertext = seal_json(secret, aad=_credential_aad(connection_id))
    timestamp = now_iso()
    with _connect() as conn:
        if expected_updated_at is None:
            conn.execute(
                """INSERT INTO integration_credentials
                   (connection_id, secret_nonce, secret_ciphertext, expires_at, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(connection_id) DO UPDATE SET
                     secret_nonce=excluded.secret_nonce,
                     secret_ciphertext=excluded.secret_ciphertext,
                     expires_at=excluded.expires_at,
                     updated_at=excluded.updated_at""",
                (connection_id, nonce, ciphertext, expires_at, timestamp),
            )
            return timestamp
        cursor = conn.execute(
            """UPDATE integration_credentials
               SET secret_nonce=?, secret_ciphertext=?, expires_at=?, updated_at=?
               WHERE connection_id=? AND updated_at=?""",
            (nonce, ciphertext, expires_at, timestamp, connection_id, expected_updated_at),
        )
        if cursor.rowcount == 0:
            raise CredentialRaceError(
                "Stored credentials changed during refresh; retry with the latest token."
            )
        return timestamp


class CredentialRaceError(RuntimeError):
    """A refresh lost a compare-and-swap against a concurrent refresh."""


def read_credential(connection_id: str) -> tuple[dict, str, str | None] | None:
    """``(secret, updated_at, expires_at)``, or ``None`` when nothing is stored.

    ``updated_at`` comes back so the caller can hand it straight to
    ``write_credential`` as ``expected_updated_at`` -- read and compare-and-swap
    are two halves of the same operation and would be easy to get wrong apart.
    """

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM integration_credentials WHERE connection_id=?", (connection_id,)
        ).fetchone()
    if row is None:
        return None
    secret = open_json(
        row["secret_nonce"], row["secret_ciphertext"], aad=_credential_aad(connection_id)
    )
    return secret, row["updated_at"], row["expires_at"]


def credential_expiry(connection_id: str) -> str | None:
    """The expiry alone, without opening the seal -- what listing needs."""

    with _connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM integration_credentials WHERE connection_id=?",
            (connection_id,),
        ).fetchone()
    return row["expires_at"] if row is not None else None


# --------------------------------------------------------------------------
# In-flight OAuth state
# --------------------------------------------------------------------------


def insert_oauth_state(payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO integration_oauth_states
               (state_hash, provider, session_hash, capabilities_json, connection_id,
                verifier_nonce, verifier_ciphertext, redirect_uri, return_origin,
                created_at, expires_at, used_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                payload["state_hash"],
                payload["provider"],
                payload["session_hash"],
                payload["capabilities_json"],
                payload.get("connection_id"),
                payload["verifier_nonce"],
                payload["verifier_ciphertext"],
                payload["redirect_uri"],
                payload["return_origin"],
                payload["created_at"],
                payload["expires_at"],
            ),
        )


def consume_oauth_state(state_hash: str, session_hash: str, now: str) -> dict | None:
    """Claim a state exactly once, or return ``None``.

    The claim is the ``UPDATE``, not the ``SELECT``. Reading the row, checking it
    and then marking it used would let two callbacks arriving together both pass
    the check -- so the conditions live in the WHERE clause and the winner is
    whichever statement SQLite applies first. ``rowcount`` is the answer.

    Every reason to refuse -- unknown, expired, already used, belonging to a
    different profile session -- collapses into that single ``None``, because
    telling them apart would tell a caller which of those it had guessed right.
    """

    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE integration_oauth_states SET used_at=?
               WHERE state_hash=? AND session_hash=? AND used_at IS NULL AND expires_at > ?""",
            (now, state_hash, session_hash, now),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM integration_oauth_states WHERE state_hash=?", (state_hash,)
        ).fetchone()
    return dict(row) if row is not None else None


def delete_expired_oauth_states(now: str) -> int:
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM integration_oauth_states WHERE expires_at <= ?", (now,)
        )
        return cursor.rowcount


def get_oauth_state(state_hash: str) -> dict | None:
    """Read a state without claiming it.

    Used only to recover where to send the user back to when the flow ends
    badly. It deliberately does not check the session or the expiry: the single
    value it is read for is a return origin that was checked against a fixed
    allowlist before it was written and is checked again before it is used, so
    it discloses nothing that was not already one of four constants.
    """

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM integration_oauth_states WHERE state_hash=?", (state_hash,)
        ).fetchone()
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------
# Bring-your-own OAuth client
# --------------------------------------------------------------------------
#
# Neo is installed rather than hosted, so the OAuth client it identifies itself
# with has to come from somewhere. A build can ship one (``Settings``), but an
# install that has none is the normal case today, and telling somebody to edit a
# file and restart a container is a poor answer when the thing they are trying to
# do is press a button. So a client can also be supplied through the interface
# and stored here.
#
# Per profile, like everything else in this database: two people sharing a
# machine can point at their own Google Cloud projects without seeing each
# other's. The client secret is sealed even though a desktop client's secret is
# not confidential (RFC 8252 section 8.5) -- it costs nothing, and the value
# still should not sit in a column any feature can read.


def _client_aad(provider: str) -> str:
    return scoped_aad(f"oauth-client:{provider}")


def write_oauth_client(provider: str, *, client_id: str, client_secret: str) -> None:
    nonce, ciphertext = (
        seal_json({"client_secret": client_secret}, aad=_client_aad(provider))
        if client_secret
        else (None, None)
    )
    with _connect() as conn:
        conn.execute(
            """INSERT INTO integration_oauth_clients
               (provider, client_id, secret_nonce, secret_ciphertext, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(provider) DO UPDATE SET
                 client_id=excluded.client_id,
                 secret_nonce=excluded.secret_nonce,
                 secret_ciphertext=excluded.secret_ciphertext,
                 updated_at=excluded.updated_at""",
            (provider, client_id, nonce, ciphertext, now_iso()),
        )


def read_oauth_client(provider: str) -> tuple[str, str] | None:
    """``(client_id, client_secret)`` for a stored client, or ``None``.

    A missing table reads as "no client", not as an error. This runs on every
    attempt to start a connection, and a profile whose database predates the
    table would otherwise fail with an OperationalError rather than falling back
    to whatever client the build shipped -- the same reasoning that makes
    ``skills/store.py`` swallow it.
    """

    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM integration_oauth_clients WHERE provider=?", (provider,)
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    secret = ""
    if row["secret_nonce"] and row["secret_ciphertext"]:
        secret = str(
            open_json(
                row["secret_nonce"], row["secret_ciphertext"], aad=_client_aad(provider)
            ).get("client_secret")
            or ""
        )
    return row["client_id"], secret


def oauth_client_summary(provider: str) -> dict | None:
    """What the interface may know about a stored client.

    The client id comes back because it is an identifier the user pasted in and
    may need to recognise; the secret never does, only whether one is set.
    """

    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT client_id, secret_ciphertext, updated_at FROM integration_oauth_clients "
                "WHERE provider=?",
                (provider,),
            ).fetchone()
    except sqlite3.OperationalError:
        # Read on every panel open; see read_oauth_client.
        return None
    if row is None:
        return None
    return {
        "client_id": row["client_id"],
        "has_secret": bool(row["secret_ciphertext"]),
        "updated_at": row["updated_at"],
    }


def delete_oauth_client(provider: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "DELETE FROM integration_oauth_clients WHERE provider=?", (provider,)
        ).rowcount > 0

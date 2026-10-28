"""Local account registry and profile-scoped storage for Neo.

The registry intentionally stores only local credentials and public profile
metadata. Each profile gets its own SQLite database, which keeps saved chats,
memories, and workspace settings separate without needing an online account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from fastapi import HTTPException

from app.core.config import (
    active_profile_database_url,
    active_profile_storage_dir,
    get_base_settings,
    get_settings,
)
from app.core.identifiers import canonical_uuid
from app.core.paths import install_root, private_dir_mode, private_file_mode
from app.db.session import initialize_database

MAX_AVATAR_BYTES = 2 * 1024 * 1024
PASSWORD_ITERATIONS = 390_000
#: How many wrong passwords a profile tolerates before it is locked for a while.
#: The password is the only thing standing between a local process and the whole
#: profile, and PBKDF2 at 390k iterations still answers fast enough to brute-force a
#: short secret over HTTP; this is what makes the attempt count the limit instead.
UNLOCK_ATTEMPT_LIMIT = 5
#: The lockout doubles per failure past the limit, up to this ceiling.
UNLOCK_LOCKOUT_BASE_SECONDS = 15
UNLOCK_LOCKOUT_MAX_SECONDS = 15 * 60
#: A session is dropped after this long without use, and unconditionally after the
#: absolute ceiling. A cookie that never expires is a permanent key to the profile.
SESSION_IDLE_DAYS = 14
SESSION_ABSOLUTE_DAYS = 90
#: Minimum length for a password being set or changed: just non-empty. Existing
#: passwords are never re-validated, so this only applies where a new secret is
#: being chosen.
MIN_PASSWORD_LENGTH = 1
PROFILE_OWNER_REVISION = "0001_profile_owner_uuid"
PROFILE_SESSION_TOKEN_BYTES = 32
_registry_initialization_lock = Lock()
_initialized_registry_paths: set[Path] = set()
# Feature tables are created per profile database, so a profile opened by an
# existing session cookie would otherwise never pick up tables added by a newer
# build. The set is process-local on purpose: it empties on restart, which is
# exactly when a redeployed image needs to re-run the initializers once.
_storage_initialization_lock = Lock()
_initialized_storage_keys: set[str] = set()
_logger = logging.getLogger(__name__)


def _root() -> Path:
    settings = get_base_settings()
    if settings.data_dir:
        return Path(settings.data_dir).expanduser().resolve() / "profiles"
    database_url = settings.database_url
    if database_url.startswith("sqlite:///"):
        database_path = Path(database_url.removeprefix("sqlite:///")).expanduser()
        if not database_path.is_absolute():
            # Anchored to the install directory. Resolving this against the process's
            # working directory is what created the stray profile registries that
            # still sit in `app/` and `frontend/`.
            database_path = install_root() / database_path
        return database_path.resolve().parent / "profiles"
    return (install_root() / "data" / "profiles").resolve()


def profiles_root() -> Path:
    """Where account profiles, guests, the registry and backups live."""

    return _root()


def _registry_path() -> Path:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    _restrict(root, directory=True)
    path = root / "registry.db"
    if path.exists():
        # The registry holds every account's password hash and session tokens.
        _restrict(path, directory=False)
        for suffix in ("-wal", "-shm"):
            beside = path.with_name(path.name + suffix)
            if beside.exists():
                _restrict(beside, directory=False)
    return path


def _connect_registry() -> sqlite3.Connection:
    conn = sqlite3.connect(_registry_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def initialize_profile_registry() -> None:
    # Session resolution runs for every authenticated API request.  SQLite DDL is
    # a write operation, so repeatedly running setup here can contend with the
    # actual chat and memory writes.  Initialise each registry database once per
    # process instead.
    registry_path = _registry_path()
    with _registry_initialization_lock:
        if registry_path in _initialized_registry_paths:
            return
        conn = _connect_registry()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_profiles (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    avatar_data TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_registry_migrations (
                    revision TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            _apply_profile_owner_migration(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_sessions (
                    token_hash TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES account_profiles(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_profile_sessions_profile_id "
                "ON profile_sessions(profile_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_unlock_attempts (
                    profile_id TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            _initialized_registry_paths.add(registry_path)
        finally:
            conn.close()


def _profile_owner_check_sql(column: str = "owner_id") -> str:
    compact = f"replace({column}, '-', '')"
    return (
        f"length({column}) = 36 AND {column} = lower({column}) "
        f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
        f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
        f"AND length({compact}) = 32 AND {compact} NOT GLOB '*[^0-9a-f]*'"
    )


def _owner_uuid_for_existing_profile(profile_id: str) -> str:
    try:
        return canonical_uuid(profile_id)
    except ValueError:
        return str(uuid.uuid4())


def _apply_profile_owner_migration(conn: sqlite3.Connection) -> None:
    applied = conn.execute(
        "SELECT 1 FROM profile_registry_migrations WHERE revision = ?",
        (PROFILE_OWNER_REVISION,),
    ).fetchone()
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(account_profiles)").fetchall()
    }
    if applied is not None:
        if "owner_id" not in columns:
            raise RuntimeError("profile_owner_migration_ledger_schema_mismatch")
        _validate_profile_owner_rows(conn)
        return

    if "owner_id" not in columns:
        rows = conn.execute("SELECT * FROM account_profiles ORDER BY id").fetchall()
        conn.execute("ALTER TABLE account_profiles RENAME TO account_profiles_before_owner_uuid")
        conn.execute(
            f"""
            CREATE TABLE account_profiles (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL UNIQUE CHECK ({_profile_owner_check_sql()}),
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                avatar_data TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        used_owners: set[str] = set()
        for row in rows:
            owner_id = _owner_uuid_for_existing_profile(str(row["id"]))
            if owner_id in used_owners:
                owner_id = str(uuid.uuid4())
            used_owners.add(owner_id)
            conn.execute(
                """
                INSERT INTO account_profiles (
                    id, owner_id, username, password_salt, password_hash,
                    avatar_data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    owner_id,
                    row["username"],
                    row["password_salt"],
                    row["password_hash"],
                    row["avatar_data"],
                    row["created_at"],
                ),
            )
        conn.execute("DROP TABLE account_profiles_before_owner_uuid")
    else:
        rows = conn.execute("SELECT id, owner_id FROM account_profiles ORDER BY id").fetchall()
        used_owners: set[str] = set()
        for row in rows:
            try:
                owner_id = canonical_uuid(row["owner_id"])
            except (TypeError, ValueError):
                owner_id = _owner_uuid_for_existing_profile(str(row["id"]))
            if owner_id in used_owners:
                owner_id = str(uuid.uuid4())
            used_owners.add(owner_id)
            conn.execute(
                "UPDATE account_profiles SET owner_id = ? WHERE id = ?",
                (owner_id, row["id"]),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_profiles_owner_id "
            "ON account_profiles(owner_id)"
        )

    _validate_profile_owner_rows(conn)
    conn.execute(
        "INSERT INTO profile_registry_migrations (revision, applied_at) "
        "VALUES (?, CURRENT_TIMESTAMP)",
        (PROFILE_OWNER_REVISION,),
    )


def _validate_profile_owner_rows(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, owner_id FROM account_profiles").fetchall()
    seen: set[str] = set()
    for row in rows:
        owner_id = canonical_uuid(row["owner_id"])
        if owner_id != row["owner_id"] or owner_id in seen:
            raise RuntimeError("invalid_or_duplicate_profile_owner_uuid")
        seen.add(owner_id)


def _profile_directory(profile_id: str, *, guest: bool = False) -> Path:
    section = "guests" if guest else "accounts"
    return _root() / section / profile_id


def _restrict(path: Path, *, directory: bool) -> None:
    """Take away other-account access, leaving the owner's (and a container's group).

    Applied where Neo creates its own storage. SQLite makes its database and the
    ``-wal``/``-shm`` beside it with the process umask, which on a normal account is
    0644 -- readable by every other account on the machine. That is worth closing on
    its own, before any agent is in the picture.
    """

    mode = private_dir_mode() if directory else private_file_mode()
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH):
            path.chmod(mode)
    except OSError:
        # Not worth failing a request over; the directory above is already closed.
        pass


def restrict_profile_storage(profile_id: str, *, guest: bool = False) -> None:
    """Close other-account access across one profile's storage, in place.

    Re-run on every resolution rather than only at creation, because SQLite creates
    new ``-wal`` and ``-shm`` files as it goes and each arrives with the umask.
    """

    directory = _profile_directory(profile_id, guest=guest)
    if not directory.exists():
        return
    _restrict(directory, directory=True)
    try:
        entries = list(directory.rglob("*"))
    except OSError:
        return
    for path in entries:
        try:
            _restrict(path, directory=path.is_dir())
        except OSError:
            continue


def profile_storage_dir(profile_id: str, *, guest: bool = False) -> Path:
    """One profile's private storage directory, created if it does not exist.

    Public because things outside this module legitimately need it -- the memory
    keyring lives here, and so does the isolation policy's list of protected roots.
    """

    directory = _profile_directory(profile_id, guest=guest)
    directory.mkdir(parents=True, exist_ok=True)
    _restrict(directory, directory=True)
    return directory


def database_url_for(profile_id: str, *, guest: bool = False) -> str:
    directory = _profile_directory(profile_id, guest=guest)
    directory.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{directory / 'neo.db'}"


@contextmanager
def profile_database(profile_id: str, *, guest: bool = False):
    directory = _profile_directory(profile_id, guest=guest)
    database_token = active_profile_database_url.set(database_url_for(profile_id, guest=guest))
    storage_token = active_profile_storage_dir.set(str(directory))
    try:
        yield
    finally:
        active_profile_storage_dir.reset(storage_token)
        active_profile_database_url.reset(database_token)


def _validate_avatar(avatar_data: str | None) -> str | None:
    if not avatar_data:
        return None
    if not avatar_data.startswith("data:image/") or ";base64," not in avatar_data:
        raise HTTPException(status_code=422, detail="Profile picture must be an image file.")
    try:
        encoded = avatar_data.split(";base64,", 1)[1]
        if len(base64.b64decode(encoded, validate=True)) > MAX_AVATAR_BYTES:
            raise HTTPException(status_code=422, detail="Profile picture must be 2 MB or smaller.")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Profile picture is not valid image data."
        ) from exc
    return avatar_data


def _password_parts(password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def _verify_password(password: str, salt: str, digest: str) -> bool:
    computed = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(salt), PASSWORD_ITERATIONS
    )
    return hmac.compare_digest(base64.b64encode(computed).decode(), digest)


def public_profile(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "username": row["username"],
        "avatar_data": row["avatar_data"],
        "is_guest": False,
    }


def listed_profile(row: sqlite3.Row | dict) -> dict:
    """What the profile picker may show before anyone has signed in.

    Narrower than :func:`public_profile` on purpose. That one builds the session
    dict Neo's own code reads, which needs ``owner_id``; this one feeds an
    *unauthenticated* endpoint, and ``owner_id`` is the per-profile identifier the
    old memory key was derived from. It has no use in the picker, so it is not sent.
    """

    return {
        "id": row["id"],
        "username": row["username"],
        "avatar_data": row["avatar_data"],
        "is_guest": False,
    }


def list_profiles() -> list[dict]:
    """Every local account, as the sign-in picker sees them."""

    initialize_profile_registry()
    conn = _connect_registry()
    try:
        rows = conn.execute(
            "SELECT id, username, avatar_data FROM account_profiles "
            "ORDER BY username COLLATE NOCASE"
        ).fetchall()
        return [listed_profile(row) for row in rows]
    finally:
        conn.close()


def create_profile(username: str, password: str, avatar_data: str | None = None) -> dict:
    initialize_profile_registry()
    username = _normalize_username(username)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail="Password cannot be empty.",
        )
    profile_id = str(uuid.uuid4())
    owner_id = canonical_uuid(profile_id)
    salt, digest = _password_parts(password)
    avatar_data = _validate_avatar(avatar_data)
    conn = _connect_registry()
    try:
        conn.execute(
            "INSERT INTO account_profiles "
            "(id, owner_id, username, password_salt, password_hash, avatar_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (profile_id, owner_id, username, salt, digest, avatar_data),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="That username is already in use on this device."
        ) from exc
    finally:
        conn.close()
    ensure_profile_storage(profile_id)
    return {
        "id": profile_id,
        "owner_id": owner_id,
        "username": username,
        "avatar_data": avatar_data,
        "is_guest": False,
    }


def _normalize_username(username: str) -> str:
    username = " ".join(username.split())
    if not username:
        raise HTTPException(status_code=422, detail="Username is required.")
    if len(username) > 48:
        raise HTTPException(status_code=422, detail="Username must be 48 characters or fewer.")
    return username


def update_profile_account(
    profile_id: str,
    *,
    username: str | None = None,
    avatar_data: str | None = None,
    clear_avatar: bool = False,
    current_password: str | None = None,
    new_password: str | None = None,
) -> dict:
    """Update one local account's display details or password.

    A password change always re-verifies the current password; renaming or changing the
    picture only needs the caller to already hold that profile's session.
    """
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        row = conn.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That profile no longer exists.")

        updates: dict[str, str | None] = {}

        if username is not None:
            updates["username"] = _normalize_username(username)

        if clear_avatar:
            updates["avatar_data"] = None
        elif avatar_data is not None:
            updates["avatar_data"] = _validate_avatar(avatar_data)

        if new_password is not None:
            if not current_password or not _verify_password(
                current_password, row["password_salt"], row["password_hash"]
            ):
                raise HTTPException(
                    status_code=401, detail="That password does not match this profile."
                )
            if len(new_password) < MIN_PASSWORD_LENGTH:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Password cannot be empty."
                    ),
                )
            # Memory keys used to be derived from the password hash below, so the
            # old seed has to be on the keyring *before* the hash is replaced --
            # afterwards it is gone, and with it every encrypted record and every
            # tombstone written under it. Done first and allowed to fail the
            # request: losing the user's memory is worse than refusing a rename.
            _preserve_memory_key_material(profile_id, row)
            salt, digest = _password_parts(new_password)
            updates["password_salt"] = salt
            updates["password_hash"] = digest

        if not updates:
            return public_profile(row)

        assignments = ", ".join(f"{column} = ?" for column in updates)
        try:
            conn.execute(
                f"UPDATE account_profiles SET {assignments} WHERE id = ?",
                (*updates.values(), profile_id),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409, detail="That username is already in use on this device."
            ) from exc

        updated = conn.execute(
            "SELECT * FROM account_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return public_profile(updated)
    finally:
        conn.close()


def _lockout_seconds(failures: int) -> int:
    """Exponential back-off past the attempt limit, capped."""

    over = max(0, failures - UNLOCK_ATTEMPT_LIMIT)
    return min(UNLOCK_LOCKOUT_MAX_SECONDS, UNLOCK_LOCKOUT_BASE_SECONDS * (2**over))


def check_unlock_allowed(profile_id: str) -> None:
    """Refuse a password check while this profile is locked out.

    Persisted in the registry rather than in memory: a throttle a restart clears is
    no throttle at all, and anything that can reach the endpoint can also restart
    the process on a local install.
    """

    initialize_profile_registry()
    conn = _connect_registry()
    try:
        row = conn.execute(
            "SELECT failures, locked_until,"
            " CAST((julianday(locked_until) - julianday('now')) * 86400 AS INTEGER) AS remaining"
            " FROM profile_unlock_attempts WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["locked_until"]:
        return
    remaining = int(row["remaining"] or 0)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "too_many_attempts",
                "message": (
                    "Too many incorrect passwords. Try again in "
                    f"{max(1, remaining)} seconds."
                ),
                "retry_after_seconds": max(1, remaining),
            },
            headers={"Retry-After": str(max(1, remaining))},
        )


def _record_unlock_failure(profile_id: str) -> None:
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        row = conn.execute(
            "SELECT failures FROM profile_unlock_attempts WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        failures = int(row["failures"] if row else 0) + 1
        locked_until = None
        if failures >= UNLOCK_ATTEMPT_LIMIT:
            locked_until = f"+{_lockout_seconds(failures)} seconds"
        conn.execute(
            "INSERT INTO profile_unlock_attempts (profile_id, failures, locked_until, updated_at)"
            " VALUES (?, ?, datetime('now', ?), CURRENT_TIMESTAMP)"
            " ON CONFLICT(profile_id) DO UPDATE SET failures = excluded.failures,"
            " locked_until = excluded.locked_until, updated_at = CURRENT_TIMESTAMP",
            (profile_id, failures, locked_until or "+0 seconds"),
        )
        if locked_until is None:
            conn.execute(
                "UPDATE profile_unlock_attempts SET locked_until = NULL WHERE profile_id = ?",
                (profile_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _clear_unlock_failures(profile_id: str) -> None:
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        conn.execute("DELETE FROM profile_unlock_attempts WHERE profile_id = ?", (profile_id,))
        conn.commit()
    finally:
        conn.close()


def reset_unlock_throttle() -> None:
    """Clear every recorded failure. For tests and for an administrator's recovery."""

    try:
        initialize_profile_registry()
    except Exception:
        return
    conn = _connect_registry()
    try:
        conn.execute("DELETE FROM profile_unlock_attempts")
        conn.commit()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()


def authenticate(profile_id: str, password: str) -> dict:
    initialize_profile_registry()
    check_unlock_allowed(profile_id)
    conn = _connect_registry()
    try:
        row = conn.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,)).fetchone()
    finally:
        conn.close()
    if row is None or not _verify_password(password, row["password_salt"], row["password_hash"]):
        # Counted even for an unknown id, so probing for valid ids is throttled too.
        _record_unlock_failure(profile_id)
        raise HTTPException(status_code=401, detail="That password does not match this profile.")
    _clear_unlock_failures(profile_id)
    ensure_profile_storage(profile_id)
    return public_profile(row)


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_profile_session(profile: dict) -> str:
    """Create a durable opaque session for a password-protected local profile."""
    if profile.get("is_guest"):
        raise ValueError("guest_sessions_are_not_durable")
    initialize_profile_registry()
    token = secrets.token_urlsafe(PROFILE_SESSION_TOKEN_BYTES)
    conn = _connect_registry()
    try:
        conn.execute(
            "INSERT INTO profile_sessions (token_hash, profile_id) VALUES (?, ?)",
            (_session_token_hash(token), str(profile["id"])),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def profile_for_session(token: str) -> dict | None:
    if not token or len(token) > 512:
        return None
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        token_hash = _session_token_hash(token)
        row = conn.execute(
            """
            SELECT p.id, p.owner_id, p.username, p.avatar_data,
                   CAST(julianday('now') - julianday(s.created_at) AS REAL) AS age_days,
                   CAST(julianday('now') - julianday(s.last_seen_at) AS REAL) AS idle_days
            FROM profile_sessions AS s
            JOIN account_profiles AS p ON p.id = s.profile_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is not None:
            # An expired cookie is deleted on the way past, so a stolen one stops
            # working rather than merely being ignored by this request.
            if (row["age_days"] or 0) > SESSION_ABSOLUTE_DAYS or (
                row["idle_days"] or 0
            ) > SESSION_IDLE_DAYS:
                conn.execute("DELETE FROM profile_sessions WHERE token_hash = ?", (token_hash,))
                conn.commit()
                return None
            conn.execute(
                "UPDATE profile_sessions SET last_seen_at = CURRENT_TIMESTAMP"
                " WHERE token_hash = ?",
                (token_hash,),
            )
            conn.commit()
    finally:
        conn.close()
    if row is None:
        return None
    # A cookie outlives a deploy, so this is the only path that runs for a user
    # who never signs in again; without it their profile database keeps whatever
    # schema it had when the session started.
    ensure_profile_storage(str(row["id"]))
    return public_profile(row)


def revoke_profile_session(token: str) -> None:
    if not token:
        return
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        conn.execute(
            "DELETE FROM profile_sessions WHERE token_hash = ?", (_session_token_hash(token),)
        )
        conn.commit()
    finally:
        conn.close()


def revoke_profile_sessions(profile_id: str) -> None:
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        conn.execute("DELETE FROM profile_sessions WHERE profile_id = ?", (profile_id,))
        conn.commit()
    finally:
        conn.close()


def create_guest() -> dict:
    profile_id = f"guest-{uuid.uuid4()}"
    owner_id = str(uuid.uuid4())
    directory = _profile_directory(profile_id, guest=True)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "owner_id").write_text(owner_id, encoding="utf-8")
    ensure_profile_storage(profile_id, guest=True)
    return {
        "id": profile_id,
        "owner_id": owner_id,
        "username": "Guest",
        "avatar_data": None,
        "is_guest": True,
    }


def owner_id_for_profile(profile_id: str, *, guest: bool = False) -> str:
    if guest:
        path = _profile_directory(profile_id, guest=True) / "owner_id"
        try:
            return canonical_uuid(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("guest_profile_owner_id_unavailable") from exc

    initialize_profile_registry()
    conn = _connect_registry()
    try:
        row = conn.execute(
            "SELECT owner_id FROM account_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError("profile_owner_id_not_found")
    return canonical_uuid(row["owner_id"])


def validate_profile_owner_pair(profile_id: str, owner_id: str, *, guest: bool = False) -> str:
    expected = owner_id_for_profile(profile_id, guest=guest)
    supplied = canonical_uuid(owner_id)
    if supplied != expected:
        raise RuntimeError("profile_owner_id_mismatch")
    return supplied


def database_identity_for_profile(profile_id: str, *, guest: bool = False) -> str:
    prefix = "guest-profile" if guest else "account-profile"
    return f"{prefix}:{profile_id}"


def _preserve_memory_key_material(profile_id: str, row: sqlite3.Row | dict) -> None:
    """Put the currently derived memory seed on the profile's keyring.

    Called immediately before a password change. Creating the keyring here also
    migrates a profile that never had one: its active key becomes random from this
    point on, while the derived seed stays available for reading what came before.
    """

    from app.services.memory import keyring as memory_keyring

    seed = _derive_memory_seed(f"{row['owner_id']}:{row['password_hash']}")
    try:
        memory_keyring.ensure_keyring(
            profile_storage_dir(profile_id),
            legacy_seed=seed,
            legacy_source="derived:password_hash",
        )
    except memory_keyring.KeyringError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Your password was not changed: Neo could not safely preserve the key "
                "that reads your existing memories. Nothing has been modified."
            ),
        ) from exc


def _derive_memory_seed(material: str) -> bytes:
    return hashlib.sha256(f"neo-memory:{material}".encode()).digest()


def memory_key_material_for_profile(profile_id: str, *, guest: bool = False) -> bytes:
    """Derive the legacy local key material for one profile.

    Retained because it is the only way to reconstruct the seed that existing rows
    and tombstones were written under. New key material comes from the profile's
    keyring (``app.services.memory.keyring``), not from here.
    """

    if guest:
        material = owner_id_for_profile(profile_id, guest=True)
    else:
        initialize_profile_registry()
        conn = _connect_registry()
        try:
            row = conn.execute(
                "SELECT owner_id, password_hash FROM account_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise RuntimeError("profile_memory_key_material_not_found")
        material = f"{row['owner_id']}:{row['password_hash']}"
    return hashlib.sha256(f"neo-memory:{material}".encode()).digest()


def delete_guest(profile_id: str) -> None:
    if profile_id.startswith("guest-"):
        shutil.rmtree(_profile_directory(profile_id, guest=True), ignore_errors=True)


def delete_profile(profile_id: str, password: str) -> dict:
    """Permanently remove one password-confirmed local account and its private data."""
    initialize_profile_registry()
    # Throttled on the same counter as unlock: this endpoint takes the same password
    # and so is the same guessing oracle, with destruction rather than a session at
    # the end of it.
    check_unlock_allowed(profile_id)
    conn = _connect_registry()
    try:
        row = conn.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None or not _verify_password(
            password, row["password_salt"], row["password_hash"]
        ):
            _record_unlock_failure(profile_id)
            raise HTTPException(
                status_code=401,
                detail="That password does not match this profile.",
            )

        # The identifier comes from the registry row, rather than the request path, before it is
        # ever used as a filesystem component. This keeps removal confined to this account.
        directory = _profile_directory(row["id"])
        accounts_root = (_root() / "accounts").resolve()
        if not directory.resolve().is_relative_to(accounts_root):
            raise HTTPException(status_code=400, detail="Invalid profile storage location.")
        if directory.exists():
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail="Neo could not remove this profile's local data. The account was kept.",
                ) from exc

        conn.execute("DELETE FROM profile_sessions WHERE profile_id = ?", (row["id"],))
        conn.execute("DELETE FROM account_profiles WHERE id = ?", (row["id"],))
        conn.commit()
        forget_profile_storage(str(row["id"]))
        return public_profile(row)
    finally:
        conn.close()


def cleanup_guests() -> None:
    shutil.rmtree(_root() / "guests", ignore_errors=True)


def ensure_profile_storage(profile_id: str, *, guest: bool = False) -> None:
    """Initialise the tables used by every Neo feature in one profile database.

    Memoised per process, so it is cheap enough to call on every session
    resolution rather than only at signup and password login.
    """

    key = f"{'guest' if guest else 'account'}:{profile_id}"
    with _storage_initialization_lock:
        if key in _initialized_storage_keys:
            return
        _initialize_profile_storage(profile_id, guest=guest)
        restrict_profile_storage(profile_id, guest=guest)
        _initialized_storage_keys.add(key)


def forget_profile_storage(profile_id: str, *, guest: bool = False) -> None:
    """Drop the memo so a recreated profile re-runs the initializers."""

    key = f"{'guest' if guest else 'account'}:{profile_id}"
    with _storage_initialization_lock:
        _initialized_storage_keys.discard(key)


def _initialize_profile_storage(profile_id: str, *, guest: bool = False) -> None:
    with profile_database(profile_id, guest=guest):
        initialize_database(get_settings().database_url)
        # The feature stores use get_settings(), which is profile-aware inside this context.
        from app.services.agent_core.store import initialize_agent_core_tables
        from app.services.agent_framework import initialize_agent_framework_tables
        from app.services.appearance import initialize_appearance_tables
        from app.services.bundles import initialize_bundle_tables
        from app.services.calendar import initialize_calendar_tables
        from app.services.chat_prefs import initialize_chat_preference_tables
        from app.services.command_sandbox import initialize_command_sandbox_tables
        from app.services.context_memory import initialize_context_memory_tables
        from app.services.continuity import initialize_continuity_tables
        from app.services.evaluation import initialize_evaluation_tables
        from app.services.files.store import initialize_workspace_file_tables
        from app.services.gallery.store import initialize_gallery_tables
        from app.services.git.store import initialize_git_tables
        from app.services.github import initialize_github_tables
        from app.services.integrations.store import initialize_integration_tables
        from app.services.keybindings import initialize_keybinding_tables
        from app.services.llm_registry.service import LLMRegistryService
        from app.services.llm_registry.store import initialize_llm_registry_tables
        from app.services.lsp import initialize_lsp_tables
        from app.services.memory_retrieval import initialize_memory_retrieval_tables
        from app.services.notes.store import initialize_notes_tables
        from app.services.projects.store import initialize_project_tables
        from app.services.provider_runtime import initialize_provider_runtime_tables
        from app.services.research.jobs import recover_interrupted_jobs
        from app.services.research.store import initialize_research_tables
        from app.services.rules.store import initialize_rule_tables
        from app.services.sidebar_nav import initialize_sidebar_nav_tables
        from app.services.skills.store import initialize_skill_tables
        from app.services.tasks.store import initialize_task_tables
        from app.services.test_runner.store import initialize_test_runner_tables
        from app.services.web_search import initialize_web_search_tables
        from app.services.workspace_orchestration import initialize_workspace_orchestration_tables

        for initializer in (
            initialize_notes_tables,
            initialize_project_tables,
            initialize_task_tables,
            initialize_calendar_tables,
            initialize_agent_core_tables,
            initialize_bundle_tables,
            initialize_agent_framework_tables,
            initialize_command_sandbox_tables,
            initialize_context_memory_tables,
            initialize_memory_retrieval_tables,
            initialize_research_tables,
            recover_interrupted_jobs,
            initialize_workspace_file_tables,
            initialize_gallery_tables,
            initialize_chat_preference_tables,
            initialize_keybinding_tables,
            initialize_appearance_tables,
            initialize_sidebar_nav_tables,
            initialize_test_runner_tables,
            initialize_git_tables,
            initialize_github_tables,
            initialize_integration_tables,
            initialize_llm_registry_tables,
            initialize_provider_runtime_tables,
            initialize_lsp_tables,
            initialize_rule_tables,
            initialize_skill_tables,
            initialize_web_search_tables,
            initialize_workspace_orchestration_tables,
            initialize_continuity_tables,
            initialize_evaluation_tables,
        ):
            initializer()
        # A managed repository used to live beside the profile database. Copy
        # it into the code-only root before any external run can use it. A bad
        # copy leaves the original and its database pointer intact; the rest
        # of the profile must remain accessible so the user can recover it.
        from app.services.repos.migration import migrate_managed_workspaces

        migration = migrate_managed_workspaces(profile_id)
        if migration["failed"]:
            _logger.warning(
                "Could not migrate %d managed repositories for a profile",
                len(migration["failed"]),
            )
        # A migration rather than an initializer, and it has to run here: every
        # profile has its own database, so a one-shot cleanup wired only into
        # application startup would reach the base database and no one's actual
        # data. Guarded by its own marker, so it still runs once per profile.
        from app.services.agent_core.store import delete_chatless_sessions

        delete_chatless_sessions()
        # Seed/reconcile provider defaults while profile storage is being
        # initialized, before any chat worker opens a transaction. Runtime
        # provider selection is deliberately read-only.
        LLMRegistryService(initialize=False).ensure_defaults()

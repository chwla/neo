"""Local account registry and profile-scoped storage for Neo.

The registry intentionally stores only local credentials and public profile
metadata. Each profile gets its own SQLite database, which keeps saved chats,
memories, and workspace settings separate without needing an online account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from app.core.config import (
    active_profile_database_url,
    active_profile_storage_dir,
    get_base_settings,
    get_settings,
)
from app.db.session import initialize_database

MAX_AVATAR_BYTES = 2 * 1024 * 1024
PASSWORD_ITERATIONS = 390_000


def _root() -> Path:
    settings = get_base_settings()
    if settings.data_dir:
        return Path(settings.data_dir).expanduser().resolve() / "profiles"
    database_url = settings.database_url
    if database_url.startswith("sqlite:///"):
        database_path = Path(database_url.removeprefix("sqlite:///"))
        return database_path.expanduser().resolve().parent / "profiles"
    return Path("data/profiles").resolve()


def _registry_path() -> Path:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    return root / "registry.db"


def _connect_registry() -> sqlite3.Connection:
    conn = sqlite3.connect(_registry_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize_profile_registry() -> None:
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
        conn.commit()
    finally:
        conn.close()


def _profile_directory(profile_id: str, *, guest: bool = False) -> Path:
    section = "guests" if guest else "accounts"
    return _root() / section / profile_id


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
        "username": row["username"],
        "avatar_data": row["avatar_data"],
        "is_guest": False,
    }


def list_profiles() -> list[dict]:
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        rows = conn.execute(
            "SELECT id, username, avatar_data FROM account_profiles "
            "ORDER BY username COLLATE NOCASE"
        ).fetchall()
        return [public_profile(row) for row in rows]
    finally:
        conn.close()


def create_profile(username: str, password: str, avatar_data: str | None = None) -> dict:
    initialize_profile_registry()
    username = " ".join(username.split())
    if not username:
        raise HTTPException(status_code=422, detail="Username is required.")
    if len(username) > 48:
        raise HTTPException(status_code=422, detail="Username must be 48 characters or fewer.")
    if len(password) < 4:
        raise HTTPException(status_code=422, detail="Password must contain at least 4 characters.")
    profile_id = str(uuid.uuid4())
    salt, digest = _password_parts(password)
    avatar_data = _validate_avatar(avatar_data)
    conn = _connect_registry()
    try:
        conn.execute(
            "INSERT INTO account_profiles "
            "(id, username, password_salt, password_hash, avatar_data) "
            "VALUES (?, ?, ?, ?, ?)",
            (profile_id, username, salt, digest, avatar_data),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="That username is already in use on this device."
        ) from exc
    finally:
        conn.close()
    ensure_profile_storage(profile_id)
    return {"id": profile_id, "username": username, "avatar_data": avatar_data, "is_guest": False}


def authenticate(profile_id: str, password: str) -> dict:
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        row = conn.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,)).fetchone()
    finally:
        conn.close()
    if row is None or not _verify_password(password, row["password_salt"], row["password_hash"]):
        raise HTTPException(status_code=401, detail="That password does not match this profile.")
    ensure_profile_storage(profile_id)
    return public_profile(row)


def create_guest() -> dict:
    profile_id = f"guest-{uuid.uuid4()}"
    ensure_profile_storage(profile_id, guest=True)
    return {"id": profile_id, "username": "Guest", "avatar_data": None, "is_guest": True}


def delete_guest(profile_id: str) -> None:
    if profile_id.startswith("guest-"):
        shutil.rmtree(_profile_directory(profile_id, guest=True), ignore_errors=True)


def delete_profile(profile_id: str, password: str) -> dict:
    """Permanently remove one password-confirmed local account and its private data."""
    initialize_profile_registry()
    conn = _connect_registry()
    try:
        row = conn.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None or not _verify_password(
            password, row["password_salt"], row["password_hash"]
        ):
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

        conn.execute("DELETE FROM account_profiles WHERE id = ?", (row["id"],))
        conn.commit()
        return public_profile(row)
    finally:
        conn.close()


def cleanup_guests() -> None:
    shutil.rmtree(_root() / "guests", ignore_errors=True)


def ensure_profile_storage(profile_id: str, *, guest: bool = False) -> None:
    """Initialise the tables used by every Neo feature in one profile database."""

    with profile_database(profile_id, guest=guest):
        initialize_database(get_settings().database_url)
        # The feature stores use get_settings(), which is profile-aware inside this context.
        from app.services.agent_framework import initialize_agent_framework_tables
        from app.services.agentic_core import initialize_agentic_core_tables
        from app.services.agents.store import initialize_agent_tables
        from app.services.bundles import initialize_bundle_tables
        from app.services.coding_agent.store import initialize_coding_agent_tables
        from app.services.command_sandbox import initialize_command_sandbox_tables
        from app.services.context_memory import initialize_context_memory_tables
        from app.services.continuity import initialize_continuity_tables
        from app.services.files.store import initialize_workspace_file_tables
        from app.services.git.store import initialize_git_tables
        from app.services.github import initialize_github_tables
        from app.services.llm_registry.store import initialize_llm_registry_tables
        from app.services.lsp import initialize_lsp_tables
        from app.services.memory_retrieval import initialize_memory_retrieval_tables
        from app.services.notes.store import initialize_notes_tables
        from app.services.projects.store import initialize_project_tables
        from app.services.provider_runtime import initialize_provider_runtime_tables
        from app.services.recovery import initialize_recovery_tables
        from app.services.research.store import initialize_research_tables
        from app.services.research_mode import initialize_research_mode_tables
        from app.services.rules.store import initialize_rule_tables
        from app.services.tasks.store import initialize_task_tables
        from app.services.test_runner.store import initialize_test_runner_tables
        from app.services.tools import initialize_tool_tables
        from app.services.web_search import initialize_web_search_tables
        from app.services.workspace_orchestration import initialize_workspace_orchestration_tables

        for initializer in (
            initialize_notes_tables,
            initialize_project_tables,
            initialize_task_tables,
            initialize_agent_tables,
            initialize_bundle_tables,
            initialize_tool_tables,
            initialize_agent_framework_tables,
            initialize_agentic_core_tables,
            initialize_coding_agent_tables,
            initialize_command_sandbox_tables,
            initialize_context_memory_tables,
            initialize_memory_retrieval_tables,
            initialize_research_tables,
            initialize_research_mode_tables,
            initialize_workspace_file_tables,
            initialize_test_runner_tables,
            initialize_git_tables,
            initialize_github_tables,
            initialize_llm_registry_tables,
            initialize_provider_runtime_tables,
            initialize_lsp_tables,
            initialize_rule_tables,
            initialize_recovery_tables,
            initialize_web_search_tables,
            initialize_workspace_orchestration_tables,
            initialize_continuity_tables,
        ):
            initializer()

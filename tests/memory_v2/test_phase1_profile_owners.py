from __future__ import annotations

import sqlite3

import pytest

from app.core.identifiers import canonical_uuid
from app.services import profile_accounts


def _use_profile_root(monkeypatch, tmp_path) -> None:
    root = tmp_path / "profiles"
    monkeypatch.setattr(profile_accounts, "_root", lambda: root)
    monkeypatch.setattr(profile_accounts, "ensure_profile_storage", lambda *args, **kwargs: None)


def _legacy_registry(path, *, profile_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE account_profiles (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                avatar_data TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO account_profiles "
            "(id, username, password_salt, password_hash) VALUES (?, 'Legacy', 'salt', 'hash')",
            (profile_id,),
        )
        connection.commit()
    finally:
        connection.close()


def test_new_profile_gets_stable_uuid_owner(monkeypatch, tmp_path) -> None:
    _use_profile_root(monkeypatch, tmp_path)
    profile = profile_accounts.create_profile("Alice", "pass123")
    assert profile["owner_id"] == canonical_uuid(profile["owner_id"])
    assert profile_accounts.owner_id_for_profile(profile["id"]) == profile["owner_id"]
    assert profile_accounts.list_profiles()[0]["owner_id"] == profile["owner_id"]


def test_existing_uuid_profile_receives_exactly_one_stable_owner(monkeypatch, tmp_path) -> None:
    _use_profile_root(monkeypatch, tmp_path)
    profile_id = "00000000-0000-4000-8000-000000000091"
    _legacy_registry(profile_accounts._registry_path(), profile_id=profile_id)

    profile_accounts.initialize_profile_registry()
    first = profile_accounts.owner_id_for_profile(profile_id)
    profile_accounts.initialize_profile_registry()
    second = profile_accounts.owner_id_for_profile(profile_id)

    assert first == second == profile_id
    connection = sqlite3.connect(profile_accounts._registry_path())
    try:
        count = connection.execute(
            "SELECT count(*) FROM profile_registry_migrations WHERE revision = ?",
            (profile_accounts.PROFILE_OWNER_REVISION,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_non_uuid_legacy_profile_gets_generated_owner_once(monkeypatch, tmp_path) -> None:
    _use_profile_root(monkeypatch, tmp_path)
    _legacy_registry(profile_accounts._registry_path(), profile_id="legacy-profile-id")
    profile_accounts.initialize_profile_registry()
    first = profile_accounts.owner_id_for_profile("legacy-profile-id")
    profile_accounts.initialize_profile_registry()
    assert profile_accounts.owner_id_for_profile("legacy-profile-id") == first
    assert canonical_uuid(first) == first


def test_profile_rename_does_not_change_owner_id(monkeypatch, tmp_path) -> None:
    _use_profile_root(monkeypatch, tmp_path)
    profile = profile_accounts.create_profile("Before", "pass123")
    owner_before = profile["owner_id"]
    connection = sqlite3.connect(profile_accounts._registry_path())
    try:
        connection.execute(
            "UPDATE account_profiles SET username = 'After' WHERE id = ?", (profile["id"],)
        )
        connection.commit()
    finally:
        connection.close()
    assert profile_accounts.owner_id_for_profile(profile["id"]) == owner_before


def test_profile_owner_pair_validation_fails_closed(monkeypatch, tmp_path) -> None:
    _use_profile_root(monkeypatch, tmp_path)
    profile = profile_accounts.create_profile("Alice", "pass123")
    assert (
        profile_accounts.validate_profile_owner_pair(profile["id"], profile["owner_id"])
        == profile["owner_id"]
    )
    with pytest.raises(RuntimeError, match="profile_owner_id_mismatch"):
        profile_accounts.validate_profile_owner_pair(
            profile["id"], "00000000-0000-4000-8000-000000000099"
        )


def test_guest_owner_uuid_is_ephemeral_but_stable_for_guest_lifetime(monkeypatch, tmp_path) -> None:
    _use_profile_root(monkeypatch, tmp_path)
    guest = profile_accounts.create_guest()
    assert canonical_uuid(guest["owner_id"]) == guest["owner_id"]
    assert profile_accounts.owner_id_for_profile(guest["id"], guest=True) == guest["owner_id"]


def test_corrupt_applied_registry_schema_fails_safely(monkeypatch, tmp_path) -> None:
    _use_profile_root(monkeypatch, tmp_path)
    _legacy_registry(
        profile_accounts._registry_path(),
        profile_id="00000000-0000-4000-8000-000000000091",
    )
    connection = sqlite3.connect(profile_accounts._registry_path())
    try:
        connection.execute(
            "CREATE TABLE profile_registry_migrations "
            "(revision TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO profile_registry_migrations VALUES (?, CURRENT_TIMESTAMP)",
            (profile_accounts.PROFILE_OWNER_REVISION,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="profile_owner_migration_ledger_schema_mismatch"):
        profile_accounts.initialize_profile_registry()

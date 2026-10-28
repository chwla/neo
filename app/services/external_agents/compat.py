"""Which engines have been validated here, and whether that is still true.

An engine is not trustworthy because it is installed. It is trustworthy because
someone ran it, on this machine, under this policy, and watched it work: that it
authenticates without Neo handling its credentials, that it reads its prompt from
stdin, that its writes stay inside the directories it was given, that a resume
reaches its own transcript and not another thread's.

That observation is a **compatibility record**. It is bound to the exact things that
would make it stale, and the launcher checks it on every entry point -- a turn, a
retry, a chain step, a resume, a background helper -- so there is no path that runs
an engine nobody has checked.

What invalidates a record: the engine's verified content, the resolved launcher
chain, the adapter, the policy, the OS version. Metadata like size and timestamp is
*not* identity, so the record stores content hashes and
``executables.Package.matches_source`` is what answers "has the install changed".

Cursor and Antigravity have no record and cannot get one here, because neither
binary exists on this machine. They are refused with that as the reason, rather than
being offered and failing later in a way that looks like a bug.
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.services.isolation.roots import roots

#: Bumped when a change to the adapter or the policy generator means an older
#: observation no longer describes what Neo will now do.
ADAPTER_VERSION = 1


class NotValidated(RuntimeError):
    """This engine has no current compatibility record, so it will not be run."""


@dataclass(frozen=True)
class Record:
    executor: str
    platform: str
    os_version: str
    adapter_version: int
    package_hash: str
    source_path: str
    policy_hash: str
    validated_at: str
    notes: str = ""

    def differences(self, current: Record) -> list[str]:
        """What has changed since this record was written, in the user's terms."""

        changed: list[str] = []
        if self.package_hash != current.package_hash:
            changed.append("the engine's program files changed")
        if self.source_path != current.source_path:
            changed.append("the engine is installed somewhere else now")
        if self.adapter_version != current.adapter_version:
            changed.append("Neo's own handling of this engine changed")
        if self.policy_hash != current.policy_hash:
            changed.append("Neo's isolation policy changed")
        if self.os_version != current.os_version:
            changed.append("this machine's operating system was updated")
        if self.platform != current.platform:
            changed.append("this is a different kind of machine")
        return changed


def _store_path() -> Path:
    base = roots().data_root / "isolation"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base / "engine-compatibility.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_store_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_compatibility (
            executor TEXT PRIMARY KEY,
            record_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def os_version() -> str:
    return platform.platform()


def current(executor: str, *, package_hash: str, source_path: str, policy_hash: str) -> Record:
    """What a record for this engine would say if it were written right now."""

    import sys

    return Record(
        executor=executor,
        platform=sys.platform,
        os_version=os_version(),
        adapter_version=ADAPTER_VERSION,
        package_hash=package_hash,
        source_path=source_path,
        policy_hash=policy_hash,
        validated_at=datetime.now(UTC).isoformat(),
    )


def load(executor: str) -> Record | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT record_json FROM engine_compatibility WHERE executor = ?", (executor,)
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return Record(**json.loads(row["record_json"]))
    except (ValueError, TypeError):
        # A record we cannot read is not a record. Revalidation is the answer, and
        # it is cheap; guessing at a partial one is not.
        return None


def save(record: Record) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO engine_compatibility (executor, record_json, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(executor) DO UPDATE SET record_json = excluded.record_json,"
            " updated_at = excluded.updated_at",
            (record.executor, json.dumps(asdict(record)), datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def forget(executor: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM engine_compatibility WHERE executor = ?", (executor,))
        conn.commit()
    finally:
        conn.close()


def require(
    executor: str, *, package_hash: str, source_path: str, policy_hash: str
) -> Record:
    """Return the current record, or raise with what the user has to do about it."""

    stored = load(executor)
    if stored is None:
        raise NotValidated(
            f"{executor} has not been checked on this machine yet. Validate it from "
            "Settings > Engines, which runs one harmless task and confirms the "
            "isolation holds."
        )
    now = current(
        executor, package_hash=package_hash, source_path=source_path, policy_hash=policy_hash
    )
    changed = stored.differences(now)
    if changed:
        raise NotValidated(
            f"{executor} was checked before, but "
            + ", and ".join(changed)
            + ". Validate it again from Settings > Engines before running it."
        )
    return stored


def describe(executor: str) -> dict:
    """The record as the Engines panel shows it."""

    stored = load(executor)
    if stored is None:
        return {"executor": executor, "validated": False, "reason": "never validated here"}
    return {
        "executor": executor,
        "validated": True,
        "validated_at": stored.validated_at,
        "os_version": stored.os_version,
        "adapter_version": stored.adapter_version,
    }


__all__ = [
    "ADAPTER_VERSION",
    "NotValidated",
    "Record",
    "current",
    "describe",
    "forget",
    "load",
    "require",
    "save",
]

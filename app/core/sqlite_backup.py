"""Consistent, verified copies of a SQLite database file.

SQLite's online backup API copies a live database page by page under its own
locking, so the copy is a consistent snapshot even while other connections
write. A copy is only worth keeping if it can be trusted later, so every one is
checked with ``PRAGMA integrity_check`` and fingerprinted before it is handed
back, and the file is readable by its owner alone: a backup holds everything the
original does.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SQLiteFileBackup:
    source_path: str
    destination_path: str
    sha256: str
    integrity_result: tuple[str, ...]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite_file(source: Path, destination: Path) -> SQLiteFileBackup:
    """Copy ``source`` to ``destination``, which must not exist yet.

    Raises when the copy fails its integrity check; the bad copy is removed so a
    later run never mistakes it for a good one.
    """

    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination == source:
        raise ValueError("backup_destination_must_differ_from_source")
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(destination.parent, 0o700)
    except OSError:
        pass

    source_conn = sqlite3.connect(source, timeout=30)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
        target_conn.commit()
        integrity = tuple(str(row[0]) for row in target_conn.execute("PRAGMA integrity_check"))
    finally:
        target_conn.close()
        source_conn.close()

    if integrity != ("ok",):
        destination.unlink(missing_ok=True)
        raise RuntimeError("backup_integrity_check_failed")
    os.chmod(destination, 0o600)
    return SQLiteFileBackup(
        source_path=str(source),
        destination_path=str(destination),
        sha256=file_sha256(destination),
        integrity_result=integrity,
    )

"""Copy legacy managed repositories out of private profile storage.

The old tree is never removed. A repository row is switched only after an
independently checked copy is durable, so a crash leaves either the old usable
path or a complete new path. Retrying finishes an interrupted switch.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import uuid
from contextlib import closing
from pathlib import Path

from app.core.config import active_profile_storage_dir, get_settings
from app.services.isolation.roots import overlaps_protected
from app.services.repos.safety import prepare_managed_root


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)


def _manifest(root: Path) -> dict[str, tuple[int, str]]:
    """Hash names and bytes, refusing links and special files in a code copy."""

    result: dict[str, tuple[int, str]] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in dirs + files:
            path = base / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                raise ValueError(f"link in managed workspace: {relative}")
            if stat.S_ISDIR(mode):
                result[relative] = (0, "directory")
                continue
            if not stat.S_ISREG(mode) or path.stat().st_nlink > 1:
                raise ValueError(f"unsafe file in managed workspace: {relative}")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result[relative] = (path.stat().st_size, digest.hexdigest())
    return result


def _fsync_tree(root: Path) -> None:
    for current, _dirs, files in os.walk(root):
        for name in files:
            with (Path(current) / name).open("rb") as stream:
                os.fsync(stream.fileno())
    for current, _dirs, _files in os.walk(root, topdown=False):
        fd = os.open(current, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _switch_record(repo_id: str, source: Path, destination: Path) -> None:
    """Update only the exact managed row that still names the legacy path."""

    db_url = get_settings().database_url
    if not db_url.startswith("sqlite:///"):
        raise RuntimeError("managed workspace migration requires a SQLite profile")
    db_path = Path(db_url.removeprefix("sqlite:///"))
    if not db_path.is_file():
        return  # an orphaned copy, not an attached repository
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_repos'"
            ).fetchone()
            if not table:
                return
            row = conn.execute(
                "SELECT workspace_path, access FROM workspace_repos WHERE id = ?", (repo_id,)
            ).fetchone()
            if row is None:
                return
            if row[1] != "managed" or row[0] not in {str(source), str(destination)}:
                raise RuntimeError("repository row does not match this managed copy")
            if row[0] == str(source):
                changed = conn.execute(
                    "UPDATE workspace_repos SET workspace_path = ? "
                    "WHERE id = ? AND workspace_path = ? AND access = 'managed'",
                    (str(destination), repo_id, str(source)),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("repository changed during migration")


def migrate_managed_workspaces(profile_id: str) -> dict[str, list]:
    """Migrate one active profile, preserving every original and reporting failures.

    This must run in ``profile_database(profile_id)``. A profile id is used only as
    a single path component and must match the active profile context.
    """

    result: dict[str, list] = {"migrated": [], "already_done": [], "failed": []}
    if not profile_id or Path(profile_id).name != profile_id or profile_id in {".", ".."}:
        raise ValueError("invalid profile id")
    storage = active_profile_storage_dir.get()
    if storage is None or Path(storage).name != profile_id:
        raise RuntimeError("managed workspace migration needs the active profile")
    old_root = Path(storage) / "workspace_repos"
    if not old_root.exists():
        return result
    if old_root.is_symlink() or not old_root.is_dir():
        result["failed"].append({"from": str(old_root), "error": "unsafe legacy root"})
        return result

    new_root = Path(get_settings().workspace_repos_dir)
    prepare_managed_root(new_root)
    for source in sorted(old_root.iterdir()):
        destination = new_root / source.name
        try:
            if source.is_symlink() or not source.is_dir():
                raise ValueError("legacy entry is not a plain directory")
            if overlaps_protected(destination, profile_id=profile_id) is not None:
                raise ValueError("destination is protected")
            before = _manifest(source)
            if destination.exists():
                if destination.is_symlink() or _manifest(destination) != before:
                    raise ValueError("destination exists with different contents")
                _switch_record(source.name, source, destination)
                result["already_done"].append({"from": str(source), "to": str(destination)})
                continue

            stage = new_root / f".migrate-{source.name}-{uuid.uuid4().hex}"
            try:
                _copy_tree(source, stage)
                if _manifest(source) != before or _manifest(stage) != before:
                    raise ValueError("copy does not match the original")
                _fsync_tree(stage)
                # rename is atomic on the same filesystem. A competing migration
                # that won the name leaves its copy for comparison on retry.
                try:
                    os.rename(stage, destination)
                except OSError:
                    # A second startup process may have finished the same copy
                    # after our initial existence check. Accept it only when
                    # every path and byte matches our verified source.
                    if (
                        destination.is_symlink()
                        or not destination.is_dir()
                        or _manifest(destination) != before
                    ):
                        raise
                parent_fd = os.open(new_root, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            _switch_record(source.name, source, destination)
            result["migrated"].append({"from": str(source), "to": str(destination)})
        except (OSError, RuntimeError, ValueError) as exc:
            result["failed"].append({"from": str(source), "error": str(exc)})
    return result

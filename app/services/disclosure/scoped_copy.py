"""A full, independent repository copy for a run receiving private context."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from app.services.isolation.roots import data_root, overlaps_protected
from app.services.repos.migration import _fsync_tree, _manifest
from app.services.repos.safety import prepare_managed_root


class ScopedCopyError(RuntimeError):
    pass


def manifest_digest(root: Path) -> str:
    manifest = _manifest(root)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def create(source: Path, *, profile_id: str, session_id: str) -> Path:
    """Copy a repository without hardlinks, and verify bytes before publishing it.

    Links and special files are refused by the manifest. A symlink in a project
    could otherwise point at Neo data, and a hardlink would let a run modify its
    original through an inode shared with the supposedly isolated copy.
    """

    if not profile_id or Path(profile_id).name != profile_id:
        raise ScopedCopyError("invalid profile scope")
    if not session_id or Path(session_id).name != session_id:
        raise ScopedCopyError("invalid session scope")
    source = source.resolve(strict=True)
    if overlaps_protected(source, profile_id=profile_id):
        raise ScopedCopyError("source overlaps protected storage")
    if source.is_relative_to(data_root() / "workspaces" / profile_id / "scoped"):
        raise ScopedCopyError("a scoped workspace cannot be reused as the source")
    scope_root = data_root() / "workspaces" / profile_id / "scoped"
    prepare_managed_root(scope_root.parent)
    prepare_managed_root(scope_root)
    destination = scope_root / session_id
    if destination.exists() or destination.is_symlink():
        raise ScopedCopyError("this session already has a scoped workspace")
    before = _manifest(source)
    stage = scope_root / f".copy-{session_id}-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, stage, symlinks=True, copy_function=shutil.copy2)
        if _manifest(source) != before or _manifest(stage) != before:
            raise ScopedCopyError("repository changed while it was copied")
        _fsync_tree(stage)
        os.rename(stage, destination)
        fd = os.open(scope_root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination

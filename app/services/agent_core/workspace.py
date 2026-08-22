"""Locating and containing the agent's working directory.

Every file tool resolves paths through here. The rules are the same ones
``git/safety.py`` enforces for diff paths -- stay inside the managed copy, no
traversal, no symlinks -- but they are applied to a path that may not exist yet,
because an agent that can only edit existing files cannot create one.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.core.config import get_settings
from app.services.repos import store as repos_store


class WorkspaceError(ValueError):
    """The requested path is not a usable location inside the managed copy."""


def repo_root(repo_id: str | None) -> Path:
    """The managed copy's directory, re-validated on every call.

    Containment is checked here rather than trusted from the stored row, so a
    tampered ``workspace_path`` cannot redirect writes outside the managed tree.
    """

    if not repo_id:
        raise WorkspaceError("This action needs a repository. Attach one to the session first.")
    repo = repos_store.get_repo(repo_id)
    if repo is None or repo.get("deleted"):
        raise WorkspaceError("Repository not found.")
    managed_root = Path(get_settings().workspace_repos_dir).resolve()
    root = Path(repo["workspace_path"])
    if root.is_symlink():
        raise WorkspaceError("Managed repository workspace may not be a symlink.")
    resolved = root.resolve()
    if resolved != (managed_root / repo["id"]).resolve() or managed_root not in resolved.parents:
        raise WorkspaceError("Repository workspace is outside Neo's managed directory.")
    if resolved == Path(repo["original_path"]).resolve():
        raise WorkspaceError("Agent file operations may never target the original repository.")
    return resolved


def normalize_relative(raw_path: str) -> str:
    """Reject anything that is not a plain relative path inside the tree."""

    if not raw_path or not raw_path.strip():
        raise WorkspaceError("A path is required.")
    candidate = raw_path.strip().replace("\\", "/")
    if candidate.startswith("/") or PurePosixPath(candidate).is_absolute():
        raise WorkspaceError("Path must be relative to the repository root.")
    parts = [part for part in PurePosixPath(candidate).parts if part not in {"."}]
    if ".." in parts:
        raise WorkspaceError("Path traversal is not allowed.")
    if not parts:
        raise WorkspaceError("Path must name a file.")
    return PurePosixPath(*parts).as_posix()


def resolve(root: Path, raw_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a relative path inside ``root``, refusing every way out of it.

    ``must_exist=False`` is the create case, so the *deepest existing ancestor*
    is what gets resolved and containment-checked -- resolving a not-yet-created
    path would silently succeed even when a parent is a symlink pointing away.
    """

    relative = normalize_relative(raw_path)
    candidate = root.joinpath(*PurePosixPath(relative).parts)

    probe = candidate
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    resolved_ancestor = probe.resolve()
    if resolved_ancestor != root and root not in resolved_ancestor.parents:
        raise WorkspaceError("Path escapes the repository root.")

    if candidate.is_symlink():
        raise WorkspaceError("Symlinks may not be read or written.")
    if must_exist:
        if not candidate.exists():
            raise WorkspaceError(f"'{relative}' does not exist.")
        if not candidate.is_file():
            raise WorkspaceError(f"'{relative}' is not a file.")
    return candidate


def relative_to_root(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()

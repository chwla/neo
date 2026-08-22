"""Locating and containing the agent's working directory.

Every file tool resolves paths through here. The rules are the same ones
``git/safety.py`` enforces for diff paths -- stay inside the managed copy, no
traversal, no symlinks -- but they are applied to a path that may not exist yet,
because an agent that can only edit existing files cannot create one.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.core.paths import is_reparse_point, is_within, normalize_relative_parts, same_path
from app.services.repos import store as repos_store
from app.services.repos.safety import validate_live_root

#: Written as ``original_path`` by the browser upload flow, which no longer
#: exists. Rows created by it survive in older databases and name no directory
#: any process can reach, so they are refused by name rather than left to fail
#: further in as a confusing path error.
_RETIRED_UPLOAD_SCHEME = "upload://"


class WorkspaceError(ValueError):
    """The requested path is not a usable location inside the agent's workspace."""


def repo_root(repo_id: str | None) -> Path:
    """The agent's working directory, re-validated on every call.

    Nothing is trusted from the stored row. That mattered when the only
    workspace was a managed copy -- a tampered ``workspace_path`` must not
    redirect writes out of the managed tree -- and it matters more now that a
    live workspace *is* the user's folder, because the same tampering would
    otherwise aim writes at an arbitrary directory. So each kind re-derives its
    own answer here:

    * ``managed`` must resolve to ``workspace_repos_dir/<repo_id>`` and must not
      be the original repository.
    * ``live`` must have ``workspace_path`` and ``original_path`` in agreement,
      and that path must still pass the full live-root check -- system trees,
      ``$HOME`` and the configured root list included.
    """

    if not repo_id:
        raise WorkspaceError("This action needs a repository. Attach one to the session first.")
    repo = repos_store.get_repo(repo_id)
    if repo is None or repo.get("deleted"):
        raise WorkspaceError("Repository not found.")

    original = str(repo.get("original_path") or "")
    if original.startswith(_RETIRED_UPLOAD_SCHEME):
        raise WorkspaceError(
            "This workspace was uploaded, and uploads are no longer supported. "
            "Attach the folder from your machine instead and Neo will work in it directly."
        )

    root = Path(repo["workspace_path"])
    if is_reparse_point(root):
        raise WorkspaceError("A repository workspace may not be a symlink or junction.")

    if repo.get("access") == repos_store.LIVE:
        resolved = root.resolve()
        if not same_path(resolved, Path(original).resolve()):
            raise WorkspaceError(
                "This workspace's folder no longer matches the one that was attached."
            )
        try:
            return validate_live_root(original)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc

    managed_root = Path(get_settings().workspace_repos_dir).resolve()
    resolved = root.resolve()
    expected = (managed_root / repo["id"]).resolve()
    if not same_path(resolved, expected) or not is_within(managed_root, resolved):
        raise WorkspaceError("Repository workspace is outside Neo's managed directory.")
    if same_path(resolved, Path(original).resolve()):
        raise WorkspaceError("Agent file operations may never target the original repository.")
    return resolved


def is_live(repo_id: str | None) -> bool:
    """Whether this repository's files are the user's own, not a copy."""

    if not repo_id:
        return False
    repo = repos_store.get_repo(repo_id)
    return bool(repo) and repo.get("access") == repos_store.LIVE


def normalize_relative(raw_path: str) -> str:
    """Reject anything that is not a plain relative path inside the tree.

    Delegates to ``core.paths`` so the file tools and the command sandbox share
    one definition of "relative". That shared definition is also what rejects a
    Windows drive path: ``C:/Windows`` is not absolute by POSIX rules, so a check
    that only asked ``is_absolute()`` accepted it as relative and then joined it
    onto the root -- which discards the root.
    """

    try:
        parts = normalize_relative_parts(raw_path)
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc
    return "/".join(parts)


def resolve(root: Path, raw_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a relative path inside ``root``, refusing every way out of it.

    ``must_exist=False`` is the create case, so the *deepest existing ancestor*
    is what gets resolved and containment-checked -- resolving a not-yet-created
    path would silently succeed even when a parent is a symlink pointing away.
    """

    relative = normalize_relative(raw_path)
    candidate = root.joinpath(*relative.split("/"))

    probe = candidate
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    resolved_ancestor = probe.resolve()
    if not is_within(root, resolved_ancestor):
        raise WorkspaceError("Path escapes the repository root.")

    # Checked on every component, not just the leaf: a junction or symlink part
    # way down the path redirects everything below it, and on Windows a junction
    # is not a symlink so the leaf check alone would miss it.
    walked = root
    for part in relative.split("/"):
        walked = walked / part
        if is_reparse_point(walked):
            raise WorkspaceError("Symlinks and junctions may not be read or written.")
    if must_exist:
        if not candidate.exists():
            raise WorkspaceError(f"'{relative}' does not exist.")
        if not candidate.is_file():
            raise WorkspaceError(f"'{relative}' is not a file.")
    return candidate


def relative_to_root(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()

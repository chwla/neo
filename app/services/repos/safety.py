from __future__ import annotations

import os
from pathlib import Path

from app.core.config import get_settings
from app.core.paths import (
    account_containers,
    in_container,
    is_reparse_point,
    is_within,
    path_key,
    same_path,
    split_roots,
    system_roots,
)

__all__ = [
    "ensure_inside",
    "in_container",
    "validate_live_root",
    "validate_repo_root",
]

#: Broad user directories that are a mistake to register rather than a danger:
#: they hold everything, so indexing one is never what was meant.
_BROAD_HOME_DIRS = ("Desktop", "Documents", "Downloads", "OneDrive", "Pictures", "Music", "Videos")


def _refused_home_dirs() -> set[str]:
    home = Path.home()
    return {
        path_key((home / name).resolve())
        for name in _BROAD_HOME_DIRS
        if (home / name).exists()
    }


def validate_repo_root(raw_path: str) -> Path:
    """The rules for any folder Neo will index, on every platform.

    Comparisons go through ``path_key`` rather than ``==``, because on Windows
    and macOS the filesystem ignores case: written as plain equality, every
    refusal below is bypassed by typing the path in a different case.
    """

    candidate = Path(raw_path).expanduser()
    # Junctions are checked as well as symlinks: on Windows a junction redirects
    # exactly like a symlink but reports ``is_symlink() == False``.
    if is_reparse_point(candidate):
        raise ValueError("Repository root may not be a symlink or junction.")
    if not candidate.exists():
        raise ValueError("Repository path does not exist.")
    if not candidate.is_dir():
        raise ValueError("Repository path must be a directory.")
    resolved = candidate.resolve()
    if is_reparse_point(resolved):
        raise ValueError("Repository root may not be a symlink or junction.")
    if same_path(resolved, resolved.anchor):
        raise ValueError("Root directories cannot be registered.")
    if same_path(resolved, Path.home().resolve()):
        raise ValueError("The user home directory cannot be registered as a repository.")
    if any(same_path(resolved, container) for container in account_containers()):
        raise ValueError(
            "This directory holds every user account; choose a project folder inside it."
        )
    if any(is_within(root, resolved) for root in system_roots()):
        raise ValueError("System directories cannot be registered as repositories.")
    if path_key(resolved) in _refused_home_dirs():
        raise ValueError("Choose a project folder, not a broad user directory.")
    return resolved


def ensure_inside(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if not is_within(root, resolved):
        raise ValueError("Repository entry escapes the selected folder.")
    return resolved


def _live_roots() -> list[Path]:
    """The configured roots, parsed with the platform's own list separator.

    ``os.pathsep`` rather than a hardcoded colon: on Windows ``C:\\code`` would
    split into ``C`` and ``\\code``, turning one real root into two that do not
    exist -- which fails open into "no roots configured".
    """

    raw = get_settings().workspace_live_roots
    configured = split_roots(raw)
    roots: list[Path] = []
    for part in configured:
        try:
            candidate = Path(part).expanduser().resolve()
        except OSError:
            continue
        if candidate.is_dir():
            roots.append(candidate)
    if configured and not roots:
        # Fail closed, loudly. The caller's test is ``if roots``, so an empty
        # list reads as "no restriction configured" -- which would switch the
        # restriction off in precisely the case where someone tried to set it.
        # The realistic way to get here is a separator mismatch: ``C:\code``
        # written into a Linux container's environment splits on ``:`` into two
        # directories that do not exist.
        raise ValueError(
            f"NEO_WORKSPACE_LIVE_ROOTS is set to {raw!r} but none of those directories "
            f"exist here. Separate entries with {os.pathsep!r} on this platform, and use "
            "the path as it appears inside the container if Neo is containerised."
        )
    return roots


def validate_live_root(raw_path: str) -> Path:
    """Validate a folder the agent will edit *in place*.

    Everything ``validate_repo_root`` refuses is refused here too -- this adds to
    that check rather than replacing it, because writing directly to a folder is
    strictly more dangerous than copying out of one.

    The two additions are the configured root list, and a better diagnosis when
    the path is simply not visible: inside a container an unmounted host folder
    looks exactly like a typo, and saying "does not exist" sends the user looking
    for the wrong problem.
    """

    candidate = Path(raw_path).expanduser()
    if not candidate.exists() and in_container():
        raise ValueError(
            "Neo cannot see that folder because it is running in a container. "
            "Bind-mount it and attach it by its path inside the container "
            "(see NEO_WORKSPACE_HOST_ROOT and NEO_WORKSPACE_LIVE_ROOTS), or run "
            "Neo directly on this machine."
        )
    resolved = validate_repo_root(raw_path)
    roots = _live_roots()
    if roots and not any(is_within(root, resolved) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ValueError(f"Folders opened for direct editing must live under: {allowed}.")
    return resolved

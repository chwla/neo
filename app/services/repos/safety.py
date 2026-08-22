from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings

# Matched against a candidate *and all of its parents*: nothing in these trees may be
# registered.
SYSTEM_ROOTS = {
    Path("/System"),
    Path("/Library"),
    Path("/usr"),
    Path("/etc"),
    Path("/var"),
    Path("/bin"),
    Path("/sbin"),
    Path("/opt"),
    Path("/Applications"),
}
# Matched *exactly*, never against parents. These hold the account directories rather
# than being system trees themselves: /Users is not registrable, but /Users/<name>/...
# is where every real project on macOS lives.
ACCOUNT_CONTAINERS = {
    Path("/Users"),
    Path("/home"),
}


def validate_repo_root(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_symlink():
        raise ValueError("Repository root may not be a symlink.")
    if not candidate.exists():
        raise ValueError("Repository path does not exist.")
    if not candidate.is_dir():
        raise ValueError("Repository path must be a directory.")
    resolved = candidate.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError("Root directories cannot be registered.")
    if resolved == Path.home().resolve():
        raise ValueError("The user home directory cannot be registered as a repository.")
    if resolved in ACCOUNT_CONTAINERS:
        raise ValueError(
            "This directory holds every user account; choose a project folder inside it."
        )
    if any(resolved == root or root in resolved.parents for root in SYSTEM_ROOTS):
        raise ValueError("System directories cannot be registered as repositories.")
    if resolved in {
        (Path.home() / name).resolve()
        for name in ("Desktop", "Documents", "Downloads")
        if (Path.home() / name).exists()
    }:
        raise ValueError("Choose a project folder, not a broad user directory.")
    return resolved


def ensure_inside(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Repository entry escapes the selected folder.")
    return resolved


def _live_roots() -> list[Path]:
    raw = get_settings().workspace_live_roots
    return [Path(part).expanduser().resolve() for part in raw.split(":") if part.strip()]


def in_container() -> bool:
    """Whether this process can only see the filesystem someone mounted for it."""

    return Path("/.dockerenv").exists()


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
            "Bind-mount the folder into the container and attach it by its path "
            "there (NEO_WORKSPACE_LIVE_ROOTS), or run Neo directly on this machine."
        )
    resolved = validate_repo_root(raw_path)
    roots = _live_roots()
    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ValueError(
            f"Folders opened for direct editing must live under: {allowed}."
        )
    return resolved

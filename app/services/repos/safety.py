from __future__ import annotations

from pathlib import Path

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

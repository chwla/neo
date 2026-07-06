from __future__ import annotations

from pathlib import Path

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
    Path("/Users"),
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

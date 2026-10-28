"""Finding private data that ended up where Neo does not look after it.

Relative storage defaults used to resolve against the process's working directory,
so starting Neo from a different folder quietly created a second installation's
worth of data there. This repository still contains two of them --
``app/neo_memory.db`` with its own ``app/profiles/accounts/``, and
``frontend/profiles/registry.db`` -- each a real profile's data, sitting outside
every protection the data root gets.

This module finds them and says where they are. It does not move them, merge them or
delete them, and that restraint is the point: a stray store can be the only copy of
an account, and Neo cannot know which of two registries the person wants. Reporting
is the whole job; the decision is theirs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.services.isolation.roots import data_root

_LOG = logging.getLogger(__name__)

#: Filenames that are Neo's own storage wherever they are found.
STORE_NAMES = ("neo_memory.db", "registry.db", "neo.db")
#: Directories that are Neo's storage by their shape.
STORE_DIRS = ("profiles",)
#: Not worth walking, and full of files that merely look similar.
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".mypy_cache",
}
#: A bound on the walk: a stray store sits near the install, not ten levels down.
MAX_DEPTH = 3


def _install_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def find_stray_stores(extra_roots: list[Path] | None = None) -> list[dict]:
    """Neo-shaped storage outside the data root.

    Returns one entry per find, with enough for a person to decide what it is: the
    path, what made it look like Neo's, its size and when it was last written.
    """

    root = data_root()
    searched: list[Path] = [_install_root(), *(extra_roots or [])]
    found: dict[str, dict] = {}

    for base in searched:
        if not base.is_dir():
            continue
        for current, dirnames, filenames in os.walk(base):
            here = Path(current)
            depth = len(here.relative_to(base).parts)
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

            for name in filenames:
                if name in STORE_NAMES or name.endswith((".db-wal", ".db-shm")):
                    if name.endswith((".db-wal", ".db-shm")):
                        stem = name.rsplit(".db", 1)[0] + ".db"
                        if stem not in STORE_NAMES:
                            continue
                    path = here / name
                    if _is_expected(path, root):
                        continue
                    found.setdefault(str(path), _describe(path, f"a file named {name}"))
            for name in list(dirnames):
                if name in STORE_DIRS and (here / name / "accounts").is_dir():
                    path = here / name
                    if _is_expected(path, root):
                        continue
                    dirnames.remove(name)
                    found.setdefault(str(path), _describe(path, "a profiles directory"))
    return sorted(found.values(), key=lambda item: item["path"])


def _is_expected(path: Path, root: Path) -> bool:
    """Whether this is storage at one of the places Neo actually keeps it.

    Checked against the canonical paths rather than by "is it under the data root":
    when the data root *is* the install directory -- the default -- a stray left in
    ``app/`` or ``frontend/`` is under it too, and skipping the whole subtree would
    hide exactly the ones worth reporting.
    """

    expected = {
        root / "profiles",
        root / "isolation",
        root / "tmp",
    }
    # The live database and the -wal/-shm SQLite keeps beside it.
    for name in ("neo_memory.db", "neo.db"):
        expected.add(root / name)
        expected.add(root / f"{name}-wal")
        expected.add(root / f"{name}-shm")
    if path in expected:
        return True
    # Anything beneath the live profiles directory, or the scoped roots.
    for base in (
        root / "profiles",
        root / "workspaces",
        root / "agent-state",
        root / "engines",
    ):
        if _is_within(base, path):
            return True
    return False


def _describe(path: Path, why: str) -> dict:
    try:
        info = path.stat()
        size = info.st_size if path.is_file() else _tree_size(path)
        modified = info.st_mtime
    except OSError:
        size, modified = 0, 0.0
    return {
        "path": str(path),
        "reason": why,
        "size_bytes": size,
        "modified_at": modified,
        "is_directory": path.is_dir(),
    }


def _tree_size(path: Path) -> int:
    total = 0
    for current, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def report() -> dict:
    """What the readiness probe and the settings panel show.

    Advisory only. Nothing acts on this, and nothing should: the remedy is for the
    person to look at what is there and decide.
    """

    strays = find_stray_stores()
    return {
        "data_root": str(data_root()),
        "stray_stores": strays,
        "advice": (
            "These hold profile data created when Neo was started from a different "
            "directory. Nothing reads them now, and nothing here has changed them. "
            "Open them if you need anything from them, then remove them yourself."
        )
        if strays
        else "",
    }


def log_report() -> None:
    """Note any strays once at startup, without their contents."""

    try:
        strays = find_stray_stores()
    except OSError:
        return
    for item in strays:
        _LOG.warning(
            "stray_store_found path=%s reason=%s size=%s",
            item["path"],
            item["reason"],
            item["size_bytes"],
        )


__all__ = ["MAX_DEPTH", "STORE_DIRS", "STORE_NAMES", "find_stray_stores", "log_report", "report"]

"""Staging for browser-uploaded folders.

The path-based registration flow reads a directory that the server can already
see. That assumption does not survive containerisation: inside Docker the user's
folders are simply not on the filesystem. Uploading is the way across that
boundary, and this module is the narrow part of it -- it rebuilds the folder the
browser sent as a real directory tree so the existing scanner, ignore rules and
importer can run against it unchanged.

Every path arriving here is attacker-controlled (``webkitRelativePath`` is just a
string the page supplies), so each segment is validated before it becomes a path
component and the finished path is re-checked against the staging root.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings

_UNSAFE_SEGMENTS = {"", ".", ".."}
MAX_PATH_SEGMENTS = 40
MAX_SEGMENT_LENGTH = 255


@dataclass(frozen=True)
class StagedUpload:
    root: Path
    name: str
    file_count: int
    total_bytes: int
    skipped: int


def _staging_root() -> Path:
    root = Path(get_settings().workspace_repos_dir).resolve().parent / "_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def normalize_relative_path(raw: str) -> tuple[str, ...] | None:
    """Split a browser-supplied path into safe segments, or ``None`` to skip it.

    Returning ``None`` rather than raising keeps one malformed entry in a large
    folder from failing the whole upload; the caller reports the skip count.
    """

    if not raw or "\x00" in raw:
        return None
    candidate = raw.replace("\\", "/")
    if candidate.startswith("/"):
        return None
    segments = tuple(segment.strip() for segment in candidate.split("/"))
    if not segments or len(segments) > MAX_PATH_SEGMENTS:
        return None
    for segment in segments:
        if segment in _UNSAFE_SEGMENTS or len(segment) > MAX_SEGMENT_LENGTH:
            return None
        # A drive letter or device name would be reinterpreted by the OS rather
        # than treated as a plain directory name.
        if ":" in segment:
            return None
    return segments


def _common_root(paths: list[tuple[str, ...]]) -> str | None:
    """The single top-level folder every file sits under, if there is one.

    Browsers prefix ``webkitRelativePath`` with the chosen folder's own name.
    Stripping it keeps stored paths relative to the repository root, matching
    what path-based registration produces.
    """

    if not paths:
        return None
    first = paths[0][0]
    if len(paths[0]) < 2:
        return None
    for parts in paths:
        if len(parts) < 2 or parts[0] != first:
            return None
    return first


def stage_upload(entries: list[tuple[str, bytes]], *, fallback_name: str) -> StagedUpload:
    """Write uploaded (path, content) pairs into a fresh staging directory."""

    settings = get_settings()
    max_files = settings.workspace_repo_max_files
    max_total_bytes = settings.workspace_repo_max_total_bytes
    max_file_bytes = settings.workspace_repo_max_file_bytes

    normalized: list[tuple[tuple[str, ...], bytes]] = []
    skipped = 0
    for raw_path, content in entries:
        segments = normalize_relative_path(raw_path)
        if segments is None or len(content) > max_file_bytes:
            skipped += 1
            continue
        normalized.append((segments, content))

    if not normalized:
        raise ValueError("The uploaded folder contained no files Neo can read.")
    if len(normalized) > max_files:
        raise ValueError(
            f"That folder has more than {max_files} files. "
            "Upload a smaller folder, or remove build output before uploading."
        )
    total_bytes = sum(len(content) for _, content in normalized)
    if total_bytes > max_total_bytes:
        limit_mb = max_total_bytes // (1024 * 1024)
        raise ValueError(f"That folder is larger than the {limit_mb} MB upload limit.")

    stripped = _common_root([segments for segments, _ in normalized])
    name = stripped or fallback_name

    root = _staging_root() / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=False)
    written = 0
    written_bytes = 0
    try:
        for segments, content in normalized:
            relative = segments[1:] if stripped else segments
            if not relative:
                skipped += 1
                continue
            destination = root.joinpath(*relative)
            # Belt and braces: the segment checks above should make this
            # impossible, but staging writes attacker-named paths to disk.
            if not destination.resolve().is_relative_to(root.resolve()):
                skipped += 1
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            written += 1
            written_bytes += len(content)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    if not written:
        shutil.rmtree(root, ignore_errors=True)
        raise ValueError("The uploaded folder contained no files Neo can read.")

    return StagedUpload(
        root=root,
        name=name,
        file_count=written,
        total_bytes=written_bytes,
        skipped=skipped,
    )


def discard(root: Path) -> None:
    """Remove a staging directory once its contents have been imported."""

    shutil.rmtree(root, ignore_errors=True)

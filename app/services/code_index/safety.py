from __future__ import annotations

from pathlib import Path

from app.services.files.service import WorkspaceFilesService

MAX_INDEX_CHARS_PER_FILE = 100_000


def workspace_text(file_item: dict) -> str:
    metadata = file_item.get("metadata", {})
    if metadata.get("source") != "local_repo":
        raise ValueError("Codebase Index only reads repo-backed workspace files.")
    path = WorkspaceFilesService().download_path(file_item["id"])
    if not Path(path).is_file():
        raise LookupError("Managed workspace file is unavailable.")
    text = file_item.get("extracted_text")
    if text is None:
        raise ValueError("File has no supported static text content.")
    return text[:MAX_INDEX_CHARS_PER_FILE]

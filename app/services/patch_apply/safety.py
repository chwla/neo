from __future__ import annotations

import re
from pathlib import PurePosixPath

FORBIDDEN_PATCH_MARKERS = (
    "/dev/null",
    "new file mode",
    "deleted file mode",
    "rename from",
    "rename to",
)


def validate_patch_text_safety(patch_text: str) -> None:
    lowered = patch_text.lower()
    for marker in FORBIDDEN_PATCH_MARKERS:
        if marker in lowered:
            raise ValueError(f"Patch operation is not supported: {marker}.")
    if "../" in patch_text or "..\\" in patch_text:
        raise ValueError("Patch paths may not contain path traversal.")


def normalize_target_path(raw: str) -> str:
    value = raw.strip().split("\t", 1)[0].strip()
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[/\\]", value):
        raise ValueError("Patch paths must be relative workspace filenames.")
    if "\\" in value or ".." in PurePosixPath(value).parts:
        raise ValueError("Patch paths may not contain path traversal.")
    if any(part in {"", "."} for part in PurePosixPath(value).parts):
        raise ValueError("Patch paths must identify a managed workspace file.")
    return value

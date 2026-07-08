from __future__ import annotations

import re
from pathlib import PurePosixPath

from app.services.files.safety import is_preview_supported

FORBIDDEN_MARKERS = (
    "deleted file mode",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "old mode",
    "new mode",
    "git binary patch",
    "binary files ",
    "submodule",
)
FORBIDDEN_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
    ".next", ".cache", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
FORBIDDEN_NAMES = {".env", ".env.local", ".env.production", ".ds_store"}


def validate_patch_text_safety(patch_text: str) -> None:
    lowered = patch_text.lower()
    for marker in FORBIDDEN_MARKERS:
        if marker in lowered:
            raise ValueError(f"Patch operation is not supported: {marker}.")
    if "../" in patch_text or "..\\" in patch_text:
        raise ValueError("Patch paths may not contain path traversal.")


def normalize_target_path(raw: str) -> str:
    value = raw.strip().split("\t", 1)[0].strip()
    if value == "/dev/null":
        return value
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[/\\]", value):
        raise ValueError("Patch paths must be repository-relative.")
    path = PurePosixPath(value)
    if "\\" in value or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise ValueError("Patch paths may not contain path traversal.")
    validate_target_path(value)
    return value


def validate_target_path(value: str) -> None:
    path = PurePosixPath(value)
    lowered_parts = [part.lower() for part in path.parts]
    if any(part.startswith(".") for part in path.parts):
        raise ValueError("Hidden files and directories cannot be patched.")
    if any(part in FORBIDDEN_DIRS for part in lowered_parts):
        raise ValueError("Dependency, build, cache, and Git directories cannot be patched.")
    if path.name.lower() in FORBIDDEN_NAMES:
        raise ValueError("Secret or hidden files cannot be patched.")
    if not is_preview_supported(path.name):
        raise ValueError("Only supported text/code files can be patched.")

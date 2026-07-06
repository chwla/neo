from __future__ import annotations

import re
from pathlib import Path

SUPPORTED_EXTENSIONS = {
    "txt",
    "md",
    "py",
    "js",
    "jsx",
    "ts",
    "tsx",
    "c",
    "cpp",
    "h",
    "hpp",
    "java",
    "go",
    "rs",
    "json",
    "yaml",
    "yml",
    "toml",
    "env.example",
    "html",
    "css",
    "sql",
    "sh",
    "ps1",
}


def sanitize_filename(value: str) -> str:
    name = Path((value or "upload").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name[:180] or "upload"


def extension_for(filename: str) -> str | None:
    lowered = filename.lower()
    if lowered.endswith(".env.example") or lowered == "env.example":
        return "env.example"
    suffix = Path(lowered).suffix.lstrip(".")
    return suffix or None


def is_preview_supported(filename: str) -> bool:
    return extension_for(filename) in SUPPORTED_EXTENSIONS


def safe_storage_path(root: Path, internal_filename: str) -> Path:
    root = root.resolve()
    candidate = (root / internal_filename).resolve()
    if candidate.parent != root:
        raise ValueError("Unsafe workspace storage path.")
    return candidate

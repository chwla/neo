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

#: Images are previewable, but not as *text*. They are deliberately kept out of
#: SUPPORTED_EXTENSIONS: that set gates extract_text, and a JPEG rarely carries a
#: null byte in its first 8 KB, so admitting it there would sail past the binary
#: probe and latin-1 decode the pixels into 500 KB of mojibake.
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff"}


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


def is_image(filename: str) -> bool:
    return extension_for(filename) in IMAGE_EXTENSIONS


def safe_storage_path(root: Path, internal_filename: str) -> Path:
    root = root.resolve()
    candidate = (root / internal_filename).resolve()
    if candidate.parent != root:
        raise ValueError("Unsafe workspace storage path.")
    return candidate

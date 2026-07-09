from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from app.services.files.safety import extension_for, is_preview_supported
from app.services.repos.safety import ensure_inside

IGNORED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".cache",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
}
IGNORED_NAMES = {".DS_Store", ".env", ".env.local", ".env.production"}
IGNORED_PATTERNS = {
    "*.pyc",
    "*.pyo",
    "*.class",
    "*.o",
    "*.obj",
    "*.exe",
    "*.dll",
    "*.so",
    "*.dylib",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.pdf",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.7z",
    "*.mp4",
    "*.mov",
    "*.mp3",
    "*.wav",
}


@dataclass(frozen=True)
class ScannedFile:
    source_path: Path
    relative_path: str
    content: bytes


@dataclass(frozen=True)
class ScanResult:
    files: list[ScannedFile]
    total_bytes: int
    ignored_files: int
    ignored_dirs: int
    unsupported_files: int


def scan_repo(
    root: Path, *, max_files: int, max_total_bytes: int, max_file_bytes: int
) -> ScanResult:
    root = root.resolve()
    files: list[ScannedFile] = []
    total_bytes = ignored_files = ignored_dirs = unsupported_files = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for name in directories:
            path = current_path / name
            if (
                name in IGNORED_DIRS
                or (name.startswith(".") and name != ".neo")
                or path.is_symlink()
            ):
                ignored_dirs += 1
            else:
                kept_dirs.append(name)
        directories[:] = kept_dirs
        for name in filenames:
            source = current_path / name
            relative_parent = current_path.relative_to(root).as_posix()
            if relative_parent == ".neo" and name != "rules.json":
                ignored_files += 1
                continue
            if (
                name in IGNORED_NAMES
                or name.startswith(".")
                or any(fnmatch.fnmatch(name.lower(), pattern) for pattern in IGNORED_PATTERNS)
                or source.is_symlink()
            ):
                ignored_files += 1
                continue
            if not is_preview_supported(name):
                unsupported_files += 1
                continue
            safe_source = ensure_inside(root, source)
            size = safe_source.stat().st_size
            if size == 0:
                ignored_files += 1
                continue
            if size > max_file_bytes:
                raise ValueError(
                    f"Repository file exceeds the {max_file_bytes}-byte per-file cap: "
                    f"{safe_source.relative_to(root).as_posix()}"
                )
            content = safe_source.read_bytes()
            if b"\x00" in content[:8192]:
                unsupported_files += 1
                continue
            if len(files) + 1 > max_files:
                raise ValueError(
                    f"Repository exceeds the {max_files}-file import cap; narrow the scope."
                )
            if total_bytes + size > max_total_bytes:
                raise ValueError(
                    f"Repository exceeds the {max_total_bytes}-byte import cap; narrow the scope."
                )
            relative = safe_source.relative_to(root).as_posix()
            files.append(ScannedFile(safe_source, relative, content))
            total_bytes += size
    return ScanResult(files, total_bytes, ignored_files, ignored_dirs, unsupported_files)


def language_for(path: str) -> str | None:
    extension = extension_for(path)
    return {
        "py": "Python",
        "js": "JavaScript",
        "jsx": "JavaScript React",
        "ts": "TypeScript",
        "tsx": "TypeScript React",
        "md": "Markdown",
        "json": "JSON",
        "yaml": "YAML",
        "yml": "YAML",
        "toml": "TOML",
        "html": "HTML",
        "css": "CSS",
        "sql": "SQL",
        "sh": "Shell",
        "ps1": "PowerShell",
        "go": "Go",
        "rs": "Rust",
        "java": "Java",
        "c": "C",
        "h": "C",
        "cpp": "C++",
        "hpp": "C++",
        "txt": "Text",
    }.get(extension)

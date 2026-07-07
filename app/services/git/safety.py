from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from app.core.config import get_settings

SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
FORBIDDEN_OPERATIONS = {
    "clone",
    "fetch",
    "pull",
    "push",
    "remote",
    "submodule",
    "clean",
    "reset",
}


def validate_workspace(repo: dict) -> Path:
    configured_root = Path(get_settings().workspace_repos_dir).resolve(strict=True)
    expected = (configured_root / repo["id"]).resolve(strict=True)
    workspace = Path(repo["workspace_path"])
    if workspace.is_symlink():
        raise ValueError("Managed repository workspace may not be a symlink.")
    resolved = workspace.resolve(strict=True)
    if resolved != expected or configured_root not in resolved.parents:
        raise ValueError("Git operations are restricted to the managed repository workspace.")
    original = Path(repo["original_path"]).resolve()
    if resolved == original:
        raise ValueError("Git operations may never run in the original repository.")
    return resolved


def validate_relative_path(root: Path, raw_path: str) -> str:
    if not raw_path or Path(raw_path).is_absolute():
        raise ValueError("Diff path must be relative to the managed repository.")
    normalized = PurePosixPath(raw_path.replace("\\", "/"))
    if ".." in normalized.parts or normalized == PurePosixPath("."):
        raise ValueError("Diff path traversal is forbidden.")
    candidate = root.joinpath(*normalized.parts)
    parent = candidate.parent.resolve(strict=True)
    if parent != root and root not in parent.parents:
        raise ValueError("Diff path escapes the managed repository.")
    if candidate.is_symlink():
        raise ValueError("Diff paths may not target symlinks.")
    return normalized.as_posix()


def validate_sha(value: str) -> str:
    lowered = value.lower()
    if not SHA_PATTERN.fullmatch(lowered):
        raise ValueError("Invalid checkpoint commit SHA.")
    return lowered


def validate_message(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(ord(char) < 32 and char not in {"\t"} for char in cleaned):
        raise ValueError("Checkpoint message contains invalid control characters.")
    return cleaned


def validate_git_args(args: list[str]) -> list[str]:
    if not args or args[0] in FORBIDDEN_OPERATIONS:
        raise ValueError("Forbidden Git operation.")
    operation = args[0]
    valid = False
    if args == ["init"]:
        valid = True
    elif operation == "config" and len(args) == 4:
        valid = args[1] == "--local" and args[2] in {"user.name", "user.email"}
    elif operation == "status":
        valid = args == ["status", "--porcelain=v1"]
    elif operation == "diff":
        valid = args == ["diff", "--stat"] or (len(args) in {2, 3} and "--" in args)
    elif operation == "add":
        valid = len(args) >= 3 and args[1] == "--"
    elif operation == "commit":
        valid = len(args) == 3 and args[1] == "-m"
    elif operation == "log":
        valid = len(args) == 4 and args[:3] == ["log", "--oneline", "--decorate"]
    elif operation == "show":
        valid = len(args) == 4 and args[1:3] in (
            ["--stat", "--summary"],
            ["--name-only", "--format="],
        )
    elif operation == "rev-parse":
        valid = args == ["rev-parse", "HEAD"]
    elif operation == "branch":
        valid = args == ["branch", "--show-current"]
    elif operation == "restore":
        valid = (
            len(args) == 5
            and args[1].startswith("--source=")
            and bool(SHA_PATTERN.fullmatch(args[1].removeprefix("--source=")))
            and args[2:] == ["--worktree", "--", "."]
        )
    if not valid:
        raise ValueError("Git argv is not an approved fixed operation.")
    return list(args)

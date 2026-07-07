from __future__ import annotations

from pathlib import Path, PurePosixPath

SHELL_TOKENS = ("&&", "||", ";", "|", ">", "<", "`", "$(")
FORBIDDEN_EXECUTABLES = {
    "rm",
    "rmdir",
    "del",
    "sudo",
    "chmod",
    "chown",
    "git",
    "docker",
    "curl",
    "wget",
    "ssh",
    "scp",
    "bash",
    "sh",
    "zsh",
    "powershell",
    "cmd",
    "pip",
    "pip3",
    "pnpm",
    "yarn",
}
ALLOWED_NPM_SCRIPTS = {"test", "build", "lint", "typecheck"}


def validate_command(command: list[str]) -> list[str]:
    if not isinstance(command, list) or not command:
        raise ValueError("Command must be a non-empty JSON argv list.")
    if len(command) > 64 or any(not isinstance(arg, str) or not arg.strip() for arg in command):
        raise ValueError("Command arguments must be non-empty strings.")
    for arg in command:
        if any(token in arg for token in SHELL_TOKENS):
            raise ValueError("Shell chaining, substitution, pipes, and redirection are forbidden.")
        normalized = arg.replace("\\", "/")
        if (
            normalized.startswith(("/", "~/"))
            or "/../" in f"/{normalized}/"
            or "=/" in normalized
            or "=~" in normalized
        ):
            raise ValueError("Command arguments may not reference paths outside the workspace.")

    executable = command[0].lower()
    if executable in FORBIDDEN_EXECUTABLES:
        raise ValueError(f"Executable '{command[0]}' is forbidden.")
    if "/" in executable or "\\" in executable:
        raise ValueError("Executable paths are not allowed; use an allowlisted command name.")

    args = command[1:]
    if executable == "python":
        if len(args) < 2 or args[0] != "-m" or args[1] not in {"pytest", "unittest"}:
            raise ValueError("Python commands are limited to 'python -m pytest' or unittest.")
    elif executable == "pytest":
        pass
    elif executable == "npm":
        if args == ["test"]:
            pass
        elif len(args) == 2 and args[0] == "run" and args[1] in ALLOWED_NPM_SCRIPTS:
            pass
        else:
            raise ValueError(
                "npm is limited to test/build/lint/typecheck scripts; installs are forbidden."
            )
    elif executable == "node":
        if not args or args[0] != "--test":
            raise ValueError("Node commands are limited to 'node --test'.")
    else:
        raise ValueError(f"Executable '{command[0]}' is not allowlisted.")
    return list(command)


def validate_timeout(timeout_seconds: int) -> int:
    if not 1 <= timeout_seconds <= 600:
        raise ValueError("Timeout must be between 1 and 600 seconds.")
    return timeout_seconds


def resolve_working_directory(workspace_path: str, working_directory: str) -> Path:
    if not working_directory or Path(working_directory).is_absolute():
        raise ValueError("Working directory must be a relative path inside the managed repository.")
    normalized = PurePosixPath(working_directory.replace("\\", "/"))
    if ".." in normalized.parts:
        raise ValueError("Working directory traversal is forbidden.")
    root = Path(workspace_path).resolve(strict=True)
    candidate = (root / Path(*normalized.parts)).resolve(strict=True)
    if candidate != root and root not in candidate.parents:
        raise ValueError("Working directory escapes the managed repository workspace.")
    if not candidate.is_dir():
        raise ValueError("Working directory does not exist or is not a directory.")
    return candidate

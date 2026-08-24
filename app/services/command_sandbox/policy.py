from __future__ import annotations

from pathlib import PurePosixPath

ALLOWED: dict[str, tuple[tuple[str, ...], ...]] = {
    "read_only": (
        ("pwd",),
        ("ls",),
        ("find",),
        ("grep",),
        ("rg",),
        ("cat",),
        ("head",),
        ("tail",),
        ("wc",),
        ("tree",),
        # Local, read-only git inspection. Each of these queries repository state
        # without mutating it, so -- like cat or grep above -- any arguments are
        # allowed. "branch" is the exception: bare or flag-only invocations list
        # branches or name the current one, but a positional argument names one to
        # create/delete/rename, so that shape is rejected below instead of here.
        ("git", "status"),
        ("git", "log"),
        ("git", "diff"),
        ("git", "show"),
        ("git", "blame"),
        ("git", "describe"),
        ("git", "rev-parse"),
        ("git", "branch"),
    ),
    "test": (
        ("python", "-m", "pytest"),
        ("pytest",),
        ("npm", "test"),
        ("npm", "run", "test"),
        ("npm", "run", "build"),
        ("npm", "run", "lint"),
        ("ruff", "check"),
        ("mypy",),
    ),
    "build": (("python", "-m", "compileall"), ("npm", "run", "build")),
}
TIMEOUTS_MS = {"read_only": 10_000, "test": 120_000, "build": 180_000}
SHELL = ("&&", "||", ";", "|", ">", "<", "`", "$(", "${")
FORBIDDEN = {
    "sudo",
    "chmod",
    "chown",
    "rm",
    "rmdir",
    "del",
    "curl",
    "wget",
    "ssh",
    "scp",
    "bash",
    "sh",
    "zsh",
    "powershell",
    "cmd",
    "docker",
    "pip",
    "pip3",
    "uv",
    "poetry",
    "pnpm",
    "yarn",
    "brew",
    "apt",
    "apt-get",
    "dnf",
    "cargo",
    "go",
}
INSTALL = {"install", "add"}
ENV_DUMP = {"env", "printenv", "set"}
# The allowlist gates *which* program may run; these gate what it may be told to do.
# Several allowlisted read-only tools spawn processes or write files through their own
# flags, needing no shell at all, so they slip past FORBIDDEN and the SHELL check.
DANGEROUS_FLAGS: dict[str, frozenset[str]] = {
    "find": frozenset(
        {
            "-exec",
            "-execdir",
            "-ok",
            "-okdir",
            "-delete",
            "-fprintf",
            "-fprint",
            "-fprint0",
            "-fls",
        }
    ),
    "rg": frozenset({"--pre", "--hostname-bin"}),
}


def validate(command: list[str], category: str, cwd: str) -> dict:
    reasons: list[str] = []
    if category not in ALLOWED:
        reasons.append("unknown command category")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(x, str) or not x.strip() for x in command)
    ):
        reasons.append("command must be a non-empty argv array")
    if len(command) > 32:
        reasons.append("command has too many arguments")
    if command:
        executable = command[0].lower()
        if executable in FORBIDDEN or executable in ENV_DUMP:
            reasons.append(f"executable '{command[0]}' is forbidden")
        if "/" in executable or "\\" in executable:
            reasons.append("executable paths are forbidden")
        if executable == "git" and any(
            x in {"fetch", "pull", "push", "clone", "remote"} for x in command[1:]
        ):
            reasons.append("remote Git operations are forbidden")
        if (
            executable == "git"
            and len(command) > 1
            and command[1] == "branch"
            and any(not arg.startswith("-") for arg in command[2:])
        ):
            reasons.append(
                "git branch may only list branches or report the current one "
                "(e.g. --show-current); creating, deleting or renaming a branch is forbidden"
            )
        dangerous = DANGEROUS_FLAGS.get(executable, frozenset())
        for arg in command[1:]:
            # Match both "--flag value" and "--flag=value" spellings.
            if arg.lower() in dangerous or arg.split("=", 1)[0].lower() in dangerous:
                reasons.append(f"flag '{arg.split('=', 1)[0]}' is forbidden for '{command[0]}'")
        for arg in command:
            norm = arg.replace("\\", "/")
            if any(token in arg for token in SHELL):
                reasons.append("shell syntax is forbidden")
            if norm.startswith(("/", "~/")) or ".." in PurePosixPath(norm).parts:
                reasons.append("absolute or traversal command paths are forbidden")
            if "=" in arg and any(
                key in arg.lower() for key in ("key", "secret", "token", "password")
            ):
                reasons.append("credential arguments are forbidden")
        if executable in {
            "npm",
            "pnpm",
            "yarn",
            "pip",
            "pip3",
            "uv",
            "poetry",
            "cargo",
            "go",
            "brew",
        } and any(x.lower() in INSTALL for x in command[1:]):
            reasons.append("package installation is forbidden")
        if not reasons and not any(
            tuple(command[: len(prefix)]) == prefix for prefix in ALLOWED.get(category, ())
        ):
            reasons.append("command is not allowlisted for this category")
    path = PurePosixPath(cwd.replace("\\", "/"))
    if not cwd or path.is_absolute() or ".." in path.parts:
        reasons.append("cwd must be a managed-workspace relative path")
    return {
        "allowed": not reasons,
        "approval_required": True,
        "reason": "allowlisted command; explicit approval required"
        if not reasons
        else "command blocked by policy",
        "blocked_reasons": sorted(set(reasons)),
    }

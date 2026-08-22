"""File tools: read, list, glob, grep, write, edit.

These are the agent's hands. Everything resolves through
``agent_core.workspace``, so containment is enforced in one place rather than
per tool, and every write lands in the managed copy -- never in the user's
original repository, which is reached only through explicit delivery.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.agent_core.workspace import WorkspaceError, repo_root, resolve

MAX_READ_BYTES = 200_000
MAX_MATCHES = 100
MAX_ENTRIES = 400
#: Text is what the agent can reason about; a binary blob would only burn
#: context. Detection is by NUL byte, which is how git decides the same thing.
_BINARY_PROBE = 8192


def _is_binary(path: Path) -> bool:
    try:
        return b"\x00" in path.open("rb").read(_BINARY_PROBE)
    except OSError:
        return False


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def read_file(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    path = resolve(root, str(arguments.get("path", "")), must_exist=True)
    if _is_binary(path):
        raise WorkspaceError("That file is binary and cannot be read as text.")
    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(text.encode("utf-8")) > MAX_READ_BYTES
    if truncated:
        text = text.encode("utf-8")[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
    lines = text.splitlines()
    offset = max(int(arguments.get("offset") or 0), 0)
    limit = int(arguments.get("limit") or len(lines))
    window = lines[offset : offset + limit]
    numbered = "\n".join(f"{offset + index + 1}\t{line}" for index, line in enumerate(window))
    suffix = "\n... (truncated)" if truncated else ""
    return f"{numbered}{suffix}" if numbered else "(empty file)"


def list_dir(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    raw = str(arguments.get("path") or "").strip()
    target = root if raw in {"", ".", "/"} else resolve(root, raw)
    if not target.exists() or not target.is_dir():
        raise WorkspaceError(f"'{raw or '.'}' is not a directory.")
    entries = []
    for child in sorted(target.iterdir())[:MAX_ENTRIES]:
        entries.append(f"{child.name}/" if child.is_dir() else child.name)
    return "\n".join(entries) or "(empty directory)"


def glob_files(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    pattern = str(arguments.get("pattern") or "").strip()
    if not pattern:
        raise WorkspaceError("A glob pattern is required.")
    matches = [
        path.relative_to(root).as_posix()
        for path in _iter_files(root)
        if fnmatch.fnmatch(path.relative_to(root).as_posix(), pattern)
    ]
    return "\n".join(matches[:MAX_MATCHES]) or "(no files matched)"


def grep(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    raw_pattern = str(arguments.get("pattern") or "").strip()
    if not raw_pattern:
        raise WorkspaceError("A search pattern is required.")
    try:
        expression = re.compile(raw_pattern, re.IGNORECASE if arguments.get("ignore_case") else 0)
    except re.error as exc:
        raise WorkspaceError(f"Invalid regular expression: {exc}") from exc
    include = str(arguments.get("include") or "").strip()

    results: list[str] = []
    for path in _iter_files(root):
        relative = path.relative_to(root).as_posix()
        if include and not fnmatch.fnmatch(relative, include):
            continue
        if _is_binary(path):
            continue
        try:
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                if expression.search(line):
                    results.append(f"{relative}:{number}: {line.strip()[:200]}")
                    if len(results) >= MAX_MATCHES:
                        return "\n".join(results) + f"\n... (stopped at {MAX_MATCHES} matches)"
        except OSError:
            continue
    return "\n".join(results) or "(no matches)"


def write_file(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    path = resolve(root, str(arguments.get("path", "")))
    content = arguments.get("content")
    if not isinstance(content, str):
        raise WorkspaceError("`content` must be a string.")
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    relative = path.relative_to(root).as_posix()
    return f"{'Updated' if existed else 'Created'} {relative} ({len(content)} characters)."


def edit_file(arguments: dict, context: ToolContext):
    """Replace an exact substring.

    Uniqueness is required rather than replacing the first hit: an ambiguous
    match means the agent does not actually know which site it is changing, and
    silently picking one is how an edit lands in the wrong place.
    """

    root = repo_root(context.repo_id)
    path = resolve(root, str(arguments.get("path", "")), must_exist=True)
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        raise WorkspaceError("`old_string` and `new_string` must both be strings.")
    if not old:
        raise WorkspaceError("`old_string` must not be empty; use write_file to create a file.")
    if old == new:
        raise WorkspaceError("`old_string` and `new_string` are identical.")

    text = path.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(old)
    if occurrences == 0:
        raise WorkspaceError("`old_string` was not found in that file.")
    replace_all = bool(arguments.get("replace_all"))
    if occurrences > 1 and not replace_all:
        raise WorkspaceError(
            f"`old_string` appears {occurrences} times. Include more surrounding context "
            "to make it unique, or pass replace_all=true."
        )
    path.write_text(text.replace(old, new), encoding="utf-8")
    relative = path.relative_to(root).as_posix()
    return f"Edited {relative} ({occurrences} replacement{'s' if occurrences != 1 else ''})."


def _text(handler):
    def run(arguments: dict, context: ToolContext):
        return handler(arguments, context)

    return run


TOOLS = [
    AgentTool(
        name="read_file",
        description="Read a text file from the repository, with line numbers.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repository root."},
                "offset": {"type": "integer", "description": "First line to return (0-based)."},
                "limit": {"type": "integer", "description": "How many lines to return."},
            },
            "required": ["path"],
        },
        risk="read",
        requires_repo=True,
        handler=_text(read_file),
        path_arguments=("path",),
        summary=lambda a: f"Read {a.get('path')}",
    ),
    AgentTool(
        name="list_dir",
        description="List the entries of a directory in the repository.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory, default root."}},
        },
        risk="read",
        requires_repo=True,
        handler=_text(list_dir),
        path_arguments=("path",),
        summary=lambda a: f"List {a.get('path') or '.'}",
    ),
    AgentTool(
        name="glob",
        description="Find files whose path matches a glob pattern, e.g. 'app/**/*.py'.",
        parameters={
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
        risk="read",
        requires_repo=True,
        handler=_text(glob_files),
        summary=lambda a: f"Find files matching {a.get('pattern')}",
    ),
    AgentTool(
        name="grep",
        description="Search file contents with a regular expression.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "include": {
                    "type": "string",
                    "description": "Only search paths matching this glob.",
                },
                "ignore_case": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
        risk="read",
        requires_repo=True,
        handler=_text(grep),
        summary=lambda a: f"Search for {a.get('pattern')}",
    ),
    AgentTool(
        name="write_file",
        description="Create or overwrite a file in the managed repository copy.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        risk="workspace_write",
        requires_repo=True,
        handler=_text(write_file),
        path_arguments=("path",),
        summary=lambda a: f"Write {a.get('path')}",
    ),
    AgentTool(
        name="edit_file",
        description="Replace an exact, unique substring inside an existing file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        risk="workspace_write",
        requires_repo=True,
        handler=_text(edit_file),
        path_arguments=("path",),
        summary=lambda a: f"Edit {a.get('path')}",
    ),
]

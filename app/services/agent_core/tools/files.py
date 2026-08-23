"""File tools: read, list, glob, grep, write, edit.

These are the agent's hands. Everything resolves through
``agent_core.workspace``, so containment is enforced in one place rather than
per tool, and neither tool knows which kind of workspace it is in: ``repo_root``
returns the managed copy or the user's own folder, and the same containment
rules apply to both.

The one place the difference shows is the journal. A write into a live folder is
a write to the user's real file, so it is bracketed by a snapshot -- what the
file held before, and what the run left in it -- which is what makes the change
list, the diff and undo possible. A managed copy needs none of that: the copy is
itself the before-image.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from app.core import textio
from app.services.agent_core import journal
from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.agent_core.workspace import WorkspaceError, is_live, repo_root, resolve

#: A bind-mounted folder keeps its host ownership, so a container running as a
#: different uid can read everything and write nothing. Saying so beats "Errno 13".
PERMISSION_HINT = (
    "Permission denied writing {path}. If Neo is running in a container, the "
    "mounted folder belongs to a different user than the container runs as -- set "
    "NEO_UID and NEO_GID to your own (`id -u` / `id -g`) and restart."
)

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
    text = textio.read_text(path)
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
            for number, line in enumerate(textio.read_text(path).splitlines(), start=1):
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
    relative = path.relative_to(root).as_posix()
    live = is_live(context.repo_id)
    if live:
        journal.record_before(context.session_id, context.repo_id, root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A file Neo is replacing keeps the line endings it already had; a new one
    # gets LF. Either way the platform never gets to decide.
    try:
        textio.write_text(path, content)
    except PermissionError as exc:
        raise WorkspaceError(PERMISSION_HINT.format(path=relative)) from exc
    if live:
        journal.record_after(context.session_id, root, relative)
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

    text = textio.read_text(path)
    occurrences = text.count(old)
    if occurrences == 0:
        raise WorkspaceError("`old_string` was not found in that file.")
    replace_all = bool(arguments.get("replace_all"))
    if occurrences > 1 and not replace_all:
        raise WorkspaceError(
            f"`old_string` appears {occurrences} times. Include more surrounding context "
            "to make it unique, or pass replace_all=true."
        )
    relative = path.relative_to(root).as_posix()
    live = is_live(context.repo_id)
    if live:
        journal.record_before(context.session_id, context.repo_id, root, relative)
    try:
        textio.write_text(path, text.replace(old, new))
    except PermissionError as exc:
        raise WorkspaceError(PERMISSION_HINT.format(path=relative)) from exc
    if live:
        journal.record_after(context.session_id, root, relative)
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
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the repository root; '.' is the root.",
                }
            },
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
        description="Create or overwrite a file in the repository.",
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

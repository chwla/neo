"""File tools: read, list, glob, grep, create directories, write, edit, delete.

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
from app.core.fs_errors import permission_message
from app.core.paths import is_reparse_point, walk_contained
from app.services.agent_core import journal
from app.services.agent_core.tools.base import AgentTool, ToolContext, ok
from app.services.agent_core.types import Evidence
from app.services.agent_core.workspace import WorkspaceError, is_live, repo_root, resolve
from app.services.isolation import safe_fs

MAX_READ_BYTES = 200_000
MAX_MATCHES = 100
MAX_ENTRIES = 400
#: Text is what the agent can reason about; a binary blob would only burn
#: context. Detection is by NUL byte, which is how git decides the same thing.
_BINARY_PROBE = 8192


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_PROBE)
    except OSError:
        return False


def _iter_files(root: Path):
    """Every file the agent may see, in a stable order.

    Through ``walk_contained``, which never enters a symlink or a junction: a
    walk that follows one reads a folder outside the workspace, whatever the
    per-path checks in ``workspace.resolve`` would have said about it.
    """

    yield from sorted(walk_contained(root), key=lambda path: path.relative_to(root).as_posix())


def read_file(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    path = resolve(root, str(arguments.get("path", "")), must_exist=True)
    if _is_binary(path):
        raise WorkspaceError("That file is binary and cannot be read as text.")
    # Read no more than is shown: loading a multi-gigabyte file whole to show its
    # first 200 KB is how one tool call could exhaust the server's memory.
    with path.open("rb") as handle:
        raw = handle.read(MAX_READ_BYTES + 1)
    truncated = len(raw) > MAX_READ_BYTES
    if truncated:
        # Cut on a character boundary rather than mid-way through a UTF-8 sequence.
        raw = raw[:MAX_READ_BYTES].decode("utf-8", errors="ignore").encode("utf-8")
    text = textio.decode(raw)
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
    # Links are left out: nothing under one can be read or written, so listing
    # it would only invite a call that is refused.
    children = [child for child in sorted(target.iterdir()) if not is_reparse_point(child)]
    for child in children[:MAX_ENTRIES]:
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


def create_directory(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise WorkspaceError("`path` must be a non-empty relative path.")
    path = resolve(root, raw)
    relative = path.relative_to(root).as_posix()
    try:
        if safe_fs.supported():
            safe_fs.mkdir(root, relative)
        else:
            # Windows uses the same containment and reparse-point checks as
            # other workspace tools. Only create the leaf, never its parents.
            path.mkdir(exist_ok=True)
        if not resolve(root, relative).is_dir():
            raise WorkspaceError(f"'{relative}' is not a directory.")
    except PermissionError as exc:
        raise WorkspaceError(permission_message(relative, exc, target=path.parent)) from exc
    except FileNotFoundError as exc:
        raise WorkspaceError(f"The parent folder for '{relative}' does not exist.") from exc
    except FileExistsError as exc:
        raise WorkspaceError(f"'{relative}' already exists and is not a directory.") from exc

    call_id = context.extras["call_id"]
    detail = f"Verified directory {relative}/ in the attached workspace."
    if not is_live(context.repo_id):
        detail += " Empty directories in a managed copy are not included in file delivery."
    return ok(call_id, "create_directory", detail, Evidence(
        kind="file_verify", tool_call_id=call_id, passed=True, detail=detail
    ))


COMPLETE_TASK_PARAMETER = {
    "type": "boolean",
    "description": (
        "Set true on the LAST file operation if it completes the entire user request. "
        "A successful verified save ends the task without another model call. "
        "Leave false if requested work, tests, explanation, or delivery remains."
    ),
}


def _verified_save(path, expected: str, context: ToolContext, name: str, detail: str):
    # Match the text writer's newline normalization and encoding preservation.
    # This checks persistence only; it must never claim compilation or tests passed.
    actual = textio.read_editable(path).text
    if actual != expected.replace("\r\n", "\n").replace("\r", "\n"):
        raise WorkspaceError(f"Saved contents of '{path.name}' did not match the requested text.")
    call_id = context.extras["call_id"]
    detail += " Saved contents checked; execution was not checked."
    return ok(call_id, name, detail, Evidence(
        kind="file_verify", tool_call_id=call_id, passed=True, detail=detail
    ))


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
    # A file Neo is replacing keeps the line endings it already had; a new one
    # gets LF. Either way the platform never gets to decide. The write is atomic:
    # if it is refused, what was there before is still there.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        textio.write_text(path, content)
    except PermissionError as exc:
        raise WorkspaceError(permission_message(relative, exc, target=path)) from exc
    except textio.NotTextError as exc:
        raise WorkspaceError(str(exc)) from exc
    if live:
        journal.record_after(context.session_id, root, relative)
    return _verified_save(
        path, content, context, "write_file",
        f"{'Updated' if existed else 'Created'} {relative} ({len(content)} characters).",
    )


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

    # Read in the encoding the file is actually in. Decoding a cp1252 file as
    # UTF-8 "with replacement" and writing it back destroyed every accented
    # character in it, far from the line being edited.
    try:
        current = textio.read_editable(path)
    except textio.NotTextError as exc:
        raise WorkspaceError(str(exc)) from exc
    text = current.text
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
        textio.write_text(
            path, text.replace(old, new), newline=current.newline, encoding=current.encoding
        )
    except PermissionError as exc:
        raise WorkspaceError(permission_message(relative, exc, target=path)) from exc
    except textio.NotTextError as exc:
        raise WorkspaceError(str(exc)) from exc
    if live:
        journal.record_after(context.session_id, root, relative)
    return _verified_save(
        path, text.replace(old, new), context, "edit_file",
        f"Edited {relative} ({occurrences} replacement{'s' if occurrences != 1 else ''}).",
    )


def delete_file(arguments: dict, context: ToolContext):
    root = repo_root(context.repo_id)
    path = resolve(root, str(arguments.get("path", "")), must_exist=True)
    relative = path.relative_to(root).as_posix()
    live = is_live(context.repo_id)
    # No record_after: the snapshot's "before" is what undo restores, and a
    # missing after_sha256 is exactly what tells session_changes/undo the file
    # was removed rather than rewritten.
    if live:
        journal.record_before(context.session_id, context.repo_id, root, relative)
    try:
        path.unlink()
    except PermissionError as exc:
        raise WorkspaceError(permission_message(relative, exc, target=path)) from exc
    return f"Deleted {relative}."


def _text(handler):
    def run(arguments: dict, context: ToolContext):
        return handler(arguments, context)

    return run


TOOLS = [
    AgentTool(
        name="create_directory",
        description=(
            "Create and verify an empty directory inside the attached workspace. "
            "The parent directory must already exist."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace root."},
                "complete_task": COMPLETE_TASK_PARAMETER,
            },
            "required": ["path"],
        },
        risk="workspace_write",
        requires_repo=True,
        handler=create_directory,
        path_arguments=("path",),
        summary=lambda a: f"Create folder {a.get('path')}",
        supports_undo=False,
    ),
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
            "properties": {
                "path": {"type": "string"}, "content": {"type": "string"},
                "complete_task": COMPLETE_TASK_PARAMETER,
            },
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
                "complete_task": COMPLETE_TASK_PARAMETER,
            },
            "required": ["path", "old_string", "new_string"],
        },
        risk="workspace_write",
        requires_repo=True,
        handler=_text(edit_file),
        path_arguments=("path",),
        summary=lambda a: f"Edit {a.get('path')}",
    ),
    AgentTool(
        name="delete_file",
        description="Delete a file from the repository.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        risk="workspace_write",
        requires_repo=True,
        handler=_text(delete_file),
        path_arguments=("path",),
        summary=lambda a: f"Delete {a.get('path')}",
    ),
]

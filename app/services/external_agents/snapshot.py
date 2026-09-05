"""What an external run changed, recorded the way Neo's own runs record it.

Neo's diff, delivery and undo all read ``workspace_agent_file_snapshots``, which
``journal.py`` fills in as Neo's ``write_file`` tool touches each file. An
external CLI writes directly, so none of that happens and a run that rewrote half
the repository reports no changes at all.

This module closes that gap from git, and the whole difficulty is one case:
**the repository may already be dirty**. If the user had uncommitted edits before
the run, diffing against HEAD afterwards would attribute their work to the agent
-- and an undo would then revert it. So "before" here means *before this run*,
not *at the last commit*:

* Ahead of the run we record the working-tree content of every already-dirty
  path. That set is normally small, and it is the only content git cannot
  reconstruct later.
* Afterwards, a path belongs to the run only if it was not dirty before, or if
  its content actually moved. A file the user left dirty and the agent never
  touched is untouched here too.
* "Before" content comes from the pre-run capture for paths that were already
  dirty, and from ``git show HEAD:<path>`` for paths that were clean -- for a
  clean path those are by definition the same bytes.

The rows written are exactly the shape ``journal.session_changes`` and
``journal.undo`` already consume, so delivery, the diff view and undo work for an
external run without changing any of them.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.services.agent_core import store
from app.services.external_agents.types import ExternalAgentError
from app.services.git.executor import GitResult, git_available, run_git

_LOG = logging.getLogger(__name__)

#: Matches ``journal.MAX_SNAPSHOT_BYTES``: above this a snapshot records that the
#: file existed but not its content, because holding two copies of something that
#: large in SQLite to enable an undo nobody wants is a poor trade.
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024

#: A ceiling on how many already-dirty files we read up front. A repository left
#: in a wildly dirty state should slow a run down, not stall it -- past this we
#: record the paths without content, and undo declines rather than guesses.
MAX_PRE_CAPTURE_FILES = 2000


@dataclass
class RepoState:
    """The repository as it stood at one moment."""

    head: str | None = None
    #: path -> sha256 of the working-tree bytes, or None when the path is absent
    #: (a staged deletion, say).
    hashes: dict[str, str | None] = field(default_factory=dict)
    #: path -> decoded text, captured only for paths already dirty pre-run.
    contents: dict[str, str | None] = field(default_factory=dict)


def ensure_git_worktree(root: Path) -> None:
    """Refuse an external run outside git, before the CLI is ever started.

    Checked up front rather than discovered afterwards: without a commit to
    compare against there is no honest way to say what the run changed, and
    finding that out *after* an agent has edited files is the one moment it is
    useless. Codex agrees, incidentally -- `codex exec` refuses a non-repository
    unless told otherwise.
    """

    if not git_available():
        raise ExternalAgentError(
            "Git is not installed, and external agents need it to track what a run changed."
        )
    result = run_git(root, ["rev-parse", "HEAD"], check=False)
    if result.returncode != 0:
        raise ExternalAgentError(
            "This folder is not a git repository with any commits, and an external agent "
            "needs one so Neo can show and undo what the run changes. Initialise it with a "
            "first commit, then try again."
        )


def _status(root: Path) -> list[tuple[str, str]]:
    """`(code, path)` for every dirty entry, renames resolved to their target."""

    result: GitResult = run_git(root, ["status", "--porcelain=v1"], check=False)
    if result.returncode != 0:
        return []
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        # `R  old -> new`: the new name is the one on disk now.
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"')
        # An untracked *directory* is reported as one entry with a trailing
        # slash (`?? __pycache__/`), not as its contents. It is not a file, so
        # journalling it produces a change row that names no content and an undo
        # that can only skip it. Dropping it here keeps the change list to things
        # a reader recognises. (A repository with a normal .gitignore never gets
        # this far -- git omits ignored paths itself.)
        if path and not path.endswith("/"):
            entries.append((code, path))
    return entries


def _hash_file(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _newline(text: str | None) -> str:
    """Preserve CRLF, so an undo does not leave a whole-file diff behind."""

    if text and "\r\n" in text:
        return "\r\n"
    return "\n"


def capture(root: Path) -> RepoState:
    """The pre-run state: HEAD, plus every already-dirty path and its content."""

    state = RepoState()
    head = run_git(root, ["rev-parse", "HEAD"], check=False)
    if head.returncode == 0:
        state.head = head.stdout.strip() or None

    for index, (_code, relative) in enumerate(_status(root)):
        absolute = root / relative
        state.hashes[relative] = _hash_file(absolute)
        if index < MAX_PRE_CAPTURE_FILES:
            state.contents[relative] = _read_text(absolute)
    return state


def _head_text(root: Path, relative: str) -> str | None:
    """A file's content at HEAD, or None when it was not tracked."""

    try:
        result = run_git(root, ["show", f"HEAD:{relative}"], check=False)
    except (ValueError, RuntimeError):
        return None
    return result.stdout if result.returncode == 0 else None


def changed_paths(root: Path, before: RepoState) -> list[str]:
    """Paths this run is responsible for, excluding the user's own prior edits."""

    after = {relative: _hash_file(root / relative) for _code, relative in _status(root)}
    changed = {
        relative
        for relative, digest in after.items()
        if relative not in before.hashes or before.hashes[relative] != digest
    }
    # A path that was dirty before and is clean now was reverted by the run,
    # which is still a change the run is answerable for.
    changed |= {relative for relative in before.hashes if relative not in after}
    return sorted(changed)


def record(session_id: str, repo_id: str, root: Path, before: RepoState) -> list[str]:
    """Write journal rows for what the run changed. Returns the paths recorded."""

    recorded: list[str] = []
    for relative in changed_paths(root, before):
        if relative in before.contents:
            # Already dirty when the run started: the pre-run working tree is the
            # only correct "before", and we are holding it.
            before_text = before.contents[relative]
            existed = before.hashes.get(relative) is not None
        elif relative in before.hashes:
            # Dirty pre-run but past the capture ceiling: record that it existed
            # so the change is listed; undo will decline for want of content.
            before_text = None
            existed = before.hashes[relative] is not None
        else:
            # Clean before the run, so its content then is its content at HEAD.
            before_text = _head_text(root, relative)
            existed = before_text is not None

        absolute = root / relative
        try:
            store.insert_file_snapshot(
                {
                    "id": store.new_id(),
                    "session_id": session_id,
                    "repo_id": repo_id,
                    "relative_path": relative,
                    "existed_before": existed,
                    "before_text": before_text,
                    "before_newline": _newline(before_text),
                    "after_sha256": _hash_file(absolute),
                    "created_at": store.now_iso(),
                }
            )
            recorded.append(relative)
        except Exception:  # pragma: no cover - the run is done; this is bookkeeping
            _LOG.warning(
                "external_agent_snapshot_failed session=%s path=%s", session_id, relative
            )
    return recorded


def summarize(root: Path, before: RepoState, *, limit: int = 40) -> str:
    """A short human-readable change list, for handing to the next executor.

    Names and counts only. The next CLI can read the files itself and the diff is
    frequently larger than the context it is worth spending.
    """

    paths = changed_paths(root, before)
    if not paths:
        return "No files were changed."
    shown = paths[:limit]
    more = len(paths) - len(shown)
    lines = "\n".join(f"- {path}" for path in shown)
    return (
        f"{len(paths)} file(s) changed:\n{lines}"
        + (f"\n- ... and {more} more" if more > 0 else "")
    )


__all__ = [
    "RepoState",
    "capture",
    "changed_paths",
    "ensure_git_worktree",
    "record",
    "summarize",
]

"""Getting verified work out of a managed copy and into the user's repository.

This is the ``managed`` workspace's exit route. The copy is a dead end without
it: ``repos.register`` copies a filtered subset of text files into
``workspace_repos_dir``, ``git/safety.py`` forbids push/pull/remote and refuses
any operation that resolves to the original path, and nothing else writes back.

A ``live`` workspace needs none of this, because the agent edited the user's
files in the first place. What that workspace does need -- a change list, a diff
and an undo -- comes from ``agent_core.journal``, which reuses the ``FileChange``
and ``DeliveryPlan`` shapes below so ``build_patch`` serves both.

Three properties make writing back safe to offer:

* **Per file, never whole-tree.** The copy is a *subset* of the repository, so a
  tree-wide diff would read as deletions for every file that was never imported.
* **Drift-checked.** ``workspace_repo_files.sha256`` records what was imported.
  If the user has since edited a file, the recorded hash no longer matches and
  that file is refused rather than silently overwritten.
* **Never a git operation.** Delivery writes files. It does not stage, commit,
  branch, or contact a remote; the user reviews with their own tools.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from app.core import textio
from app.services.agent_core.workspace import WorkspaceError, repo_root
from app.services.repos import store as repos_store
from app.services.repos.scanner import IGNORED_DIRS, IGNORED_NAMES

#: How the finished work reaches the user. ``WRITE_BACK`` copies it out of the
#: managed workspace; ``LIVE`` means it never left, because the agent was
#: editing the user's own files all along.
WRITE_BACK = "write_back"
LIVE = "live"

#: An untracked file this size is build output or a binary the agent should not
#: have produced; reading it into a diff would help no one.
MAX_UNTRACKED_BYTES = 1024 * 1024


def delivery_mode(repo: dict) -> str:
    """A live folder has nothing to deliver; a managed copy is written back."""

    return LIVE if repo.get("access") == repos_store.LIVE else WRITE_BACK


@dataclass
class FileChange:
    relative_path: str
    original_relative_path: str
    status: str  # "modified" | "created"
    drifted: bool = False
    reason: str = ""
    managed_text: str = ""
    original_text: str = ""

    @property
    def deliverable(self) -> bool:
        return not self.drifted


@dataclass
class DeliveryPlan:
    repo_id: str
    original_root: str
    #: ``WRITE_BACK`` or ``LIVE``. Carried on the plan so every consumer --
    #: tool, HTTP route and UI -- branches on one decision made in one place.
    mode: str = WRITE_BACK
    changes: list[FileChange] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return self.mode == WRITE_BACK

    @property
    def deliverable(self) -> list[FileChange]:
        return [change for change in self.changes if change.deliverable]

    @property
    def blocked(self) -> list[FileChange]:
        return [change for change in self.changes if not change.deliverable]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> tuple[str, bytes] | None:
    """Decoded text for diffing, and the raw bytes the import hash was taken over.

    Both are returned because they answer different questions: the hash must be
    over what is actually on disk, while the diff has to be line-ending
    normalised or a CRLF file reads as entirely changed.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return textio.decode(raw), raw


def _untracked_changes(root: Path, known: set[str]) -> list[FileChange]:
    """Files the agent created, which have no import record to compare against.

    ``write_file`` writes into the managed copy without registering a row, so a
    plan built only from import records misses every new file -- which for a
    workspace that started empty is all of the work.
    """

    changes: list[FileChange] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in known or path.name in IGNORED_NAMES:
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_UNTRACKED_BYTES:
                continue
        except OSError:
            continue
        managed = _read(path)
        if managed is None:
            continue
        changes.append(
            FileChange(
                relative_path=relative,
                original_relative_path=relative,
                status="created",
                managed_text=managed[0],
                original_text="",
            )
        )
    return changes


def plan_delivery(repo_id: str) -> DeliveryPlan:
    """Work out which files changed in the copy and which are safe to deliver."""

    repo = repos_store.get_repo(repo_id)
    if repo is None or repo.get("deleted"):
        raise WorkspaceError("Repository not found.")
    root = repo_root(repo_id)
    mode = delivery_mode(repo)
    if mode == LIVE:
        # Nothing to plan: the agent wrote the user's files directly. What that
        # run changed is the journal's business, not a hash comparison against
        # import time, which would also report the user's own edits.
        raise WorkspaceError(
            "This workspace is your own folder, so these changes are already in it."
        )
    records, _total = repos_store.list_repo_files(repo_id, limit=100_000)

    original_root = Path(repo["original_path"]).resolve()

    plan = DeliveryPlan(repo_id=repo_id, original_root=str(original_root), mode=mode)
    for record in records:
        relative = record["relative_path"]
        managed_path = root / relative
        managed = _read(managed_path)
        if managed is None:
            # The agent cannot delete through the file tools, so a missing file
            # is a storage problem rather than an intended removal. Reporting it
            # as a deletion would be a destructive guess.
            continue
        managed_text, managed_bytes = managed
        baseline_hash = record.get("sha256") or ""
        if _sha256(managed_bytes) == baseline_hash:
            continue  # unchanged in the copy

        original_relative = record.get("original_relative_path") or relative
        original_path = original_root / original_relative
        original = _read(original_path)

        if original is None:
            plan.changes.append(
                FileChange(
                    relative_path=relative,
                    original_relative_path=original_relative,
                    status="created",
                    managed_text=managed_text,
                    original_text="",
                )
            )
            continue

        original_text, original_bytes = original
        if _sha256(original_bytes) != baseline_hash:
            plan.changes.append(
                FileChange(
                    relative_path=relative,
                    original_relative_path=original_relative,
                    status="modified",
                    drifted=True,
                    reason="This file changed in your repository after Neo imported it.",
                    managed_text=managed_text,
                    original_text=original_text,
                )
            )
            continue

        plan.changes.append(
            FileChange(
                relative_path=relative,
                original_relative_path=original_relative,
                status="modified",
                managed_text=managed_text,
                original_text=original_text,
            )
        )
    plan.changes.extend(
        _untracked_changes(root, {record["relative_path"] for record in records})
    )
    plan.changes.sort(key=lambda change: change.relative_path)
    return plan


def build_patch(plan: DeliveryPlan, only: list[str] | None = None) -> str:
    """A unified diff of the deliverable changes, applicable with ``git apply``."""

    selected = _select(plan, only)
    sections: list[str] = []
    for change in selected:
        diff = difflib.unified_diff(
            change.original_text.splitlines(keepends=True),
            change.managed_text.splitlines(keepends=True),
            fromfile=f"a/{change.original_relative_path}",
            tofile=f"b/{change.original_relative_path}",
            n=3,
        )
        body = "".join(diff)
        if not body:
            continue
        if not body.endswith("\n"):
            body += "\n"
        sections.append(
            f"diff --git a/{change.original_relative_path}"
            f" b/{change.original_relative_path}\n{body}"
        )
    return "".join(sections)


def _select(plan: DeliveryPlan, only: list[str] | None) -> list[FileChange]:
    deliverable = plan.deliverable
    if only is None:
        return deliverable
    wanted = set(only)
    chosen = [change for change in deliverable if change.relative_path in wanted]
    missing = wanted - {change.relative_path for change in chosen}
    if missing:
        raise WorkspaceError("These files cannot be delivered: " + ", ".join(sorted(missing)) + ".")
    return chosen


def write_to_working_tree(plan: DeliveryPlan, only: list[str] | None = None) -> dict:
    """Write deliverable files into the user's repository. No git, no commit.

    Drift is re-checked immediately before each write rather than trusting the
    plan: the plan may be minutes old, and the user may have opened the file in
    between.
    """

    if not plan.writable:
        # The last line of defence: a direct API call, a stale plan or a future
        # caller must not reach a filesystem write for a workspace that has no
        # copy to write out of.
        raise WorkspaceError(
            "This workspace is your own folder; its changes are already written."
        )
    selected = _select(plan, only)
    original_root = Path(plan.original_root)
    written: list[str] = []
    skipped: list[dict] = []

    for change in selected:
        target = original_root / change.original_relative_path
        resolved_parent = target.parent
        # Containment: the destination must stay inside the registered repository.
        try:
            probe = resolved_parent
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            if probe.resolve() != original_root and original_root not in probe.resolve().parents:
                skipped.append(
                    {"path": change.relative_path, "reason": "Path escapes the repository."}
                )
                continue
        except OSError as exc:
            skipped.append({"path": change.relative_path, "reason": str(exc)})
            continue

        if target.exists():
            current = _read(target)
            if current is None:
                skipped.append({"path": change.relative_path, "reason": "File could not be read."})
                continue
            if current[0] != change.original_text:
                skipped.append(
                    {
                        "path": change.relative_path,
                        "reason": "The file changed again since this delivery was planned.",
                    }
                )
                continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            textio.write_text(target, change.managed_text)
        except PermissionError:
            skipped.append(
                {
                    "path": change.relative_path,
                    "reason": (
                        "Permission denied. If Neo is in a container, set NEO_UID/NEO_GID "
                        "to your own user and restart."
                    ),
                }
            )
            continue
        except OSError as exc:
            skipped.append({"path": change.relative_path, "reason": str(exc)})
            continue
        written.append(change.relative_path)

    return {
        "written": written,
        "skipped": skipped,
        "blocked": [
            {"path": change.relative_path, "reason": change.reason} for change in plan.blocked
        ],
    }


def summarize(plan: DeliveryPlan) -> str:
    if not plan.changes:
        return "No files changed in the managed copy."
    lines = [f"{len(plan.deliverable)} file(s) ready to deliver:"]
    lines += [f"  {change.status}: {change.relative_path}" for change in plan.deliverable]
    if plan.blocked:
        lines.append(f"{len(plan.blocked)} file(s) blocked:")
        lines += [f"  {change.relative_path}: {change.reason}" for change in plan.blocked]
    return "\n".join(lines)

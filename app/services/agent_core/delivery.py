"""Getting verified work out of the managed copy and into the user's repository.

Until now the managed copy was a dead end. ``repos.register`` copies a filtered
subset of text files into ``workspace_repos_dir``, ``git/safety.py`` forbids
push/pull/remote and refuses any operation that resolves to the original path,
and nothing anywhere wrote back. An agent that can only edit a copy the user
cannot retrieve is not useful, so this module closes the loop.

Three properties make it safe to offer:

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
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.services.agent_core.workspace import WorkspaceError, repo_root
from app.services.repos import store as repos_store
from app.services.repos.scanner import IGNORED_DIRS, IGNORED_NAMES

#: Marker written as ``original_path`` by an uploaded repository. There is no
#: such directory on disk -- the folder lives on the user's machine, not the
#: server -- so write-back is not merely unwise for these, it is meaningless.
UPLOAD_SCHEME = "upload://"

#: How the finished work can leave the managed copy.
WRITE_BACK = "write_back"
DOWNLOAD = "download"

#: An untracked file this size is build output or a binary the agent should not
#: have produced; reading it into a diff would help no one.
MAX_UNTRACKED_BYTES = 1024 * 1024


def is_uploaded(repo: dict) -> bool:
    return str(repo.get("original_path") or "").startswith(UPLOAD_SCHEME)


def delivery_mode(repo: dict) -> str:
    """Uploaded folders can only be handed back; registered paths can be written."""

    return DOWNLOAD if is_uploaded(repo) else WRITE_BACK


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
    #: ``WRITE_BACK`` or ``DOWNLOAD``. Carried on the plan so every consumer --
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
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace"), raw


def _untracked_changes(root: Path, known: set[str]) -> list[FileChange]:
    """Files the agent created, which have no import record to compare against.

    ``write_file`` writes into the managed copy without registering a row, so a
    plan built only from import records misses every new file -- which for an
    uploaded or empty workspace is all of the work.
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
    records, _total = repos_store.list_repo_files(repo_id, limit=100_000)

    if mode == DOWNLOAD:
        # There is no original directory to compare against or write to, so the
        # import hash is the only baseline and every change is handed back.
        plan = DeliveryPlan(repo_id=repo_id, original_root="", mode=mode)
        known: set[str] = set()
        for record in records:
            relative = record["relative_path"]
            known.add(relative)
            managed = _read(root / relative)
            if managed is None:
                continue
            managed_text, managed_bytes = managed
            if _sha256(managed_bytes) == (record.get("sha256") or ""):
                continue
            plan.changes.append(
                FileChange(
                    relative_path=relative,
                    original_relative_path=relative,
                    status="modified",
                    managed_text=managed_text,
                    original_text="",
                )
            )
        plan.changes.extend(_untracked_changes(root, known))
        plan.changes.sort(key=lambda change: change.relative_path)
        return plan

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
        # caller must not be able to turn an uploaded folder into a filesystem
        # write. `upload://...` is not a path, and treating it as one would
        # create junk directories wherever the server happens to be running.
        raise WorkspaceError(
            "This repository was uploaded, so it has no folder on this machine to "
            "write into. Download the changed files instead."
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
            target.write_text(change.managed_text, encoding="utf-8")
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


def build_archive(
    plan: DeliveryPlan, only: list[str] | None = None, *, whole_workspace: bool = False
) -> bytes:
    """A zip of the work, for a repository Neo cannot write back to.

    ``whole_workspace`` returns everything the agent can see rather than only
    what changed -- what you want when the upload started empty and "changed
    files" and "the project" are the same thing anyway.
    """

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if whole_workspace:
            root = repo_root(plan.repo_id)
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                if any(part in IGNORED_DIRS for part in path.parts):
                    continue
                archive.write(path, arcname=relative)
        else:
            for change in _select(plan, only):
                archive.writestr(change.relative_path, change.managed_text)
    return buffer.getvalue()


def summarize(plan: DeliveryPlan) -> str:
    if not plan.changes:
        return "No files changed in the managed copy."
    lines = [f"{len(plan.deliverable)} file(s) ready to deliver:"]
    lines += [f"  {change.status}: {change.relative_path}" for change in plan.deliverable]
    if plan.blocked:
        lines.append(f"{len(plan.blocked)} file(s) blocked:")
        lines += [f"  {change.relative_path} — {change.reason}" for change in plan.blocked]
    return "\n".join(lines)

"""What a run changed, and how to put it back.

A live workspace is the user's own folder, so the managed copy's two safety
properties disappear at once: there is no pristine baseline to diff against, and
a bad edit is not confined to a directory Neo can throw away. This module
restores both by recording each file's content the first time a session touches
it.

That one record does three jobs:

* **The change list.** Derived from what the agent actually wrote, not from
  comparing hashes against import time, so it stays accurate across repeated
  runs on the same folder and never reports a file the user edited themselves.
* **The diff.** Snapshot versus current is a true before/after for this run.
* **Undo.** Restoring the snapshots reverses the run, including deleting files
  that did not exist before it.

Only the *first* touch is stored, which is what makes "before" mean before the
run rather than before the most recent edit within it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.core import textio
from app.services.agent_core import store
from app.services.agent_core.delivery import DeliveryPlan, FileChange
from app.services.agent_core.workspace import WorkspaceError, repo_root, resolve

#: Bind mounts keep the host's ownership, so a container running as a different
#: uid than the folder's owner can read every file and write none of them. The
#: error the OS gives for that is "Permission denied", which reads as a Neo bug
#: rather than a mount that needs one flag.
_PERMISSION_HINT = (
    "{path}: permission denied. If Neo is in a container, the mounted folder is "
    "owned by a different user than the container runs as -- set NEO_UID/NEO_GID "
    "to your own (`id -u`/`id -g`) and restart."
)

#: A file this size is not something a text agent meaningfully edited, and
#: holding two copies of it in SQLite to enable an undo nobody wants is a poor
#: trade. Above it the snapshot records only that the file existed.
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        return textio.read_text(path)
    except OSError:
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_before(session_id: str, repo_id: str, root: Path, relative_path: str) -> None:
    """Capture a file's pre-edit state, once per session.

    Deliberately best-effort: a journal failure must not stop the edit the user
    asked for. The cost of that choice is a file missing from an undo, which is
    strictly better than a write tool that fails because bookkeeping did.
    """

    if not session_id or not repo_id:
        return
    path = root / relative_path
    existed = path.is_file()
    try:
        store.insert_file_snapshot(
            {
                "id": store.new_id(),
                "session_id": session_id,
                "repo_id": repo_id,
                "relative_path": relative_path,
                "existed_before": existed,
                "before_text": _read_text(path) if existed else None,
                "before_newline": textio.detect_newline(path) if existed else textio.LF,
                "created_at": store.now_iso(),
            }
        )
    except Exception:
        return


def record_after(session_id: str, root: Path, relative_path: str) -> None:
    """Note what the run left in a file, so a later outside edit is detectable.

    Best-effort for the same reason as ``record_before``: losing this costs a
    file its undo, and that is a better failure than refusing the write.
    """

    if not session_id:
        return
    current = _read_text(root / relative_path)
    if current is None:
        return
    try:
        store.record_snapshot_result(session_id, relative_path, _sha256(current))
    except Exception:
        return


def session_changes(session_id: str, repo_id: str) -> DeliveryPlan:
    """This run's changes, in the shape the diff builder already understands.

    Reusing ``DeliveryPlan``/``FileChange`` is the point: ``delivery.build_patch``
    then produces the unified diff for a live run with no second implementation.
    """

    root = repo_root(repo_id)
    plan = DeliveryPlan(repo_id=repo_id, original_root=str(root), mode="live")
    for record in store.list_file_snapshots(session_id):
        relative = record["relative_path"]
        try:
            path = resolve(root, relative)
        except WorkspaceError:
            continue
        current = _read_text(path) if path.is_file() else None
        before = record["before_text"] or ""
        existed = bool(record["existed_before"])
        if current is None:
            # Written during the run and since removed. There is no "after" to
            # show, but the file is still part of what the run did, so it is
            # reported rather than dropped.
            status = "deleted"
            current_text = ""
        else:
            status = "modified" if existed else "created"
            current_text = current
        if existed and current_text == before:
            continue  # touched, then returned to where it started
        plan.changes.append(
            FileChange(
                relative_path=relative,
                original_relative_path=relative,
                status=status,
                managed_text=current_text,
                original_text=before,
            )
        )
    plan.changes.sort(key=lambda change: change.relative_path)
    return plan


def undo(session_id: str, repo_id: str) -> dict:
    """Put the folder back the way this run found it.

    Drift is checked per file immediately before touching it, the same
    discipline delivery applies: if the content no longer matches what the run
    left behind, someone else has edited it since, and overwriting that would
    destroy work the run never made. Those files are reported, not restored.
    """

    root = repo_root(repo_id)
    restored: list[str] = []
    removed: list[str] = []
    skipped: list[dict] = []

    for record in store.list_file_snapshots(session_id):
        relative = record["relative_path"]
        try:
            path = resolve(root, relative)
        except WorkspaceError as exc:
            skipped.append({"path": relative, "reason": str(exc)})
            continue

        existed = bool(record["existed_before"])
        before = record["before_text"]
        if existed and before is None:
            skipped.append(
                {"path": relative, "reason": "This file was too large to snapshot."}
            )
            continue

        # Drift: if the file no longer holds what the run left, someone has
        # edited it since. Undoing would then destroy work this run never made,
        # so the file is reported and left exactly as it is.
        expected = record["after_sha256"]
        current = _read_text(path) if path.is_file() else None
        if expected and current is not None and _sha256(current) != expected:
            skipped.append(
                {"path": relative, "reason": "This file changed after the run finished."}
            )
            continue

        if not existed:
            if current is None:
                continue  # already gone; nothing to undo
            try:
                path.unlink()
                removed.append(relative)
            except PermissionError:
                skipped.append({"path": relative, "reason": _PERMISSION_HINT.format(path=relative)})
            except OSError as exc:
                skipped.append({"path": relative, "reason": str(exc)})
            continue

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            textio.write_text(path, before, newline=record["before_newline"] or textio.LF)
            restored.append(relative)
        except PermissionError:
            skipped.append({"path": relative, "reason": _PERMISSION_HINT.format(path=relative)})
        except OSError as exc:
            skipped.append({"path": relative, "reason": str(exc)})

    # Only a clean undo clears the journal. Leaving it in place when something
    # was skipped keeps the remaining changes visible and retryable rather than
    # making the panel claim the run left nothing behind.
    if not skipped:
        store.clear_file_snapshots(session_id)
    return {"restored": restored, "removed": removed, "skipped": skipped}

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
from app.services.agent_core.workspace import repo_root
from app.services.isolation import fs_ops, safe_fs

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


def _read_raw(root: Path, relative_path: str) -> bytes | None:
    try:
        raw = safe_fs.read_file(root, relative_path)
        return raw if len(raw) <= MAX_SNAPSHOT_BYTES else None
    except FileNotFoundError:
        return None


def _read_text(root: Path, relative_path: str) -> str | None:
    raw = _read_raw(root, relative_path)
    return textio.decode(raw) if raw is not None else None


def _newline(raw: bytes) -> str:
    index = raw.find(b"\n")
    return textio.CRLF if index > 0 and raw[index - 1 : index] == b"\r" else textio.LF


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
    try:
        raw = _read_raw(root, relative_path)
        # A very large file still needs an "existed" snapshot row so undo
        # knows not to delete it as a file the run created.
        existed = raw is not None or safe_fs.exists(root, relative_path)
        store.insert_file_snapshot(
            {
                "id": store.new_id(),
                "session_id": session_id,
                "repo_id": repo_id,
                "relative_path": relative_path,
                "existed_before": existed,
                "before_text": textio.decode(raw) if raw is not None else None,
                "before_newline": _newline(raw) if raw is not None else textio.LF,
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
    try:
        current = _read_text(root, relative_path)
    except OSError:
        return
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
            current = _read_text(root, relative)
        except OSError:
            continue
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
            current_raw = _read_raw(root, relative)
        except OSError:
            skipped.append({"path": relative, "reason": "This path is unsafe or unreadable."})
            continue

        existed = bool(record["existed_before"])
        before = record["before_text"]
        if existed and before is None:
            skipped.append({"path": relative, "reason": "This file was too large to snapshot."})
            continue

        # Drift: if the file no longer holds what the run left, someone has
        # edited it since. Undoing would then destroy work this run never made,
        # so the file is reported and left exactly as it is.
        expected = record["after_sha256"]
        current = textio.decode(current_raw) if current_raw is not None else None
        if expected and (current is None or _sha256(current) != expected):
            skipped.append(
                {"path": relative, "reason": "This file changed after the run finished."}
            )
            continue
        if not expected and current is not None:
            skipped.append(
                {"path": relative, "reason": "This file changed after the run finished."}
            )
            continue

        # Written through the anchored walk rather than by path. Every component of
        # this path was under the agent's control a moment ago, so a parent could
        # now be a symlink pointing out of the folder, and a plain write would
        # follow it. `safe_fs` opens each component relative to the last and refuses
        # links, so an undo cannot be redirected into Neo's own data.
        if not existed:
            if current is None:
                continue  # already gone; nothing to undo
            try:
                fs_ops.apply(root, relative, replacement=None, expected=current_raw)
                removed.append(relative)
            except safe_fs.UnsafePath as exc:
                skipped.append({"path": relative, "reason": str(exc)})
            except PermissionError:
                skipped.append({"path": relative, "reason": _PERMISSION_HINT.format(path=relative)})
            except OSError as exc:
                skipped.append({"path": relative, "reason": str(exc)})
            continue

        try:
            fs_ops.apply(
                root,
                relative,
                replacement=textio.encode(before, record["before_newline"] or textio.LF),
                expected=current_raw,
            )
            restored.append(relative)
        except safe_fs.UnsafePath as exc:
            skipped.append({"path": relative, "reason": str(exc)})
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

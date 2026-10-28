"""Version-bound, user-reviewed delivery from a private scoped agent copy."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from app.core.config import active_profile_storage_dir
from app.services.agent_core import delivery, store
from app.services.agent_core.workspace import repo_root
from app.services.disclosure import approvals
from app.services.disclosure.artifacts import ArtifactLabels
from app.services.disclosure.scoped_copy import manifest_digest
from app.services.isolation import fs_ops, safe_fs
from app.services.isolation.roots import data_root, overlaps_protected
from app.services.repos import store as repos_store
from app.services.repos.migration import _manifest
from app.services.repos.safety import validate_repo_root

MAX_TEXT_BYTES = 1024 * 1024
LIFETIME_SECONDS = 600


class ScopedDeliveryError(ValueError):
    pass


def _context(profile_id: str, session_id: str) -> tuple[dict, Path, Path, Path, dict]:
    session = store.get_session(session_id)
    if not session or session.get("status") != "completed" or not session.get("repo_id"):
        raise ScopedDeliveryError("completed_scoped_run_required")
    meta = (session.get("external_meta") or {}).get(session["executor"]) or {}
    expected_copy = data_root() / "workspaces" / profile_id / "scoped" / session_id
    if meta.get("scoped_workspace") != str(expected_copy) or not meta.get("source_manifest_hash"):
        raise ScopedDeliveryError("scoped_run_required")
    source = repo_root(session["repo_id"])
    repo = repos_store.get_repo(session["repo_id"])
    if repo is None or repo.get("deleted"):
        raise ScopedDeliveryError("repository_unavailable")
    target = validate_repo_root(repo["original_path"])
    if overlaps_protected(target, profile_id=profile_id):
        raise ScopedDeliveryError("destination_overlaps_protected_storage")
    if not target.is_dir() or not expected_copy.is_dir():
        raise ScopedDeliveryError("delivery_tree_missing")
    if manifest_digest(source) != meta["source_manifest_hash"]:
        raise ScopedDeliveryError("source_workspace_changed_since_run")
    return session, source, expected_copy, target, meta


def _target_bytes(root: Path, relative: str) -> bytes | None:
    try:
        return safe_fs.read_file(root, relative)
    except (FileNotFoundError, safe_fs.UnsafePath) as exc:
        path = root / relative
        if path.exists() or path.is_symlink():
            raise ScopedDeliveryError(f"unsafe_delivery_path:{relative}") from exc
        return None


def _plan(profile_id: str, session_id: str) -> tuple[delivery.DeliveryPlan, dict]:
    session, source, copy, target, meta = _context(profile_id, session_id)
    if manifest_digest(source) != meta["source_manifest_hash"]:
        raise ScopedDeliveryError("source_workspace_changed_since_run")
    source_files = _manifest(source)
    copy_files = _manifest(copy)
    changed = sorted(path for path in set(source_files) | set(copy_files)
                     if source_files.get(path) != copy_files.get(path)
                     and not (path == ".git" or path.startswith(".git/")))
    if len(changed) > 2000:
        raise ScopedDeliveryError("too_many_changed_files")
    plan = delivery.DeliveryPlan(
        repo_id=session["repo_id"], original_root=str(target), mode=delivery.WRITE_BACK
    )
    versions: dict[str, str | None] = {}
    copy_versions: dict[str, str] = {}
    for relative in changed:
        entry = copy_files.get(relative)
        if entry is not None and entry[1] == "directory":
            continue
        if entry is None:
            raise ScopedDeliveryError(
                f"deletions_or_directory_changes_need_manual_review:{relative}"
            )
        if entry[0] > MAX_TEXT_BYTES:
            raise ScopedDeliveryError(f"file_too_large_for_review:{relative}")
        raw = safe_fs.read_file(copy, relative)
        current = _target_bytes(target, relative)
        if current is not None and len(current) > MAX_TEXT_BYTES:
            raise ScopedDeliveryError(f"destination_file_too_large:{relative}")
        try:
            new_text = raw.decode("utf-8")
            old_text = current.decode("utf-8") if current is not None else ""
        except UnicodeDecodeError as exc:
            raise ScopedDeliveryError(f"binary_file_needs_manual_review:{relative}") from exc
        versions[relative] = hashlib.sha256(current).hexdigest() if current is not None else None
        copy_versions[relative] = entry[1]
        if raw != current:
            plan.changes.append(delivery.FileChange(
                relative_path=relative, original_relative_path=relative,
                status="modified" if current is not None else "created",
                managed_text=new_text, original_text=old_text,
            ))
    patch = delivery.build_patch(plan)
    binding = {
        "source": meta["source_manifest_hash"],
        "copy": manifest_digest(copy),
        "versions": versions,
        "copy_versions": copy_versions,
        "patch_hash": hashlib.sha256(patch.encode()).hexdigest(),
    }
    return plan, binding


def _table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scoped_delivery_reviews (
            id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL,
            binding TEXT NOT NULL, status TEXT NOT NULL, expires_at REAL NOT NULL
        )
    """)


def preview(profile_id: str, session_id: str) -> dict:
    plan, binding = _plan(profile_id, session_id)
    patch = delivery.build_patch(plan)
    review_id = uuid.uuid4().hex
    with closing(approvals._connect(profile_id)) as conn, conn:
        _table(conn)
        conn.execute(
            "INSERT INTO scoped_delivery_reviews VALUES (?,?,?,?,?,?)",
            (review_id, profile_id, session_id, json.dumps(binding, sort_keys=True),
             "preview", time.time() + LIFETIME_SECONDS),
        )
    return {
        "id": review_id, "patch": patch,
        "files": [change.relative_path for change in plan.changes],
        "expires_at": time.time() + LIFETIME_SECONDS,
    }


def approve_and_apply(profile_id: str, session_id: str, review_id: str) -> dict:
    plan, binding = _plan(profile_id, session_id)
    if not plan.changes:
        raise ScopedDeliveryError("no_changes_to_deliver")
    with closing(approvals._connect(profile_id)) as conn, conn:
        _table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM scoped_delivery_reviews WHERE id=? AND profile_id=? AND session_id=?",
            (review_id, profile_id, session_id),
        ).fetchone()
        if not row or row["status"] != "preview" or row["expires_at"] <= time.time():
            raise ScopedDeliveryError("review_expired_or_used")
        if json.loads(row["binding"]) != binding:
            raise ScopedDeliveryError("files_changed_since_review")
        conn.execute(
            "UPDATE scoped_delivery_reviews SET status='consumed' WHERE id=?", (review_id,)
        )
    storage = active_profile_storage_dir.get()
    if not storage or Path(storage).name != profile_id:
        raise ScopedDeliveryError("profile_scope_required")
    labels = ArtifactLabels(Path(storage) / "artifact-labels.json")
    source_label = labels.get(f"scoped:{profile_id}:{session_id}")
    if source_label.is_unknown():
        raise ScopedDeliveryError("scoped_copy_label_missing")
    repo_id = plan.repo_id
    labels.raise_label(f"repo:{repo_id}", source_label)
    target = Path(plan.original_root)
    identity = target.stat()
    labels.raise_label(f"folder:{identity.st_dev}:{identity.st_ino}", source_label)
    written: list[str] = []
    skipped: list[dict] = []
    copy = data_root() / "workspaces" / profile_id / "scoped" / session_id
    for change in plan.changes:
        relative = change.relative_path
        try:
            current = _target_bytes(target, relative)
            current_hash = hashlib.sha256(current).hexdigest() if current is not None else None
            if current_hash != binding["versions"][relative]:
                skipped.append({"path": relative, "reason": "Destination changed after review."})
                continue
            raw = safe_fs.read_file(copy, relative)
            if hashlib.sha256(raw).hexdigest() != binding["copy_versions"][relative]:
                skipped.append({"path": relative, "reason": "Scoped copy changed after review."})
                continue
            fs_ops.apply(target, relative, replacement=raw, expected=current)
            written.append(relative)
        except (OSError, ValueError) as exc:
            skipped.append({"path": relative, "reason": str(exc)})
    return {
        "summary": delivery.summarize(plan), "written": written,
        "skipped": skipped, "blocked": [],
    }

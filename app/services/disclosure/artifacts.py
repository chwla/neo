"""Labels for workspace artifacts, which must survive copy and reuse.

The contract (docs/security/data-isolation.md §6) requires that once private
material has reached a workspace, git store or thread-state directory, that
restriction cannot be shed by copying, renaming, reusing or deleting a file. The
authoritative record lives outside anything an agent can write.

This module is the record. It is monotonic: :meth:`ArtifactLabels.raise_label`
only ever unions in more restriction, :meth:`copy` carries a source's restriction
into a destination, and an artifact with no record reads back as UNKNOWN (the
conservative default). A brand-new workspace that never received private data is
labelled PUBLIC explicitly by the code that creates it -- not by the absence of a
record here.

Storage is a small JSON file written atomically. In tests it lives under a
temporary path; in production it belongs in PRIVATE storage.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.services.disclosure.labels import Label, from_json, join, to_json


class ArtifactLabels:
    _fallback_lock = threading.RLock()

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._records: dict[str, str] = {}

    @contextmanager
    def _locked(self, *, exclusive: bool):
        """Serialize readers and writers across Neo worker processes."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.is_symlink():
            raise RuntimeError("artifact label store is a link")
        lock_path = self._path.with_name(self._path.name + ".lock")
        if lock_path.is_symlink():
            raise RuntimeError("artifact label lock is a link")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                import fcntl
            except ImportError:  # Windows has no external-engine backend.
                with self._fallback_lock:
                    yield
            else:
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._records = {}
            return
        except (OSError, ValueError) as exc:
            raise RuntimeError("artifact label store cannot be read") from exc
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("artifact label store is corrupt") from exc
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in data.items()
        ):
            raise RuntimeError("artifact label store is corrupt")
        self._records = data

    def _flush(self) -> None:
        tmp = self._path.with_name(self._path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._records, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._path)
            parent_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _current(self, artifact_id: str) -> Label:
        raw = self._records.get(artifact_id)
        return from_json(raw) if raw is not None else Label.unknown("no_artifact_record")

    def get(self, artifact_id: str) -> Label:
        with self._locked(exclusive=False):
            self._load()
            return self._current(artifact_id)

    def raise_label(self, artifact_id: str, label: Label) -> Label:
        """Union ``label`` into the artifact's record. Never lowers it."""

        with self._locked(exclusive=True):
            self._load()
            current = self._current(artifact_id)
            if current.is_unknown() and self._records.get(artifact_id) is None:
                combined = label
            else:
                combined = join(current, label)
            self._records[artifact_id] = to_json(combined)
            self._flush()
            return combined

    def copy(self, src_id: str, dst_id: str) -> Label:
        """Carry the source's restriction into the destination (monotonically)."""

        with self._locked(exclusive=True):
            self._load()
            source = self._current(src_id)
            current = self._current(dst_id)
            combined = source if dst_id not in self._records else join(current, source)
            self._records[dst_id] = to_json(combined)
            self._flush()
            return combined


__all__ = ["ArtifactLabels"]

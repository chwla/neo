"""The provenance of a task, held by the server.

A task is a private asset in the data-isolation contract whoever wrote it, so every
task is at least ``PRIVATE{task:<id>}``. A task *made from* something -- an imported
GitHub issue today -- is also everything that something was: its record is raised to
the source's label when it is created, and records only ever rise.

A task linked to an imported item but with no record is the crash window between
writing the task and recording its label, or a task made before labels existed.
Either way its real provenance is not known, and it is treated as UNKNOWN rather than
as an ordinary private task, because "ordinary" would quietly drop the connector and
credential tags of what is inside it.
"""

from __future__ import annotations

from pathlib import Path

from app.services.disclosure import Label, join
from app.services.disclosure.artifacts import ArtifactLabels

#: Link types that mean "this task's text was copied from somewhere else".
DERIVED_LINK_TYPES = frozenset({"github_item"})

_NO_RECORD = "no_artifact_record"


def _labels_path() -> Path:
    from app.core.config import active_profile_storage_dir
    from app.services.isolation.roots import roots

    storage = active_profile_storage_dir.get()
    base = Path(storage) if storage else roots().data_root
    return base / "artifact-labels.json"


def _key(task_id: str) -> str:
    return f"task:{task_id}"


def task_label(task_id: str) -> Label:
    """The task's provenance: never PUBLIC, never lower than what it was made from."""

    base = Label.private(_key(task_id))
    record = ArtifactLabels(_labels_path()).get(_key(task_id))
    if record.reason != _NO_RECORD:
        return join(base, record)
    from app.services.tasks import store as task_store

    links = task_store.list_links(task_id) or []
    if any(link.get("link_type") in DERIVED_LINK_TYPES for link in links):
        return Label.unknown("derived_task_without_label")
    return base


def raise_task_label(task_id: str, label: Label) -> Label:
    """Record what a task was made from. Monotonic: it can only ever rise."""

    return ArtifactLabels(_labels_path()).raise_label(_key(task_id), label)


__all__ = ["DERIVED_LINK_TYPES", "raise_task_label", "task_label"]

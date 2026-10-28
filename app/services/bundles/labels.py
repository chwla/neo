"""Conservative provenance for archives that can outlive their source rows."""

from __future__ import annotations

from pathlib import Path

from app.core.config import active_profile_storage_dir
from app.services.disclosure.artifacts import ArtifactLabels
from app.services.disclosure.labels import Label, join


def store() -> ArtifactLabels:
    storage = active_profile_storage_dir.get()
    if not storage:
        raise RuntimeError("profile scope is required for bundle labels")
    return ArtifactLabels(Path(storage) / "artifact-labels.json")


def source_label(entities: dict[str, list[dict]], labels: ArtifactLabels) -> Label:
    """Join server records for every exported root; missing records stay UNKNOWN."""

    sources: list[Label] = []
    for key, prefix in (
        ("agent_sessions", "session"),
        ("tasks", "task"),
        ("projects", "project"),
    ):
        for item in entities.get(key, []):
            if not isinstance(item, dict) or not item.get("id"):
                sources.append(Label.unknown("bundle_entity_without_identity"))
            else:
                sources.append(labels.get(f"{prefix}:{item['id']}"))
    return join(*sources) if sources else Label.unknown("bundle_without_labelled_source")


__all__ = ["source_label", "store"]

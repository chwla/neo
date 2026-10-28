"""Server-side labels for external-engine output and its derived transcript."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.disclosure import Label


def output_label(session: Mapping[str, Any], session_id: str, executor: str) -> Label:
    """Carry every disclosure in a handoff chain into its resulting answer."""

    all_meta = session.get("external_meta") or {}
    if not isinstance(all_meta, Mapping):
        return Label.unknown("malformed_external_metadata")
    tags = {f"lineage:{executor}:{session_id}"}
    for engine, metadata in all_meta.items():
        if not isinstance(engine, str) or not isinstance(metadata, Mapping):
            return Label.unknown("malformed_external_metadata")
        tags.add(f"lineage:{engine}:{session_id}")
        disclosures = metadata.get("disclosure_tags") or []
        if not isinstance(disclosures, list) or not all(
            isinstance(tag, str) for tag in disclosures
        ):
            return Label.unknown("malformed_disclosure_tags")
        tags.update(disclosures)
    return Label.private(*tags)


__all__ = ["output_label"]

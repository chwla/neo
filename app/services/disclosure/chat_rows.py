"""Provenance carried by persisted chat rows and their derived copies."""

from __future__ import annotations

import json
from collections.abc import Iterable

from app.services.disclosure.labels import Label, from_json, join, to_json


def metadata(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def stored_label(raw: str | None) -> Label:
    return from_json(metadata(raw).get("source_label"))


def derived_metadata(raw: str | None, source: Label) -> str:
    body = metadata(raw)
    body["source_label"] = to_json(join(stored_label(raw), source))
    return json.dumps(body, sort_keys=True)


def joined_rows(rows: Iterable) -> Label:
    labels = [stored_label(row.metadata_json) for row in rows]
    return join(*labels) if labels else Label.unknown("empty_chat_derivation")


__all__ = ["derived_metadata", "joined_rows", "metadata", "stored_label"]

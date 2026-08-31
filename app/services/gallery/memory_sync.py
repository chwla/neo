"""Publishing gallery items into Neo's workspace memory.

This is what makes the gallery part of Neo rather than a tab beside it. Once an
item is here, ``recall_memory``, ``POST /api/workspace-memory/retrieve`` and the
per-scope memory views find it with no gallery-specific code -- an image becomes
one more thing Neo remembers, alongside decisions, failures and notes.

Writes are keyed by a deterministic id derived from the gallery item, so a
re-describe or a hand-edited caption updates the existing row. ``upsert_item``
matches on the supplied id before falling back to its content key, which would
otherwise treat a changed title as a different memory and leave a stale twin
behind.
"""

from __future__ import annotations

import uuid

from app.services.gallery import store

#: Stable namespace so an item's memory id is the same on every machine and
#: across restarts. Any fixed UUID would do; this one is arbitrary and permanent.
_NAMESPACE = uuid.UUID("6f3b1d64-4f4c-4c0a-9d5e-2c8a7b6e1f90")

SOURCE_TYPE = "gallery_image"
MEMORY_TYPE = "visual_reference"


def memory_id_for(item_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"gallery:{item_id}"))


def _scope_for(item_id: str) -> tuple[str, str]:
    """Where this image belongs in memory.

    The most recent conversation it appeared in, so it shows up in that chat's
    related-memory view. Items that never appeared in a chat -- a direct upload --
    sit in a user-level scope. Neither choice hides the item from an unscoped
    query, which is how recall usually arrives.
    """

    for appearance in store.list_appearances(item_id):
        if appearance.get("chat_id") is not None:
            return "chat", str(appearance["chat_id"])
        if appearance.get("project_id"):
            return "project", str(appearance["project_id"])
    return "user", "gallery"


def _content_text(item: dict, appearances: list[dict]) -> str:
    """The searchable body.

    The OCR transcript goes in whole: it is the field that answers "the one where
    the approval button was broken", and truncating it is what would make that
    query fail.
    """

    lines = [item.get("caption") or "", item.get("ocr_text") or ""]
    if item.get("tags"):
        lines.append("Tags: " + ", ".join(item["tags"]))
    seen = [a["seen_at"][:10] for a in appearances if a.get("seen_at")]
    if seen:
        lines.append(f"Seen: {', '.join(sorted(set(seen), reverse=True)[:6])}")
    return "\n".join(line for line in lines if line.strip()) or (
        item.get("title") or "Image with no description yet."
    )


def sync_item(item_id: str) -> dict | None:
    """Write or refresh this item's memory row. Never raises for the caller."""

    item = store.get_item(item_id)
    if not item:
        return None
    from app.services.memory_retrieval import store as memory_store

    appearances = store.list_appearances(item_id)
    scope_type, scope_id = _scope_for(item_id)
    tags = ["gallery", "image", *(item.get("tags") or [])]
    return memory_store.upsert_item(
        {
            "id": memory_id_for(item_id),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "source_type": SOURCE_TYPE,
            "source_id": item_id,
            "memory_type": MEMORY_TYPE,
            "title": item.get("title") or "Untitled image",
            "content_text": _content_text(item, appearances),
            "content_json": {
                "gallery_item_id": item_id,
                "width": item.get("width"),
                "height": item.get("height"),
                "origin": item.get("origin"),
                "chat_ids": sorted(
                    {a["chat_id"] for a in appearances if a.get("chat_id") is not None}
                ),
            },
            "tags": tags[:30],
            "importance": 3,
            "confidence": 0.9,
        }
    )


def forget_item(item_id: str) -> bool:
    from app.services.memory_retrieval import store as memory_store

    return memory_store.delete_item(memory_id_for(item_id))

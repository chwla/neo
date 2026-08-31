"""Tools that let the agent reach what Neo has seen.

Like every other tool in this package, these are thin adapters: the ranking lives
in ``gallery/search.py`` and the storage in ``gallery/store.py``. They exist so
the model can go and look for an image on its own initiative rather than having
one pasted into its prompt by a hardcoded step.
"""

from __future__ import annotations

from app.services.agent_core.tools.base import AgentTool, ToolContext

_LIMIT = 6000


def _describe_hit(entry: dict) -> str:
    item = entry["item"]
    parts = [f"{item['id']} — {item.get('title') or 'untitled'}"]
    if item.get("caption"):
        parts.append(f"  {item['caption']}")
    seen = [a for a in entry.get("appearances", []) if a.get("seen_at")]
    if seen:
        where = seen[0]
        when = str(where["seen_at"])[:10]
        chat = f" in chat {where['chat_id']}" if where.get("chat_id") is not None else ""
        parts.append(f"  seen {when}{chat}")
    if item.get("ocr_text"):
        transcript = " ".join(item["ocr_text"].split())[:200]
        parts.append(f"  text in image: {transcript}")
    return "\n".join(parts)


def search_gallery(arguments: dict, context: ToolContext) -> str:
    from app.services.gallery.search import GallerySearch

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("A search query is required.")
    limit = max(1, min(int(arguments.get("limit") or 6), 20))
    try:
        outcome = GallerySearch().search(
            query,
            chat_id=arguments.get("chat_id"),
            project_id=context.project_id,
            since=arguments.get("since"),
            until=arguments.get("until"),
            limit=limit,
        )
    except Exception as exc:
        return f"The gallery is unavailable ({exc})."
    results = outcome.get("results") or []
    if not results:
        window = outcome.get("window")
        if window:
            return (
                f"No image matched that, searching {window['start'][:10]} to "
                f"{window['end'][:10]}. Try without the time range."
            )
        return "(no image in the gallery matched that)"
    lines = [_describe_hit(entry) for entry in results]
    return "\n".join(lines)[:_LIMIT]


def view_image(arguments: dict, _context: ToolContext) -> str:
    """What an image contains, in words.

    Returns the stored description rather than the pixels. An image that was
    never successfully described says so, so the model asks the user rather than
    inventing what is in it.
    """

    from app.services.gallery.service import GalleryService

    image_id = str(arguments.get("image_id") or "").strip()
    if not image_id:
        raise ValueError("An image_id is required.")
    try:
        detail = GalleryService().detail(image_id)
    except LookupError as exc:
        raise ValueError(f"No gallery image with id {image_id}.") from exc
    item = detail["item"]
    if item.get("description_status") != "ready":
        status = item.get("description_status")
        reason = item.get("description_error") or "it has not been described yet"
        return (
            f"{item.get('title') or image_id}: no description is available "
            f"({status} — {reason}). Ask the user what it shows rather than guessing."
        )
    lines = [
        f"Title: {item.get('title')}",
        f"Caption: {item.get('caption')}",
    ]
    if item.get("tags"):
        lines.append(f"Tags: {', '.join(item['tags'])}")
    if item.get("ocr_text"):
        lines.append(f"Text in image:\n{item['ocr_text']}")
    for appearance in detail["appearances"][:5]:
        chat = appearance.get("chat_id")
        lines.append(
            f"Seen {str(appearance['seen_at'])[:10]}"
            + (f" in chat {chat}" if chat is not None else "")
        )
    return "\n".join(line for line in lines if line)[:_LIMIT]


TOOLS = [
    AgentTool(
        name="search_gallery",
        description=(
            "Search images the user has shown Neo, by what is in them. Matches the "
            "description, any text visible in the image, tags, and the conversation "
            "it appeared in. A time phrase in the query ('last week', 'in March') "
            "narrows by when it was seen. Use this when the user refers to an image "
            "from an earlier conversation rather than asking them to send it again."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "chat_id": {"type": "integer"},
                "since": {"type": "string", "description": "ISO date lower bound"},
                "until": {"type": "string", "description": "ISO date upper bound"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        risk="read",
        handler=search_gallery,
        summary=lambda a: f"Search the gallery for {a.get('query')!r}",
    ),
    AgentTool(
        name="view_image",
        description=(
            "Read the stored description and transcribed text of one gallery image, "
            "by its id. Use after search_gallery to look at a specific result."
        ),
        parameters={
            "type": "object",
            "properties": {"image_id": {"type": "string"}},
            "required": ["image_id"],
        },
        risk="read",
        handler=view_image,
        summary=lambda a: f"View image {a.get('image_id')}",
    ),
]

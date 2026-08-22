"""An optional, run-scoped checklist the agent owns.

Deliberately *not* Neo's Tasks. The previous design turned every objective into
persistent Task rows before any inspection had happened, which fixed the plan
before the agent knew anything. This list lives and dies with the run, is
rewritten as the agent learns, and reaches Tasks only if the user exports it.

It is also optional. A one-file change should just be made; manufacturing a
checklist for it is noise, and the tool description says so.
"""

from __future__ import annotations

from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.agent_core.types import TodoItem

MAX_ITEMS = 30

_MARKS = {"completed": "x", "in_progress": "~", "pending": " "}


def _mark(status: str) -> str:
    return _MARKS.get(status, " ")


def todo_write(arguments: dict, context: ToolContext) -> str:
    raw = arguments.get("items")
    if not isinstance(raw, list):
        raise ValueError("`items` must be an array of {title, status} objects.")
    if len(raw) > MAX_ITEMS:
        raise ValueError(f"Keep the checklist to {MAX_ITEMS} items or fewer.")

    items: list[TodoItem] = []
    for entry in raw:
        if isinstance(entry, str):
            items.append(TodoItem(title=entry.strip()[:200]))
            continue
        if not isinstance(entry, dict):
            raise ValueError("Each checklist item must be an object or a string.")
        title = str(entry.get("title") or "").strip()
        if not title:
            raise ValueError("Each checklist item needs a title.")
        status = str(entry.get("status") or "pending")
        if status not in {"pending", "in_progress", "completed"}:
            status = "pending"
        items.append(TodoItem(title=title[:200], status=status))  # type: ignore[arg-type]

    sink = context.extras.get("todo_sink")
    if sink is None:
        raise ValueError("No checklist is available for this run.")
    sink(items)

    done = sum(1 for item in items if item.status == "completed")
    return f"Checklist updated: {done}/{len(items)} complete.\n" + "\n".join(
        f"[{_mark(item.status)}] {item.title}"
        for item in items
    )


TOOLS = [
    AgentTool(
        name="todo_write",
        description=(
            "Record or rewrite your working checklist. Send the complete list every time. "
            "Use it for objectives that span several steps or files so progress stays "
            "visible, and revise it as you learn. Skip it for simple objectives you can "
            "finish directly — do not invent a checklist for a one-step task."
        ),
        parameters={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["title"],
                    },
                }
            },
            "required": ["items"],
        },
        risk="read",
        handler=todo_write,
        summary=lambda a: f"Update checklist ({len(a.get('items') or [])} items)",
    )
]

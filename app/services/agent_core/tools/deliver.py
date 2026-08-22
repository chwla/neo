"""The delivery tool: move verified work into the user's real repository.

Classified ``delivery``, which the permission overlay never auto-approves --
not even in AUTO. Everything else the agent does is confined to Neo's managed
copy; this is the one action that leaves it, so it is always the user's call.
"""

from __future__ import annotations

from app.services.agent_core import delivery
from app.services.agent_core.tools.base import AgentTool, ToolContext


def deliver_changes(arguments: dict, context: ToolContext) -> str:
    if not context.repo_id:
        raise ValueError("This action needs a repository. Attach one to the session first.")
    plan = delivery.plan_delivery(context.repo_id)
    if not plan.deliverable:
        return delivery.summarize(plan)

    only = arguments.get("files")
    only = [str(item) for item in only] if isinstance(only, list) and only else None
    mode = str(arguments.get("mode") or "patch")

    if mode == "patch":
        patch = delivery.build_patch(plan, only)
        return f"{delivery.summarize(plan)}\n\nUnified diff:\n{patch}"
    if mode == "working_tree":
        if not plan.writable:
            # Say what to do instead, so the agent adapts rather than retrying
            # the same refused call until it burns its error budget.
            raise ValueError(
                "This repository was uploaded, so there is no folder on this machine "
                "to write into. Use mode='patch'; the user downloads the files from "
                "the run instead."
            )
        result = delivery.write_to_working_tree(plan, only)
        lines = [f"Wrote {len(result['written'])} file(s) into the repository working tree."]
        lines += [f"  {path}" for path in result["written"]]
        for item in result["skipped"] + result["blocked"]:
            lines.append(f"  skipped {item['path']} — {item['reason']}")
        lines.append("Nothing was staged, committed, or pushed.")
        return "\n".join(lines)
    raise ValueError("`mode` must be 'patch' or 'working_tree'.")


TOOLS = [
    AgentTool(
        name="deliver_changes",
        description=(
            "Deliver changes from Neo's managed copy into the user's real repository. "
            "mode='patch' returns a unified diff for the user to apply; "
            "mode='working_tree' writes the files directly, and is only available for a "
            "repository registered from a local path -- an uploaded folder has none, so "
            "the user downloads it instead. Files the user edited since import are "
            "always refused. Nothing is staged, committed, or pushed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["patch", "working_tree"]},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repository-relative paths; omit to deliver everything.",
                },
            },
        },
        risk="delivery",
        requires_repo=True,
        handler=deliver_changes,
        summary=lambda a: (
            "Write changes into the real repository"
            if a.get("mode") == "working_tree"
            else "Produce a patch of the changes"
        ),
    )
]

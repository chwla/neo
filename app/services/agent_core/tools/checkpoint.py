"""Local git checkpoints inside the managed copy.

This capability came from the Coding Workbench. It is preserved here rather than
reimplemented: the tool is a thin adapter over ``GitService``, which keeps the
argv allowlist and the ban on push/pull/remote in ``git/safety.py``.

A checkpoint is a commit in Neo's managed copy only. It never touches the user's
repository and never contacts a network remote.
"""

from __future__ import annotations

from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.git.service import GitService
from app.services.git.types import CheckpointCreateRequest, GitInitRequest


def create_checkpoint(arguments: dict, context: ToolContext) -> str:
    if not context.repo_id:
        raise ValueError("This action needs a repository. Attach one to the session first.")
    title = str(arguments.get("title") or "").strip()
    if not title:
        raise ValueError("A checkpoint title is required.")

    service = GitService()
    try:
        result = service.create_checkpoint(
            context.repo_id,
            CheckpointCreateRequest(
                title=title,
                message=str(arguments.get("message") or "") or None,
                task_id=context.task_id,
                confirm=True,
            ),
        )
    except ValueError as exc:
        # The managed copy is only git-initialised on demand, so a first
        # checkpoint has to create the repository before it can commit.
        if "initial" not in str(exc).lower() and "not initialized" not in str(exc).lower():
            raise
        service.initialize(context.repo_id, GitInitRequest(confirm=True))
        result = service.create_checkpoint(
            context.repo_id,
            CheckpointCreateRequest(title=title, task_id=context.task_id, confirm=True),
        )

    checkpoint = result.get("checkpoint", result) if isinstance(result, dict) else {}
    sha = str(checkpoint.get("commit_sha") or checkpoint.get("sha") or "")[:12]
    files = checkpoint.get("files") or []
    return (
        f"Checkpoint '{title}' created in the managed copy"
        f"{f' at {sha}' if sha else ''} ({len(files)} file(s)). "
        "This is local to Neo; nothing was pushed."
    )


TOOLS = [
    AgentTool(
        name="create_checkpoint",
        description=(
            "Commit the current state of the managed repository copy as a local "
            "checkpoint you can roll back to. Local only, never pushed, and the "
            "user's own repository is untouched."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short checkpoint title."},
                "message": {"type": "string", "description": "Optional longer message."},
            },
            "required": ["title"],
        },
        risk="workspace_write",
        requires_repo=True,
        handler=create_checkpoint,
        summary=lambda a: f"Checkpoint: {a.get('title')}",
    )
]

"""Everything that must be true before an external CLI is started.

Called from every entry point that can create an external run, so the answer is
the same wherever a run is asked for. The checks themselves are repeated inside
``service.run_step`` -- that is the real enforcement, and a CLI can be signed out
between the two moments -- but doing them here as well is what turns "the turn
sat in the transcript for a minute and then failed" into an immediate, actionable
error on the request.

The ordering rule is the one that matters: **nothing is written and no process is
started until all of this passes.** A repository check discovered after an agent
has begun editing is the one moment it is worthless.
"""

from __future__ import annotations

from app.services.agent_core.types import EXTERNAL_EXECUTORS
from app.services.external_agents.types import ExternalAgentError


def preflight(
    executor: str | None,
    *,
    handoff: str | None = None,
    repo_id: str | None = None,
) -> None:
    """Raise :class:`ExternalAgentError` if this run cannot legitimately start.

    A no-op for Neo's own loop, which has none of these constraints.
    """

    executor = executor or "neo"
    if executor not in EXTERNAL_EXECUTORS and not handoff:
        return

    from app.services import chat_prefs

    if not chat_prefs.external_agents_enabled():
        raise ExternalAgentError(
            "External engines are off for this profile. Turn them on from the engine "
            "picker in Agent mode."
        )

    from app.services.external_agents import chain as external_chain
    from app.services.external_agents import detect

    if handoff:
        usable, reason = external_chain.available(handoff)
        if not usable:
            raise ExternalAgentError(f"This handoff cannot run. {reason}")
    else:
        row = detect.status(executor)
        if not row.get("available"):
            # Never a quiet fallback to Neo's own loop: a turn the user gave to
            # Claude Code either runs on Claude Code or says why it did not.
            raise ExternalAgentError(
                f"{row.get('name', executor)} is unavailable: {row.get('reason')}"
            )

    # An external agent edits the folder directly and Neo reconstructs what it
    # changed from git. Without a repository there is nowhere to run; without a
    # commit there is no honest "before" for the diff or the undo.
    if not repo_id:
        raise ExternalAgentError(
            "Attach a folder to this chat first -- an external agent works in a repository."
        )

    from app.services.agent_core.workspace import repo_root
    from app.services.external_agents.snapshot import ensure_git_worktree

    ensure_git_worktree(repo_root(repo_id))


__all__ = ["preflight"]

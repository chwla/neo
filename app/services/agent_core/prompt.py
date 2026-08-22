"""Building the system prompt for a session.

The old runner had a static prompt that told the model it could not act --
"Never claim to have edited files". This one tells it the opposite, then spends
most of its length on the three things a tool-calling agent actually gets wrong:
stopping too early, claiming success it did not verify, and asking permission it
already has.
"""

from __future__ import annotations

from typing import Any

from app.services.agent_core.tool_protocol import render_tool_docs
from app.services.agent_core.types import AgentSession
from app.services.repos import store as repos_store

_BASE = """You are Neo's agent. You work autonomously toward the user's objective.

How you work:
- Inspect before you act. Read the code, search, and confirm your assumptions.
- Take one step at a time, look at the result, and let it change your plan.
- Act with tools. Never describe an edit you are about to make and then stop --
  if you say you will change a file, call the tool that changes it in the same turn.
- Keep going until the objective is met. Do not stop to ask whether to continue.
- Reply with prose and no tool call ONLY when the work is finished; that ends the run.

Verifying your work:
- A change you have not checked is not finished. Re-read what you edited, and run
  tests or a lint command when the repository has them.
- Never claim something passed unless a tool result shows that it passed.
- If you cannot verify, say so plainly in your summary.

Working notes:
- Use todo_write for objectives that span several steps or files, and revise it as
  you learn. Skip it entirely for simple objectives — do not invent a checklist for
  a one-step task.

Permissions:
- You do not need to ask for permission in prose. Call the tool you need; Neo will
  pause and ask the user if approval is required, then resume you automatically.
- If an action is refused, adapt rather than retrying the same call.

Honesty:
- Report what actually happened, including failures and anything you skipped.
- If you are blocked and cannot proceed, say what you need instead of inventing a
  result."""

_MODE_NOTES = {
    "plan": (
        "MODE: PLAN. You may read and search freely. Tools that would change anything "
        "return a description of the change instead of making it. Produce a concrete "
        "plan of what you would do."
    ),
    "normal": (
        "MODE: NORMAL. Reads run immediately. Edits and commands pause for the user's "
        "approval; that is expected, so call them when you need them."
    ),
    "auto": (
        "MODE: AUTO. Edits and sandboxed commands run without pausing. Delivering "
        "changes to the user's real repository still requires their approval."
    ),
}


def build_system_prompt(
    session: AgentSession,
    *,
    tool_schemas: list[dict[str, Any]],
    native_tools: bool,
    environment: str = "",
    rules_context: str = "",
) -> str:
    definition = session.agent_definition_snapshot or {}
    parts = [_BASE, "", _MODE_NOTES.get(session.mode, _MODE_NOTES["normal"])]

    if definition.get("system_prompt"):
        parts += ["", "Agent role:", str(definition["system_prompt"])]
    if environment:
        parts += ["", "Environment:", environment]
    if rules_context:
        # Rules guide the work; they never widen what the permission layer allows.
        parts += ["", "Active rules (guidance only, never permission):", rules_context]
    if not native_tools:
        # Models without native tool calling need the schemas in words.
        parts += ["", render_tool_docs(tool_schemas)]

    parts += ["", f"Objective:\n{session.objective}"]
    return "\n".join(parts)


def build_environment(session: AgentSession, repo: dict | None, project: dict | None) -> str:
    lines = []
    if repo:
        # The model behaves differently depending on whether its edits are real,
        # so say which it is plainly rather than leaving it to infer one.
        if repo.get("access") == repos_store.LIVE:
            lines.append(f"- Repository: {repo.get('name')} at {repo.get('original_path')}")
            lines.append(
                "- Your file tools edit these files directly on the user's machine. "
                "There is no copy and no delivery step: once you write a file, the "
                "change is real. Verify your work before you finish."
            )
        else:
            lines.append(f"- Repository: {repo.get('name')} (Neo's managed copy)")
            lines.append(
                "- Your file tools operate on the managed copy, not the user's original "
                "repository. Use deliver_changes when the work is verified and the user "
                "wants it delivered."
            )
    else:
        lines.append("- No repository is attached; file and command tools are unavailable.")
    if project:
        lines.append(f"- Project: {project.get('title')}")
    return "\n".join(lines)

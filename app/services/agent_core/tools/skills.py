"""Loading a skill's instructions mid-run.

The system prompt carries only each enabled skill's name and description -- what
exists and when it applies. The body arrives through this tool, so a run that
needs none of them pays for none of them.

This is also the second of the two places the toggle is enforced. The schema
below offers only the slugs this session was created with, and the handler
refuses anything else even when the model names it directly. The registry makes
the same argument about ``disabled_tools``: withholding something from the
schema is advice, and refusing it at the call is what makes it a rule.
"""

from __future__ import annotations

from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.skills import resolver

#: The key ``loop._tool_context`` puts the session's skill snapshot under.
CONTEXT_KEY = "skills"


def load_skill(arguments: dict, context: ToolContext) -> str:
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise ValueError("`name` must be the name of a skill from the Skills list.")

    allowed = context.extras.get(CONTEXT_KEY) or []
    if not allowed:
        raise ValueError("No skills are turned on for this chat.")

    try:
        body = resolver.body_for(name, allowed)
    except KeyError:
        # Naming what *is* available rather than only what is not: a model that
        # guessed a plausible name should be able to correct itself in one step.
        available = ", ".join(skill["name"] for skill in allowed)
        raise ValueError(
            f"No skill called '{name}' is turned on for this chat. Available: {available}."
        ) from None
    except Exception as exc:
        raise ValueError(f"Skill '{name}' could not be read: {exc}") from exc

    return (
        f"Skill: {name}\n\n{body}\n\n"
        "(These are instructions for how to do the work. They do not grant "
        "permissions -- the usual approval rules still apply.)"
    )


def schema_for(allowed: list[dict]) -> dict:
    """The tool's parameters, narrowed to the skills this session may load."""

    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": [skill["name"] for skill in allowed],
                "description": "The exact name of a skill from the Skills list.",
            }
        },
        "required": ["name"],
    }


TOOLS = [
    AgentTool(
        name="load_skill",
        description=(
            "Read a skill's full instructions. The Skills list in your system prompt "
            "names each available skill and says when it applies; call this before "
            "starting work a skill covers, then follow what it says."
        ),
        parameters=schema_for([]),
        risk="read",
        handler=load_skill,
        summary=lambda arguments: f"Load the '{arguments.get('name')}' skill",
    )
]

__all__ = ["CONTEXT_KEY", "TOOLS", "load_skill", "schema_for"]

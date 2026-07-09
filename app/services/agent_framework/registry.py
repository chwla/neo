from __future__ import annotations

from app.services.agent_framework.prompts import BUILTIN_PROMPTS
from app.services.agent_framework.types import AgentPermissions


def built_in_definitions() -> list[dict]:
    base = {
        "default_route_name": None,
        "rules_profile_ids": [],
        "tools": [],
        "skills": [],
        "enabled": True,
        "built_in": True,
        "metadata": {"source": "built-in"},
    }
    roles = [
        (
            "general",
            "General",
            "General safe coordination agent.",
            AgentPermissions(can_delegate=True, max_delegations=2),
            10,
        ),
        (
            "planner",
            "Planner",
            "Creates plans and bounded context; read-only.",
            AgentPermissions(can_propose_patch=False),
            20,
        ),
        (
            "coder",
            "Coder",
            "Creates patch proposals; cannot apply without approval.",
            AgentPermissions(can_propose_patch=True, can_request_patch_apply=True),
            30,
        ),
        (
            "reviewer",
            "Reviewer",
            "Reviews diffs and risk; no mutation.",
            AgentPermissions(can_plan=True, can_read_files=True),
            40,
        ),
        (
            "tester",
            "Tester",
            "Suggests and analyzes saved tests; execution approval-gated.",
            AgentPermissions(can_request_tests=True),
            50,
        ),
        (
            "researcher",
            "Researcher",
            "Research and source synthesis only.",
            AgentPermissions(can_research=True),
            60,
        ),
        (
            "refactor",
            "Refactor",
            "Refactoring patch proposal role.",
            AgentPermissions(can_propose_patch=True, can_request_patch_apply=True),
            70,
        ),
        (
            "explorer",
            "Explorer",
            "Codebase exploration and context selection.",
            AgentPermissions(can_read_files=True, can_select_context=True),
            80,
        ),
        (
            "summarizer",
            "Summarizer",
            "Summaries, titles, and final reports.",
            AgentPermissions(can_read_files=True),
            90,
        ),
    ]
    return [
        {
            **base,
            "id": f"builtin-{name}",
            "name": name,
            "display_name": display,
            "description": description,
            "agent_type": name,
            "system_prompt": BUILTIN_PROMPTS[name],
            "permissions": permissions,
            "priority": priority,
        }
        for name, display, description, permissions, priority in roles
    ]

from __future__ import annotations

from app.services.tools import store


def built_in_server() -> dict:
    now = store.now_iso()
    return {
        "id": "builtin-neo",
        "name": "Neo Built-in Safe Tools",
        "server_type": "builtin",
        "command_json": None,
        "url": None,
        "env_json": {},
        "enabled": True,
        "approval_required": False,
        "metadata": {"source": "built-in", "secrets": "environment references only"},
        "created_at": now,
        "updated_at": now,
    }


def built_in_tools() -> list[dict]:
    now = store.now_iso()
    base = {
        "server_id": "builtin-neo",
        "enabled": True,
        "built_in": True,
        "created_at": now,
        "updated_at": now,
    }
    return [
        {
            **base,
            "id": "builtin.repo_metadata",
            "name": "repo_metadata",
            "display_name": "Inspect repo metadata",
            "description": "Read-only workspace metadata summary. Does not run shell commands.",
            "category": "read_only",
            "input_schema": {
                "type": "object",
                "properties": {"repo_id": {"type": "string"}},
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "permissions": {"agent_permission": "can_read_files"},
            "metadata": {"executor": "builtin.repo_metadata"},
        },
        {
            **base,
            "id": "builtin.summarize_text",
            "name": "summarize_text",
            "display_name": "Summarize bounded text",
            "description": "Read-only bounded text summary helper.",
            "category": "read_only",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 12000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "permissions": {"agent_permission": "can_read_files"},
            "metadata": {"executor": "builtin.summarize_text"},
        },
        {
            **base,
            "id": "builtin.create_note",
            "name": "create_note",
            "display_name": "Create note artifact",
            "description": "Workspace write placeholder that always requires approval.",
            "category": "workspace_write_approval_required",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 160},
                    "body": {"type": "string", "maxLength": 12000},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
            "permissions": {"requires_approval": True},
            "metadata": {"executor": "builtin.create_note"},
        },
        {
            **base,
            "id": "disabled.shell",
            "name": "shell",
            "display_name": "Shell",
            "description": "Dangerous shell execution is intentionally unavailable.",
            "category": "dangerous_disabled",
            "input_schema": {},
            "output_schema": {},
            "permissions": {"disabled_reason": "arbitrary shell is forbidden"},
            "enabled": False,
            "metadata": {"executor": "disabled"},
        },
    ]


def built_in_skills() -> list[dict]:
    now = store.now_iso()
    base = {
        "skill_type": "instruction_bundle",
        "enabled": True,
        "built_in": True,
        "metadata": {"source": "built-in"},
        "created_at": now,
        "updated_at": now,
    }
    return [
        {
            **base,
            "id": "skill.code_review",
            "name": "code_review",
            "display_name": "Code review",
            "description": "Review code for correctness, maintainability, and safety.",
            "instructions": "Review code changes. Do not apply patches or bypass approvals.",
            "tool_ids": ["builtin.repo_metadata", "builtin.summarize_text"],
            "agent_ids": ["reviewer", "coder"],
            "rules_profile_ids": [],
        },
        {
            **base,
            "id": "skill.test_failure_analysis",
            "name": "test_failure_analysis",
            "display_name": "Test failure analysis",
            "description": "Analyze test output and propose safe next steps.",
            "instructions": (
                "Analyze failures from provided logs. Suggest tests but do not run them."
            ),
            "tool_ids": ["builtin.summarize_text"],
            "agent_ids": ["tester", "coder"],
            "rules_profile_ids": [],
        },
        {
            **base,
            "id": "skill.repo_exploration",
            "name": "repo_exploration",
            "display_name": "Repo exploration",
            "description": "Explore repository structure without mutation.",
            "instructions": "Inspect repo metadata and summarize likely relevant areas.",
            "tool_ids": ["builtin.repo_metadata", "builtin.summarize_text"],
            "agent_ids": ["explorer", "planner"],
            "rules_profile_ids": [],
        },
        {
            **base,
            "id": "skill.research_brief",
            "name": "research_brief",
            "display_name": "Research brief",
            "description": "Create concise research briefs from bounded inputs.",
            "instructions": "Summarize research inputs with source-aware caveats.",
            "tool_ids": ["builtin.summarize_text"],
            "agent_ids": ["researcher", "summarizer"],
            "rules_profile_ids": [],
        },
        {
            **base,
            "id": "skill.patch_risk_check",
            "name": "patch_risk_check",
            "display_name": "Patch risk check",
            "description": "Check patch risk without applying changes.",
            "instructions": (
                "Identify patch risks. Patch application remains explicit approval only."
            ),
            "tool_ids": ["builtin.repo_metadata", "builtin.summarize_text"],
            "agent_ids": ["reviewer", "coder"],
            "rules_profile_ids": [],
        },
    ]

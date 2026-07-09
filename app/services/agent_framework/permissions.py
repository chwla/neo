from __future__ import annotations

import fnmatch

from app.services.agent_framework.types import AgentPermissions
from app.services.rules.safety import HARD_FORBIDDEN

SAFE_TOOL_NAMES = {
    "read_files",
    "select_context",
    "propose_patch",
    "request_patch_apply",
    "request_tests",
    "request_checkpoint",
    "research",
    "summarize",
    "delegate",
    "builtin.repo_metadata",
    "builtin.summarize_text",
    "builtin.create_note",
}
DANGEROUS_TOOL_NAMES = {
    "shell",
    "terminal",
    "git",
    "git_remote",
    "package_install",
    "npm_install",
    "pip_install",
}

ROLE_LIMITS: dict[str, dict[str, bool | int]] = {
    "general": {"can_delegate": True, "max_delegations": 2},
    "planner": {"can_propose_patch": False, "can_request_patch_apply": False},
    "coder": {"can_propose_patch": True, "can_request_patch_apply": True},
    "reviewer": {
        "can_propose_patch": False,
        "can_request_patch_apply": False,
        "can_request_tests": False,
        "can_request_checkpoint": False,
    },
    "tester": {
        "can_propose_patch": False,
        "can_request_patch_apply": False,
        "can_request_tests": True,
        "can_request_checkpoint": False,
    },
    "researcher": {
        "can_propose_patch": False,
        "can_request_patch_apply": False,
        "can_research": True,
    },
    "refactor": {"can_propose_patch": True, "can_request_patch_apply": True},
    "explorer": {
        "can_propose_patch": False,
        "can_request_patch_apply": False,
        "can_request_tests": False,
    },
    "summarizer": {
        "can_propose_patch": False,
        "can_request_patch_apply": False,
        "can_request_tests": False,
        "can_request_checkpoint": False,
    },
}


def clamp_permissions(
    permissions: AgentPermissions | dict,
    *,
    agent_type: str = "custom",
    built_in: bool = False,
) -> tuple[AgentPermissions, list[str]]:
    if not isinstance(permissions, AgentPermissions):
        permissions = AgentPermissions(**(permissions or {}))
    warnings: list[str] = []
    values = permissions.model_dump()
    limits = ROLE_LIMITS.get(agent_type, {})
    for key, limit in limits.items():
        if isinstance(limit, bool) and limit is False and values.get(key):
            warnings.append(f"Unsafe permission ignored for {agent_type}: {key}.")
            values[key] = False
        elif isinstance(limit, bool) and limit is True:
            values[key] = bool(values.get(key))
        elif isinstance(limit, int) and int(values.get(key, 0)) > limit:
            warnings.append(f"Delegation limit clamped for {agent_type}: {key} <= {limit}.")
            values[key] = limit
    if values.get("max_delegations", 0) > 5:
        warnings.append("Delegation limit clamped: max_delegations cannot exceed 5.")
        values["max_delegations"] = 5
    if not values.get("can_delegate") and values.get("max_delegations"):
        warnings.append("max_delegations ignored because can_delegate is false.")
        values["max_delegations"] = 0
    if values.get("can_request_patch_apply"):
        warnings.append("Patch apply remains approval-gated; agents can only request approval.")
    if values.get("can_request_tests"):
        warnings.append("Tests remain approval-gated and limited to saved commands.")
    if values.get("can_request_checkpoint"):
        warnings.append("Checkpoints remain approval-gated and local-only.")
    forbidden = list(values.get("forbidden_file_patterns") or [])
    for path in HARD_FORBIDDEN:
        if path not in forbidden:
            forbidden.append(path)
    values["forbidden_file_patterns"] = forbidden
    return AgentPermissions(**values), warnings


def clamp_tools(tools: list[str] | None) -> tuple[list[str], list[str]]:
    clean, warnings = [], []
    for tool in tools or []:
        normalized = str(tool).strip()
        if normalized in DANGEROUS_TOOL_NAMES:
            warnings.append(f"Tool '{tool}' is dangerous and was ignored.")
        elif (normalized in SAFE_TOOL_NAMES or "." in normalized) and normalized not in clean:
            clean.append(normalized)
        elif normalized and normalized not in clean:
            clean.append(tool)
        else:
            warnings.append(f"Tool '{tool}' is not available to agents and was ignored.")
    return clean, warnings


def can_access_path(permissions: AgentPermissions, relative_path: str) -> bool:
    if any(
        fnmatch.fnmatch(relative_path, pattern)
        for pattern in permissions.forbidden_file_patterns
    ):
        return False
    allowed = permissions.allowed_file_patterns
    return not allowed or any(fnmatch.fnmatch(relative_path, pattern) for pattern in allowed)

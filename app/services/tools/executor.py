from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.agent_framework.service import AgentDefinitionService
from app.services.tools import store
from app.services.tools.mcp import execute_mcp_read_only
from app.services.tools.permissions import (
    ToolPermissionError,
    approval_required,
    ensure_tool_allowed,
)
from app.services.tools.registry import built_in_server, built_in_skills, built_in_tools
from app.services.tools.types import (
    SkillDefinition,
    SkillDefinitionCreate,
    SkillDefinitionUpdate,
    ToolCall,
    ToolCallCreate,
    ToolDefinition,
    ToolDefinitionCreate,
    ToolDefinitionUpdate,
    ToolServer,
    ToolServerCreate,
    ToolServerUpdate,
)


class ToolValidationError(ValueError):
    pass


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.lower()).strip("-")


class ToolsService:
    def __init__(self) -> None:
        store.initialize_tool_tables()
        self.seed_builtins()

    def seed_builtins(self) -> None:
        store.upsert_server(built_in_server())
        for item in built_in_tools():
            store.upsert_tool(item)
        for item in built_in_skills():
            store.upsert_skill(item)

    def list_servers(self, *, include_disabled: bool = True) -> list[ToolServer]:
        return [
            ToolServer(**item)
            for item in store.list_servers(include_disabled=include_disabled)
        ]

    def create_server(self, payload: ToolServerCreate) -> ToolServer:
        now = store.now_iso()
        data = payload.model_dump()
        data["id"] = data.get("id") or f"server.{_slug(data['name']) or uuid.uuid4()}"
        return ToolServer(**store.insert_server({**data, "created_at": now, "updated_at": now}))

    def update_server(self, server_id: str, payload: ToolServerUpdate) -> ToolServer:
        if not store.get_server(server_id):
            raise ToolValidationError("Tool server not found.")
        item = store.update_server(server_id, payload.model_dump(exclude_unset=True))
        return ToolServer(**item)

    def disable_server(self, server_id: str) -> ToolServer:
        if not store.get_server(server_id):
            raise ToolValidationError("Tool server not found.")
        return ToolServer(**store.update_server(server_id, {"enabled": False}))

    def list_tools(
        self, *, include_disabled: bool = True, server_id: str | None = None
    ) -> list[ToolDefinition]:
        return [
            ToolDefinition(**item)
            for item in store.list_tools(include_disabled=include_disabled, server_id=server_id)
        ]

    def create_tool(self, payload: ToolDefinitionCreate) -> ToolDefinition:
        data = payload.model_dump()
        data["id"] = data.get("id") or f"tool.{_slug(data['name']) or uuid.uuid4()}"
        self._validate_server(data.get("server_id"))
        now = store.now_iso()
        return ToolDefinition(**store.insert_tool({**data, "created_at": now, "updated_at": now}))

    def update_tool(self, tool_id: str, payload: ToolDefinitionUpdate) -> ToolDefinition:
        current = store.get_tool(tool_id)
        if not current:
            raise ToolValidationError("Tool definition not found.")
        updates = payload.model_dump(exclude_unset=True)
        self._validate_server(updates.get("server_id"))
        if current.get("built_in"):
            updates = {
                key: value
                for key, value in updates.items()
                if key in {"enabled", "metadata"}
            }
        return ToolDefinition(**store.update_tool(tool_id, updates))

    def disable_tool(self, tool_id: str) -> ToolDefinition:
        if not store.get_tool(tool_id):
            raise ToolValidationError("Tool definition not found.")
        return ToolDefinition(**store.update_tool(tool_id, {"enabled": False}))

    def list_skills(self, *, include_disabled: bool = True) -> list[SkillDefinition]:
        return [
            SkillDefinition(**item)
            for item in store.list_skills(include_disabled=include_disabled)
        ]

    def create_skill(self, payload: SkillDefinitionCreate) -> SkillDefinition:
        data = payload.model_dump()
        data["id"] = data.get("id") or f"skill.{_slug(data['name']) or uuid.uuid4()}"
        self._validate_tool_ids(data.get("tool_ids", []))
        now = store.now_iso()
        return SkillDefinition(**store.insert_skill({**data, "created_at": now, "updated_at": now}))

    def update_skill(self, skill_id: str, payload: SkillDefinitionUpdate) -> SkillDefinition:
        current = store.get_skill(skill_id)
        if not current:
            raise ToolValidationError("Skill definition not found.")
        updates = payload.model_dump(exclude_unset=True)
        if "tool_ids" in updates:
            self._validate_tool_ids(updates["tool_ids"])
        if current.get("built_in"):
            updates = {
                key: value
                for key, value in updates.items()
                if key in {"enabled", "metadata"}
            }
        return SkillDefinition(**store.update_skill(skill_id, updates))

    def disable_skill(self, skill_id: str) -> SkillDefinition:
        if not store.get_skill(skill_id):
            raise ToolValidationError("Skill definition not found.")
        return SkillDefinition(**store.update_skill(skill_id, {"enabled": False}))

    def request_call(self, payload: ToolCallCreate) -> ToolCall:
        tool, server, agent, skill = self._resolve_call_context(payload)
        call = self._create_initial_call(payload, tool, server, agent, skill)
        if call["status"] == "completed" and call["approval_status"] == "not_required":
            call = self._execute(call["id"])
        return ToolCall(**call)

    def approve_call(self, call_id: str) -> ToolCall:
        call = store.get_call(call_id)
        if not call:
            raise ToolValidationError("Tool call not found.")
        if call["approval_status"] != "pending":
            raise ToolValidationError("Tool call is not awaiting approval.")
        store.update_call(call_id, {"approval_status": "approved"})
        return ToolCall(**self._execute(call_id))

    def reject_call(self, call_id: str, reason: str | None = None) -> ToolCall:
        call = store.get_call(call_id)
        if not call:
            raise ToolValidationError("Tool call not found.")
        updates = {
            "status": "rejected",
            "approval_status": "rejected",
            "error": reason or "Rejected by user.",
            "completed_at": store.now_iso(),
        }
        return ToolCall(**store.update_call(call_id, updates))

    def list_calls(
        self,
        *,
        run_id: str | None = None,
        coding_run_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ToolCall], int]:
        calls, total = store.list_calls(
            run_id=run_id, coding_run_id=coding_run_id, status=status, limit=limit, offset=offset
        )
        return [ToolCall(**item) for item in calls], total

    def get_call(self, call_id: str) -> ToolCall | None:
        item = store.get_call(call_id)
        return ToolCall(**item) if item else None

    def _create_initial_call(
        self,
        payload: ToolCallCreate,
        tool: dict,
        server: dict | None,
        agent: dict | None,
        skill: dict | None,
    ) -> dict:
        now = store.now_iso()
        try:
            ensure_tool_allowed(
                tool=tool,
                server=server,
                agent=agent,
                skill=skill,
                input_payload=payload.input,
            )
            validate_input_schema(tool.get("input_schema", {}), payload.input)
        except (ToolPermissionError, ToolValidationError, ValueError) as exc:
            return store.insert_call(
                {
                    "id": str(uuid.uuid4()),
                    **payload.model_dump(),
                    "status": "blocked",
                    "approval_status": "not_required",
                    "output": None,
                    "error": str(exc),
                    "latency_ms": None,
                    "created_at": now,
                    "completed_at": now,
                }
            )
        requires_approval = approval_required(tool, server)
        return store.insert_call(
            {
                "id": str(uuid.uuid4()),
                **payload.model_dump(),
                "status": "pending_approval" if requires_approval else "completed",
                "approval_status": "pending" if requires_approval else "not_required",
                "output": None,
                "error": None,
                "latency_ms": None,
                "created_at": now,
                "completed_at": None,
            }
        )

    def _execute(self, call_id: str) -> dict:
        call = store.get_call(call_id)
        tool = store.get_tool(call["tool_id"])
        server = store.get_server(tool["server_id"]) if tool.get("server_id") else None
        start = time.monotonic()
        try:
            output = execute_tool(tool, server, call["input"])
            return store.update_call(
                call_id,
                {
                    "status": "completed",
                    "output": output,
                    "error": None,
                    "latency_ms": int((time.monotonic() - start) * 1000),
                    "completed_at": store.now_iso(),
                },
            )
        except Exception as exc:
            return store.update_call(
                call_id,
                {
                    "status": "failed",
                    "output": None,
                    "error": str(exc),
                    "latency_ms": int((time.monotonic() - start) * 1000),
                    "completed_at": store.now_iso(),
                },
            )

    def _resolve_call_context(
        self, payload: ToolCallCreate
    ) -> tuple[dict, dict | None, dict | None, dict | None]:
        tool = store.get_tool(payload.tool_id)
        if not tool:
            raise ToolValidationError("Tool definition not found.")
        server = store.get_server(tool["server_id"]) if tool.get("server_id") else None
        agent = None
        if payload.agent_definition_id:
            resolved = AgentDefinitionService().get(payload.agent_definition_id)
            if not resolved:
                raise ToolValidationError("Agent definition not found.")
            agent = resolved.model_dump()
        skill = None
        if payload.skill_id:
            skill = store.get_skill(payload.skill_id)
            if not skill or not skill.get("enabled"):
                raise ToolValidationError("Skill definition not found or disabled.")
        return tool, server, agent, skill

    @staticmethod
    def _validate_server(server_id: str | None) -> None:
        if server_id and not store.get_server(server_id):
            raise ToolValidationError("Tool server not found.")

    @staticmethod
    def _validate_tool_ids(tool_ids: list[str]) -> None:
        for tool_id in tool_ids:
            if not store.get_tool(tool_id):
                raise ToolValidationError(f"Tool '{tool_id}' not found.")


def validate_input_schema(schema: dict[str, Any], payload: dict[str, Any]) -> None:
    if not schema:
        return
    if schema.get("type") == "object" and not isinstance(payload, dict):
        raise ToolValidationError("Tool input must be an object.")
    required = schema.get("required") or []
    missing = [key for key in required if key not in payload]
    if missing:
        raise ToolValidationError(f"Missing required input: {', '.join(missing)}.")
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        extra = [key for key in payload if key not in properties]
        if extra:
            raise ToolValidationError(f"Unexpected input fields: {', '.join(extra)}.")
    for key, spec in properties.items():
        if key not in payload:
            continue
        value = payload[key]
        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolValidationError(f"Input '{key}' must be a string.")
        if expected == "integer" and not isinstance(value, int):
            raise ToolValidationError(f"Input '{key}' must be an integer.")
        if isinstance(value, str) and spec.get("maxLength") and len(value) > spec["maxLength"]:
            raise ToolValidationError(f"Input '{key}' exceeds max length.")
        if isinstance(value, int):
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolValidationError(f"Input '{key}' is below minimum.")
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolValidationError(f"Input '{key}' exceeds maximum.")


def execute_tool(tool: dict, server: dict | None, payload: dict[str, Any]) -> dict[str, Any]:
    if tool["category"] == "dangerous_disabled":
        raise ToolValidationError("Dangerous tools are disabled.")
    executor = (tool.get("metadata") or {}).get("executor")
    if executor == "builtin.repo_metadata":
        return _repo_metadata(payload)
    if executor == "builtin.summarize_text":
        return _summarize_text(payload)
    if executor == "builtin.create_note":
        return {
            "created": False,
            "message": "Approved workspace write recorded; no automatic write performed.",
        }
    if server and server["server_type"] in {"stdio", "http"}:
        if tool["category"] not in {"read_only", "external_read"}:
            raise ToolValidationError("MCP write tools are approval-gated but not executable yet.")
        return execute_mcp_read_only(server, tool, payload)
    raise ToolValidationError("No safe executor is available for this tool.")


def _repo_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(get_settings().workspace_repos_dir)
    repo_id = payload.get("repo_id")
    return {
        "workspace_repos_dir": str(root),
        "repo_id": repo_id,
        "exists": root.exists(),
        "note": "Read-only metadata only; no shell command was run.",
    }


def _summarize_text(payload: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(payload.get("text", "")).split())
    limit = int(payload.get("limit") or 5)
    words = text.split(" ")
    summary = " ".join(words[: max(1, min(limit * 20, 200))])
    if len(words) > len(summary.split(" ")):
        summary = f"{summary}…"
    return {"summary": summary, "input_words": len(words)}

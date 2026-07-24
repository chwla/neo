from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.agent_framework.service import AgentDefinitionService
from app.services.tools import store
from app.services.tools.mcp import execute_mcp_tool
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
            ToolServer(**item) for item in store.list_servers(include_disabled=include_disabled)
        ]

    def create_server(self, payload: ToolServerCreate) -> ToolServer:
        now = store.now_iso()
        data = payload.model_dump()
        self._validate_server_config(data)
        data["id"] = data.get("id") or f"server.{_slug(data['name']) or uuid.uuid4()}"
        if store.get_server(data["id"]):
            if payload.id:
                raise ToolValidationError("Tool server ID already exists.")
            data["id"] = f"{data['id']}.{uuid.uuid4().hex[:12]}"
        return ToolServer(**store.insert_server({**data, "created_at": now, "updated_at": now}))

    def update_server(self, server_id: str, payload: ToolServerUpdate) -> ToolServer:
        if not store.get_server(server_id):
            raise ToolValidationError("Tool server not found.")
        updates = payload.model_dump(exclude_unset=True)
        candidate = {**store.get_server(server_id), **updates}
        self._validate_server_config(candidate)
        item = store.update_server(server_id, updates)
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
        _reject_plaintext_secrets(data.get("metadata") or {}, "metadata")
        _reject_plaintext_secrets(data.get("permissions") or {}, "permissions")
        data["id"] = data.get("id") or f"tool.{_slug(data['name']) or uuid.uuid4()}"
        if store.get_tool(data["id"]):
            if payload.id:
                raise ToolValidationError("Tool definition ID already exists.")
            data["id"] = f"{data['id']}.{uuid.uuid4().hex[:12]}"
        self._validate_server(data.get("server_id"))
        now = store.now_iso()
        return ToolDefinition(**store.insert_tool({**data, "created_at": now, "updated_at": now}))

    def update_tool(self, tool_id: str, payload: ToolDefinitionUpdate) -> ToolDefinition:
        current = store.get_tool(tool_id)
        if not current:
            raise ToolValidationError("Tool definition not found.")
        updates = payload.model_dump(exclude_unset=True)
        _reject_plaintext_secrets(updates.get("metadata") or {}, "metadata")
        _reject_plaintext_secrets(updates.get("permissions") or {}, "permissions")
        self._validate_server(updates.get("server_id"))
        if current.get("built_in"):
            updates = {
                key: value for key, value in updates.items() if key in {"enabled", "metadata"}
            }
        return ToolDefinition(**store.update_tool(tool_id, updates))

    def disable_tool(self, tool_id: str) -> ToolDefinition:
        if not store.get_tool(tool_id):
            raise ToolValidationError("Tool definition not found.")
        return ToolDefinition(**store.update_tool(tool_id, {"enabled": False}))

    def list_skills(self, *, include_disabled: bool = True) -> list[SkillDefinition]:
        return [
            SkillDefinition(**item) for item in store.list_skills(include_disabled=include_disabled)
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
                key: value for key, value in updates.items() if key in {"enabled", "metadata"}
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

    def list_enabled_read_tools(
        self,
        capability: str | None = None,
        *,
        intent: str | None = None,
    ) -> list[ToolDefinition]:
        """Return enabled connector reads ordered by deterministic capability score."""

        candidates: list[tuple[int, dict]] = []
        wanted = _capability_tokens(f"{capability or ''} {intent or ''}")
        for item in store.list_tools(include_disabled=False):
            if item["category"] not in {"read_only", "workspace_read", "external_read"}:
                continue
            server = store.get_server(item["server_id"]) if item.get("server_id") else None
            if not server or server["server_type"] == "builtin" or not server.get("enabled"):
                continue
            score = _tool_capability_score(item, wanted)
            if not wanted or score > 0:
                candidates.append((score, item))
        candidates.sort(key=lambda pair: (-pair[0], pair[1]["name"], pair[1]["id"]))
        return [ToolDefinition(**item) for _, item in candidates]

    def select_enabled_read_tool(
        self,
        capability: str,
        *,
        intent: str | None = None,
    ) -> ToolDefinition | None:
        """Select only a unique high-confidence read tool.

        Ambiguous or weak similarity deliberately returns ``None`` so chat can
        ask for confirmation or fall back instead of calling an arbitrary tool.
        """

        ranked = self.list_enabled_read_tools(capability, intent=intent)
        if not ranked:
            return None
        wanted = _capability_tokens(f"{capability} {intent or ''}")
        best_score = _tool_capability_score(ranked[0].model_dump(), wanted)
        if best_score < 6:
            return None
        if len(ranked) > 1:
            next_score = _tool_capability_score(ranked[1].model_dump(), wanted)
            if next_score == best_score:
                return None
        return ranked[0]

    def invoke_connector(
        self,
        *,
        arguments: dict[str, Any],
        capability: str | None = None,
        intent: str | None = None,
        tool_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Stable chat-facing connector invocation primitive.

        Capability selection is read-only. Explicit write tool IDs enter the
        existing approval queue and never execute in this call.
        """

        if tool_id:
            tool = store.get_tool(tool_id)
            if not tool:
                raise ToolValidationError("Tool definition not found.")
        else:
            if not capability:
                raise ToolValidationError("A capability or explicit tool_id is required.")
            selected = self.select_enabled_read_tool(capability, intent=intent)
            if selected is None:
                return {
                    "status": "not_selected",
                    "reason": "No unique high-confidence enabled read connector matched.",
                    "capability": capability,
                }
            tool = selected.model_dump()
        call = self.request_call(ToolCallCreate(tool_id=tool["id"], input=arguments, run_id=run_id))
        result = {
            "status": call.status,
            "call_id": call.id,
            "approval_status": call.approval_status,
            "approval_required": call.approval_status == "pending",
            "tool": {
                "id": tool["id"],
                "name": tool["name"],
                "display_name": tool.get("display_name"),
                "category": tool["category"],
            },
            "result": call.output.get("result") if call.output else None,
            "provenance": call.output.get("provenance") if call.output else None,
            "error": call.error,
        }
        return result

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
            output = _redact_sensitive(execute_tool(tool, server, call["input"]))
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

    @staticmethod
    def _validate_server_config(server: dict) -> None:
        metadata = server.get("metadata") or {}
        if server.get("server_type") == "http":
            if not server.get("url"):
                raise ToolValidationError("HTTP tool servers require a URL.")
            from app.services.tools.security import validate_connector_url

            validate_connector_url(
                server["url"],
                allow_trusted_localhost=bool(metadata.get("trusted_localhost")),
                resolve=False,
            )
        elif server.get("server_type") == "stdio":
            if metadata.get("trusted_stdio") is not True:
                raise ToolValidationError("Stdio servers require metadata.trusted_stdio=true.")
            if not server.get("command_json"):
                raise ToolValidationError("Stdio tool servers require command_json.")
        _reject_plaintext_secrets(metadata)
        for target, source in (server.get("env_json") or {}).items():
            if not _valid_env_name(str(target)) or not _valid_env_name(str(source)):
                raise ToolValidationError(
                    "Tool server env_json must map environment names to environment references."
                )


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
        if key in payload:
            _validate_schema_value(spec, payload[key], f"Input '{key}'", depth=0)


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
    if server and executor == "mcp":
        return execute_mcp_tool(server, tool, payload)
    if server and executor == "rest":
        from app.services.tools.rest import execute_rest_tool

        return execute_rest_tool(server, tool, payload)
    if server and server["server_type"] in {"stdio", "http"}:
        # Backwards-compatible manually-created MCP definitions.
        return execute_mcp_tool(server, tool, payload)
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


def _validate_schema_value(schema: dict, value: Any, label: str, *, depth: int) -> None:
    if depth > 12:
        raise ToolValidationError("Tool input schema nesting exceeds the limit.")
    if value is None and schema.get("nullable") is True:
        return
    expected = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        None: True,
    }.get(expected, True)
    if not valid:
        raise ToolValidationError(f"{label} must be {expected}.")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError(f"{label} is not an allowed value.")
    if isinstance(value, str):
        if schema.get("maxLength") is not None and len(value) > int(schema["maxLength"]):
            raise ToolValidationError(f"{label} exceeds max length.")
        if schema.get("minLength") is not None and len(value) < int(schema["minLength"]):
            raise ToolValidationError(f"{label} is shorter than the minimum length.")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolValidationError(f"{label} is below minimum.")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolValidationError(f"{label} exceeds maximum.")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ToolValidationError(f"{label} contains too many items.")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item_schema, item, f"{label}[{index}]", depth=depth + 1)
    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            raise ToolValidationError(f"{label} is missing: {', '.join(missing)}.")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = [key for key in value if key not in properties]
            if extra:
                raise ToolValidationError(f"{label} has unexpected fields: {', '.join(extra)}.")
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                _validate_schema_value(
                    child,
                    value[key],
                    f"{label}.{key}",
                    depth=depth + 1,
                )


def _capability_tokens(value: str) -> set[str]:
    ignored = {
        "a",
        "an",
        "and",
        "for",
        "from",
        "get",
        "please",
        "show",
        "the",
        "to",
        "tool",
        "use",
        "with",
    }
    tokens = {
        "".join(character for character in item if character.isalnum())
        for item in value.lower().replace("_", " ").replace("-", " ").split()
    }
    return {item for item in tokens if len(item) >= 2 and item not in ignored}


def _tool_capability_score(tool: dict, wanted: set[str]) -> int:
    if not wanted:
        return 0
    metadata = tool.get("metadata") or {}
    name_tokens = _capability_tokens(f"{tool.get('name', '')} {tool.get('display_name', '')}")
    capability_tokens = _capability_tokens(
        " ".join(str(item) for item in metadata.get("capabilities") or [])
    )
    description_tokens = _capability_tokens(str(tool.get("description") or ""))
    normalized_name = "".join(sorted(name_tokens))
    normalized_wanted = "".join(sorted(wanted))
    exact_name = normalized_name == normalized_wanted and bool(normalized_name)
    return (
        (12 if exact_name else 0)
        + 6 * len(wanted & name_tokens)
        + 4 * len(wanted & capability_tokens)
        + len(wanted & description_tokens)
    )


def _reject_plaintext_secrets(value: Any, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "api_key",
                "apikey",
                "access_token",
                "refresh_token",
                "bearer_token",
                "client_secret",
                "password",
                "secret",
            } and item not in (None, "", False):
                raise ToolValidationError(
                    f"Plaintext credential field '{path}.{key}' must use the credential vault."
                )
            _reject_plaintext_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_plaintext_secrets(item, f"{path}[{index}]")


def _valid_env_name(value: str) -> bool:
    return bool(value) and value.replace("_", "A").isalnum() and not value[0].isdigit()


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "access_token",
                "api_key",
                "apikey",
                "authorization",
                "bearer_token",
                "client_secret",
                "password",
                "refresh_token",
                "secret",
            }:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value

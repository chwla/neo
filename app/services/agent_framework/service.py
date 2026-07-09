from __future__ import annotations

import re
import uuid

from app.services.agent_framework import store
from app.services.agent_framework.permissions import clamp_permissions, clamp_tools
from app.services.agent_framework.registry import built_in_definitions
from app.services.agent_framework.types import (
    AgentDefinition,
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
)
from app.services.llm_registry.service import LLMRegistryService
from app.services.rules import store as rules_store


class AgentFrameworkValidationError(ValueError):
    pass


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.")[:80] or str(uuid.uuid4())


class AgentDefinitionService:
    def __init__(self) -> None:
        store.initialize_agent_framework_tables()
        self.seed_builtins()

    def seed_builtins(self) -> list[AgentDefinition]:
        seeded = []
        now = store.now_iso()
        for item in built_in_definitions():
            normalized = self._normalize({**item, "created_at": now, "updated_at": now})
            seeded.append(AgentDefinition(**store.upsert_definition(normalized)))
        return seeded

    def reset_builtins(self) -> list[AgentDefinition]:
        return self.seed_builtins()

    def list(self, *, include_disabled: bool = True) -> list[AgentDefinition]:
        return [
            AgentDefinition(**self._with_warnings(item))
            for item in store.list_definitions(include_disabled=include_disabled)
        ]

    def get(self, agent_id: str, *, require_enabled: bool = False) -> AgentDefinition | None:
        item = store.get_definition(agent_id)
        if not item or (require_enabled and not item["enabled"]):
            return None
        return AgentDefinition(**self._with_warnings(item))

    def create(self, payload: AgentDefinitionCreate) -> AgentDefinition:
        now = store.now_iso()
        data = payload.model_dump()
        data["id"] = data.get("id") or f"custom-{_slug(data['name'])}"
        data["name"] = _slug(data["name"])
        data["agent_type"] = data.get("agent_type") or "custom"
        data["built_in"] = False
        data["created_at"] = now
        data["updated_at"] = now
        item = store.insert_definition(self._normalize(data))
        return AgentDefinition(**self._with_warnings(item))

    def update(self, agent_id: str, payload: AgentDefinitionUpdate) -> AgentDefinition:
        current = store.get_definition(agent_id)
        if not current:
            raise AgentFrameworkValidationError("Agent definition not found.")
        updates = payload.model_dump(exclude_unset=True)
        if current["built_in"]:
            allowed = {"enabled", "default_route_name", "rules_profile_ids", "metadata"}
            updates = {key: value for key, value in updates.items() if key in allowed}
        merged = {**current, **updates}
        normalized = self._normalize(merged)
        allowed_update = {
            key: normalized[key]
            for key in (
                "display_name",
                "description",
                "system_prompt",
                "default_route_name",
                "rules_profile_ids",
                "permissions",
                "tools",
                "enabled",
                "priority",
                "metadata",
            )
            if key in normalized
        }
        item = store.update_definition(agent_id, allowed_update)
        if not item:
            raise AgentFrameworkValidationError("Agent definition not found.")
        return AgentDefinition(**self._with_warnings(item))

    def disable(self, agent_id: str) -> AgentDefinition:
        current = store.get_definition(agent_id)
        if not current:
            raise AgentFrameworkValidationError("Agent definition not found.")
        item = store.update_definition(agent_id, {"enabled": False})
        return AgentDefinition(**self._with_warnings(item))

    def resolve_for_run(
        self, agent_id: str | None, *, fallback: str = "general"
    ) -> AgentDefinition:
        item = self.get(agent_id or fallback, require_enabled=True) or self.get(
            fallback, require_enabled=True
        )
        if not item:
            raise AgentFrameworkValidationError("No enabled agent definition is available.")
        return item

    def _normalize(self, data: dict) -> dict:
        permissions, permission_warnings = clamp_permissions(
            data.get("permissions", {}),
            agent_type=data.get("agent_type", "custom"),
            built_in=bool(data.get("built_in")),
        )
        tools, tool_warnings = clamp_tools(data.get("tools", []))
        route = data.get("default_route_name")
        warnings = [*permission_warnings, *tool_warnings]
        if route:
            if not LLMRegistryService().get_route(route):
                warnings.append(f"Model route '{route}' is unavailable and was ignored.")
                route = None
        profile_ids = []
        for profile_id in data.get("rules_profile_ids", []):
            if rules_store.get_profile(profile_id):
                profile_ids.append(profile_id)
            else:
                warnings.append(f"Rules profile '{profile_id}' is unavailable and was ignored.")
        metadata = {**(data.get("metadata") or {}), "safety_warnings": warnings}
        return {
            **data,
            "default_route_name": route,
            "rules_profile_ids": profile_ids,
            "permissions": permissions.model_dump(),
            "tools": tools,
            "metadata": metadata,
        }

    @staticmethod
    def _with_warnings(item: dict) -> dict:
        return {
            **item,
            "safety_warnings": list((item.get("metadata") or {}).get("safety_warnings", [])),
        }

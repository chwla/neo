from __future__ import annotations

import uuid

import app.services.agents.store as agent_store
from app.services.agent_framework import store
from app.services.agent_framework.service import (
    AgentDefinitionService,
    AgentFrameworkValidationError,
)
from app.services.agent_framework.types import AgentDelegation, DelegationCreate, DelegationUpdate

MAX_DELEGATION_DEPTH = 2
MAX_CHILD_RUNS_PER_PARENT = 5


class AgentDelegationService:
    def __init__(self) -> None:
        store.initialize_agent_framework_tables()
        self.definitions = AgentDefinitionService()

    def create(self, payload: DelegationCreate) -> AgentDelegation:
        parent_run = agent_store.get_run(payload.parent_run_id)
        if not parent_run:
            raise AgentFrameworkValidationError("Parent agent run not found.")
        parent_agent_id = (
            payload.parent_agent_id or parent_run.get("agent_definition_id") or "general"
        )
        parent_agent = self.definitions.get(parent_agent_id, require_enabled=True)
        if not parent_agent:
            raise AgentFrameworkValidationError("Parent agent is disabled or missing.")
        if not parent_agent.permissions.can_delegate:
            raise AgentFrameworkValidationError("Parent agent cannot delegate.")
        child_agent = self.definitions.get(payload.child_agent_id, require_enabled=True)
        if not child_agent:
            raise AgentFrameworkValidationError("Child agent is disabled or missing.")
        existing = store.list_delegations(parent_run_id=payload.parent_run_id, limit=500)
        max_children = min(parent_agent.permissions.max_delegations, MAX_CHILD_RUNS_PER_PARENT)
        if len(existing) >= max_children:
            raise AgentFrameworkValidationError("Delegation child-run limit reached.")
        if self._depth(payload.parent_run_id) >= MAX_DELEGATION_DEPTH:
            raise AgentFrameworkValidationError("Delegation depth limit reached.")
        now = store.now_iso()
        item = store.insert_delegation(
            {
                "id": str(uuid.uuid4()),
                "parent_run_id": payload.parent_run_id,
                "child_run_id": payload.child_run_id,
                "parent_agent_id": parent_agent.id,
                "child_agent_id": child_agent.id,
                "delegation_type": payload.delegation_type,
                "objective": payload.objective,
                "status": "pending",
                "input": payload.input,
                "output": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
        )
        return AgentDelegation(**item)

    def list(self, **filters) -> list[AgentDelegation]:
        return [AgentDelegation(**item) for item in store.list_delegations(**filters)]

    def get(self, delegation_id: str) -> AgentDelegation | None:
        item = store.get_delegation(delegation_id)
        return AgentDelegation(**item) if item else None

    def update(self, delegation_id: str, payload: DelegationUpdate) -> AgentDelegation:
        updates = payload.model_dump(exclude_unset=True)
        if updates.get("status") in {"completed", "failed", "cancelled", "rejected"}:
            updates["completed_at"] = store.now_iso()
        item = store.update_delegation(delegation_id, updates)
        if not item:
            raise AgentFrameworkValidationError("Delegation not found.")
        return AgentDelegation(**item)

    def _depth(self, parent_run_id: str) -> int:
        depth = 0
        current = parent_run_id
        while True:
            parents = store.list_delegations(child_run_id=current, limit=1)
            if not parents:
                return depth
            depth += 1
            current = parents[0]["parent_run_id"]

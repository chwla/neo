from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.agent_framework import (
    AgentDefinition,
    AgentDefinitionCreate,
    AgentDefinitionService,
    AgentDefinitionUpdate,
    AgentDelegation,
    AgentDelegationService,
    AgentFrameworkValidationError,
    DelegationCreate,
    DelegationUpdate,
)

router = APIRouter(prefix="/agents", tags=["agent-framework"])


class AgentDefinitionsResponse(BaseModel):
    definitions: list[AgentDefinition]


class AgentDefinitionResponse(BaseModel):
    definition: AgentDefinition


class DelegationsResponse(BaseModel):
    delegations: list[AgentDelegation]


class DelegationResponse(BaseModel):
    delegation: AgentDelegation


def _definition_service() -> AgentDefinitionService:
    return AgentDefinitionService()


def _delegation_service() -> AgentDelegationService:
    return AgentDelegationService()


def _raise(exc: Exception) -> None:
    status = 404 if "not found" in str(exc).lower() or "missing" in str(exc).lower() else 400
    raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/definitions", response_model=AgentDefinitionsResponse)
def list_definitions(include_disabled: bool = True):
    return AgentDefinitionsResponse(
        definitions=_definition_service().list(include_disabled=include_disabled)
    )


@router.post("/definitions", response_model=AgentDefinitionResponse, status_code=201)
def create_definition(payload: AgentDefinitionCreate):
    try:
        return AgentDefinitionResponse(definition=_definition_service().create(payload))
    except (AgentFrameworkValidationError, ValueError) as exc:
        _raise(exc)


@router.get("/definitions/{agent_id}", response_model=AgentDefinitionResponse)
def get_definition(agent_id: str):
    item = _definition_service().get(agent_id)
    if item is None:
        raise HTTPException(404, "Agent definition not found.")
    return AgentDefinitionResponse(definition=item)


@router.patch("/definitions/{agent_id}", response_model=AgentDefinitionResponse)
def update_definition(agent_id: str, payload: AgentDefinitionUpdate):
    try:
        return AgentDefinitionResponse(definition=_definition_service().update(agent_id, payload))
    except (AgentFrameworkValidationError, ValueError) as exc:
        _raise(exc)


@router.delete("/definitions/{agent_id}", response_model=AgentDefinitionResponse)
def delete_definition(agent_id: str):
    try:
        return AgentDefinitionResponse(definition=_definition_service().disable(agent_id))
    except AgentFrameworkValidationError as exc:
        _raise(exc)


@router.post("/definitions/reset-builtins", response_model=AgentDefinitionsResponse)
def reset_builtins():
    return AgentDefinitionsResponse(definitions=_definition_service().reset_builtins())


@router.post("/delegations", response_model=DelegationResponse, status_code=201)
def create_delegation(payload: DelegationCreate):
    try:
        return DelegationResponse(delegation=_delegation_service().create(payload))
    except (AgentFrameworkValidationError, ValueError) as exc:
        _raise(exc)


@router.get("/delegations", response_model=DelegationsResponse)
def list_delegations(
    parent_run_id: str | None = None,
    child_run_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return DelegationsResponse(
        delegations=_delegation_service().list(
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
            status=status,
            limit=limit,
        )
    )


@router.get("/delegations/{delegation_id}", response_model=DelegationResponse)
def get_delegation(delegation_id: str):
    item = _delegation_service().get(delegation_id)
    if item is None:
        raise HTTPException(404, "Delegation not found.")
    return DelegationResponse(delegation=item)


@router.patch("/delegations/{delegation_id}", response_model=DelegationResponse)
def update_delegation(delegation_id: str, payload: DelegationUpdate):
    try:
        return DelegationResponse(delegation=_delegation_service().update(delegation_id, payload))
    except AgentFrameworkValidationError as exc:
        _raise(exc)

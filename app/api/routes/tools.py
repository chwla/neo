from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.tools import (
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
    store,
)
from app.services.tools.executor import ToolsService, ToolValidationError
from app.services.tools.mcp import discover_tools, health_check

router = APIRouter(prefix="/tools", tags=["tools"])


class ServersResponse(BaseModel):
    servers: list[ToolServer]


class ServerResponse(BaseModel):
    server: ToolServer


class DefinitionsResponse(BaseModel):
    definitions: list[ToolDefinition]


class DefinitionResponse(BaseModel):
    definition: ToolDefinition


class SkillsResponse(BaseModel):
    skills: list[SkillDefinition]


class SkillResponse(BaseModel):
    skill: SkillDefinition


class CallsResponse(BaseModel):
    calls: list[ToolCall]
    total: int


class CallResponse(BaseModel):
    call: ToolCall


class RejectRequest(BaseModel):
    reason: str | None = None


def _service() -> ToolsService:
    return ToolsService()


def _raise(exc: Exception) -> None:
    status = 404 if "not found" in str(exc).lower() else 400
    raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/servers", response_model=ServersResponse)
def list_servers(include_disabled: bool = True):
    return ServersResponse(servers=_service().list_servers(include_disabled=include_disabled))


@router.post("/servers", response_model=ServerResponse, status_code=201)
def create_server(payload: ToolServerCreate):
    try:
        return ServerResponse(server=_service().create_server(payload))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.patch("/servers/{server_id}", response_model=ServerResponse)
def update_server(server_id: str, payload: ToolServerUpdate):
    try:
        return ServerResponse(server=_service().update_server(server_id, payload))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.delete("/servers/{server_id}", response_model=ServerResponse)
def delete_server(server_id: str):
    try:
        return ServerResponse(server=_service().disable_server(server_id))
    except ToolValidationError as exc:
        _raise(exc)


@router.post("/servers/{server_id}/health")
def server_health(server_id: str):
    server = store.get_server(server_id)
    if not server:
        raise HTTPException(404, "Tool server not found.")
    return {"health": health_check(server)}


@router.post("/servers/{server_id}/discover", response_model=DefinitionsResponse)
def server_discover(server_id: str):
    server = store.get_server(server_id)
    if not server:
        raise HTTPException(404, "Tool server not found.")
    if not server.get("enabled"):
        raise HTTPException(400, "Disabled servers cannot discover tools.")
    definitions = []
    now = store.now_iso()
    for item in discover_tools(server):
        definitions.append(
            ToolDefinition(
                **store.upsert_tool({**item, "created_at": now, "updated_at": now})
            )
        )
    return DefinitionsResponse(definitions=definitions)


@router.get("/definitions", response_model=DefinitionsResponse)
def list_definitions(include_disabled: bool = True, server_id: str | None = None):
    return DefinitionsResponse(
        definitions=_service().list_tools(include_disabled=include_disabled, server_id=server_id)
    )


@router.post("/definitions", response_model=DefinitionResponse, status_code=201)
def create_definition(payload: ToolDefinitionCreate):
    try:
        return DefinitionResponse(definition=_service().create_tool(payload))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.patch("/definitions/{tool_id}", response_model=DefinitionResponse)
def update_definition(tool_id: str, payload: ToolDefinitionUpdate):
    try:
        return DefinitionResponse(definition=_service().update_tool(tool_id, payload))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.delete("/definitions/{tool_id}", response_model=DefinitionResponse)
def delete_definition(tool_id: str):
    try:
        return DefinitionResponse(definition=_service().disable_tool(tool_id))
    except ToolValidationError as exc:
        _raise(exc)


@router.get("/skills", response_model=SkillsResponse)
def list_skills(include_disabled: bool = True):
    return SkillsResponse(skills=_service().list_skills(include_disabled=include_disabled))


@router.post("/skills", response_model=SkillResponse, status_code=201)
def create_skill(payload: SkillDefinitionCreate):
    try:
        return SkillResponse(skill=_service().create_skill(payload))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.patch("/skills/{skill_id}", response_model=SkillResponse)
def update_skill(skill_id: str, payload: SkillDefinitionUpdate):
    try:
        return SkillResponse(skill=_service().update_skill(skill_id, payload))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.delete("/skills/{skill_id}", response_model=SkillResponse)
def delete_skill(skill_id: str):
    try:
        return SkillResponse(skill=_service().disable_skill(skill_id))
    except ToolValidationError as exc:
        _raise(exc)


@router.post("/calls", response_model=CallResponse, status_code=201)
def create_call(payload: ToolCallCreate):
    try:
        return CallResponse(call=_service().request_call(payload))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.post("/calls/{call_id}/approve", response_model=CallResponse)
def approve_call(call_id: str):
    try:
        return CallResponse(call=_service().approve_call(call_id))
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.post("/calls/{call_id}/reject", response_model=CallResponse)
def reject_call(call_id: str, payload: RejectRequest | None = None):
    try:
        return CallResponse(
            call=_service().reject_call(call_id, payload.reason if payload else None)
        )
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


@router.get("/calls", response_model=CallsResponse)
def list_calls(
    run_id: str | None = None,
    coding_run_id: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    calls, total = _service().list_calls(
        run_id=run_id, coding_run_id=coding_run_id, status=status, limit=limit, offset=offset
    )
    return CallsResponse(calls=calls, total=total)


@router.get("/calls/{call_id}", response_model=CallResponse)
def get_call(call_id: str):
    call = _service().get_call(call_id)
    if not call:
        raise HTTPException(404, "Tool call not found.")
    return CallResponse(call=call)

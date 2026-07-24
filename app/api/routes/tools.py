from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from app.api.routes.accounts import SESSION_COOKIE, session_for
from app.services.tools import (
    ConnectorCredentialStatus,
    ConnectorCredentialWrite,
    ConnectorSelectionRequest,
    ManualRestToolRequest,
    OAuthCallbackRequest,
    OpenAPIImportRequest,
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
from app.services.tools.credentials import (
    credential_status,
    delete_server_credential,
    set_server_credential,
)
from app.services.tools.executor import ToolsService, ToolValidationError
from app.services.tools.mcp import discover_tools, health_check
from app.services.tools.oauth import (
    finish_oauth,
    refresh_oauth_token,
    revoke_oauth_token,
    session_binding,
    start_oauth,
)
from app.services.tools.rest import create_manual_rest_tool, import_openapi, rest_health_check


def _require_tools_session(request: Request) -> dict:
    session = session_for(request)
    if session is None:
        raise HTTPException(401, "Choose a local profile before configuring connectors.")
    return session


router = APIRouter(
    prefix="/tools",
    tags=["tools"],
    dependencies=[Depends(_require_tools_session)],
)


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


class CredentialResponse(BaseModel):
    credential: ConnectorCredentialStatus


class ConnectorImportResponse(BaseModel):
    server: ToolServer
    definitions: list[ToolDefinition]


def _service() -> ToolsService:
    return ToolsService()


def _raise(exc: Exception) -> None:
    status = 404 if "not found" in str(exc).lower() else 400
    raise HTTPException(status_code=status, detail=str(exc)) from exc


def _require_profile(request: Request) -> dict:
    return _require_tools_session(request)


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
    if (server.get("metadata") or {}).get("connector_type") in {"rest", "openapi"}:
        return {"health": rest_health_check(server)}
    return {"health": health_check(server)}


@router.post("/servers/{server_id}/test")
def server_test(server_id: str):
    return server_health(server_id)


@router.post("/servers/{server_id}/discover", response_model=DefinitionsResponse)
def server_discover(server_id: str):
    server = store.get_server(server_id)
    if not server:
        raise HTTPException(404, "Tool server not found.")
    if not server.get("enabled"):
        raise HTTPException(400, "Disabled servers cannot discover tools.")
    if (server.get("metadata") or {}).get("connector_type") in {"rest", "openapi"}:
        return DefinitionsResponse(
            definitions=_service().list_tools(
                include_disabled=False,
                server_id=server_id,
            )
        )
    try:
        definitions = []
        now = store.now_iso()
        for item in discover_tools(server):
            definitions.append(
                ToolDefinition(**store.upsert_tool({**item, "created_at": now, "updated_at": now}))
            )
        return DefinitionsResponse(definitions=definitions)
    except ValueError as exc:
        _raise(exc)


@router.post(
    "/connectors/openapi/import",
    response_model=ConnectorImportResponse,
    status_code=201,
)
def import_openapi_connector(payload: OpenAPIImportRequest, request: Request):
    _require_profile(request)
    try:
        server, definitions = import_openapi(payload)
        return ConnectorImportResponse(
            server=ToolServer(**server),
            definitions=[ToolDefinition(**item) for item in definitions],
        )
    except ValueError as exc:
        _raise(exc)


@router.post(
    "/connectors/openapi/file",
    response_model=ConnectorImportResponse,
    status_code=201,
)
async def import_openapi_file(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=120)],
    file: Annotated[UploadFile, File()],
    allow_trusted_localhost: Annotated[bool, Form()] = False,
):
    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(413, "OpenAPI file exceeds the 2 MiB limit.")
    try:
        document = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "OpenAPI file must be UTF-8 JSON or YAML.") from exc
    return import_openapi_connector(
        OpenAPIImportRequest(
            name=name,
            document=document,
            allow_trusted_localhost=allow_trusted_localhost,
        ),
        request,
    )


@router.post(
    "/connectors/rest",
    response_model=ConnectorImportResponse,
    status_code=201,
)
def create_rest_connector(payload: ManualRestToolRequest, request: Request):
    _require_profile(request)
    try:
        server, definition = create_manual_rest_tool(payload)
        return ConnectorImportResponse(
            server=ToolServer(**server),
            definitions=[ToolDefinition(**definition)],
        )
    except ValueError as exc:
        _raise(exc)


@router.put(
    "/servers/{server_id}/credentials",
    response_model=CredentialResponse,
)
def set_credentials(server_id: str, payload: ConnectorCredentialWrite, request: Request):
    _require_profile(request)
    try:
        return CredentialResponse(
            credential=ConnectorCredentialStatus(**set_server_credential(server_id, payload))
        )
    except ValueError as exc:
        _raise(exc)


@router.get(
    "/servers/{server_id}/credentials",
    response_model=CredentialResponse,
)
def get_credentials(server_id: str, request: Request):
    _require_profile(request)
    try:
        return CredentialResponse(
            credential=ConnectorCredentialStatus(**credential_status(server_id))
        )
    except ValueError as exc:
        _raise(exc)


@router.delete("/servers/{server_id}/credentials", status_code=204)
def delete_credentials(server_id: str, request: Request):
    _require_profile(request)
    try:
        delete_server_credential(server_id)
    except ValueError as exc:
        _raise(exc)


def _oauth_context(server_id: str, request: Request) -> tuple[dict, str]:
    server = store.get_server(server_id)
    if not server:
        raise HTTPException(404, "Tool server not found.")
    try:
        binding = session_binding(request.cookies.get(SESSION_COOKIE))
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    return server, binding


@router.post("/servers/{server_id}/oauth/start")
def oauth_start(server_id: str, request: Request):
    server, binding = _oauth_context(server_id, request)
    try:
        return start_oauth(server, session_hash=binding)
    except ValueError as exc:
        _raise(exc)


@router.post("/servers/{server_id}/oauth/callback")
def oauth_callback(
    server_id: str,
    payload: OAuthCallbackRequest,
    request: Request,
):
    server, binding = _oauth_context(server_id, request)
    try:
        return finish_oauth(
            server,
            state=payload.state,
            code=payload.code,
            session_hash=binding,
        )
    except ValueError as exc:
        _raise(exc)


@router.get("/servers/{server_id}/oauth/callback")
def oauth_callback_get(server_id: str, state: str, code: str, request: Request):
    return oauth_callback(
        server_id,
        OAuthCallbackRequest(state=state, code=code),
        request,
    )


@router.post("/servers/{server_id}/oauth/refresh")
def oauth_refresh(server_id: str, request: Request):
    server, _ = _oauth_context(server_id, request)
    try:
        return refresh_oauth_token(server)
    except ValueError as exc:
        _raise(exc)


@router.post("/servers/{server_id}/oauth/revoke")
def oauth_revoke(server_id: str, request: Request):
    server, _ = _oauth_context(server_id, request)
    try:
        return revoke_oauth_token(server)
    except ValueError as exc:
        _raise(exc)


@router.post("/connectors/select")
def select_connector(payload: ConnectorSelectionRequest, request: Request):
    _require_profile(request)
    try:
        service = _service()
        if payload.invoke:
            return service.invoke_connector(
                capability=payload.capability,
                intent=payload.intent,
                arguments=payload.arguments,
            )
        selected = service.select_enabled_read_tool(
            payload.capability,
            intent=payload.intent,
        )
        return {"definition": selected}
    except (ToolValidationError, ValueError) as exc:
        _raise(exc)


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

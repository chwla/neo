from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.tools import router
from app.core.config import active_profile_database_url, active_profile_storage_dir
from app.services.tools import store
from app.services.tools.credentials import credential_status, set_server_credential
from app.services.tools.executor import ToolsService
from app.services.tools.mcp import discover_tools, execute_mcp_tool, health_check
from app.services.tools.oauth import (
    finish_oauth,
    refresh_oauth_token,
    revoke_oauth_token,
    start_oauth,
)
from app.services.tools.rest import import_openapi, rest_health_check
from app.services.tools.security import (
    ConnectorSecurityError,
    SafeResponse,
    validate_connector_url,
)
from app.services.tools.types import (
    ConnectorCredentialWrite,
    OpenAPIImportRequest,
    ToolCallCreate,
    ToolServerCreate,
)
from app.services.tools.vault import (
    ConnectorVaultError,
    master_key,
    read_credential,
    write_credential,
)


@pytest.fixture()
def connector_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "neo.db"
    database_token = active_profile_database_url.set(f"sqlite:///{path}")
    storage_token = active_profile_storage_dir.set(str(tmp_path))
    key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    monkeypatch.setenv("NEO_CONNECTOR_MASTER_KEY", key)
    try:
        store.initialize_tool_tables()
        yield path
    finally:
        active_profile_storage_dir.reset(storage_token)
        active_profile_database_url.reset(database_token)


def _openapi_document() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Weather", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/weather/{city}": {
                "get": {
                    "operationId": "current_weather",
                    "summary": "Current weather",
                    "parameters": [
                        {
                            "name": "city",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "maxLength": 120},
                        },
                        {
                            "name": "units",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "enum": ["metric", "imperial"],
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"temperature": {"type": "number"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/alerts": {
                "post": {
                    "operationId": "create_alert",
                    "summary": "Create weather alert",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["city"],
                                    "properties": {"city": {"type": "string"}},
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                }
            },
        },
    }


def test_every_connector_route_requires_an_authenticated_profile() -> None:
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get("/tools/servers")

    assert response.status_code == 401
    assert response.json()["detail"] == ("Choose a local profile before configuring connectors.")


def test_openapi_import_static_auth_read_execution_and_write_approval(
    connector_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, definitions = import_openapi(
        OpenAPIImportRequest(name="Weather API", document=_openapi_document())
    )
    read_tool = next(item for item in definitions if item["name"] == "current_weather")
    write_tool = next(item for item in definitions if item["name"] == "create_alert")
    assert read_tool["category"] == "external_read"
    assert write_tool["category"] == "external_write_approval_required"

    status = set_server_credential(
        server["id"],
        ConnectorCredentialWrite(
            auth_type="api_key_header",
            header_name="X-API-Key",
            secret="super-secret-value",
        ),
    )
    assert status["configured"] is True
    assert "secret" not in json.dumps(status).lower()

    requests: list[dict] = []

    def fake_request(method: str, url: str, **kwargs) -> SafeResponse:
        requests.append({"method": method, "url": url, **kwargs})
        return SafeResponse(
            url=url,
            status_code=201 if method == "POST" else 200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"created": True} if method == "POST" else {"temperature": 27.5}
            ).encode(),
        )

    monkeypatch.setattr("app.services.tools.rest.safe_request", fake_request)
    service = ToolsService()
    invoked = service.invoke_connector(
        capability="weather",
        arguments={"city": "New Delhi", "units": "metric"},
    )
    assert invoked["status"] == "completed"
    assert invoked["result"] == {"temperature": 27.5}
    assert invoked["provenance"]["untrusted_external_content"] is True
    assert requests[0]["url"] == "https://api.example.com/v1/weather/New%20Delhi"
    assert requests[0]["headers"]["X-API-Key"] == "super-secret-value"
    assert requests[0]["params"] == {"units": "metric"}

    pending = service.invoke_connector(
        tool_id=write_tool["id"],
        arguments={"body": {"city": "Delhi"}},
    )
    assert pending["status"] == "pending_approval"
    assert pending["approval_required"] is True
    assert len(requests) == 1
    approved = service.approve_call(pending["call_id"])
    assert approved.status == "completed"
    assert approved.output["result"] == {"created": True}
    assert len(requests) == 2

    raw_database = connector_database.read_bytes()
    assert b"super-secret-value" not in raw_database
    connection = sqlite3.connect(connector_database)
    try:
        row = connection.execute(
            "SELECT secret_nonce, secret_ciphertext FROM workspace_connector_credentials "
            "WHERE server_id=?",
            (server["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert row and row[0] and row[1]


def test_runtime_method_policy_prevents_category_tampering(
    connector_database: Path,
) -> None:
    _, definitions = import_openapi(
        OpenAPIImportRequest(name="Weather API", document=_openapi_document())
    )
    write_tool = next(item for item in definitions if item["name"] == "create_alert")
    store.update_tool(write_tool["id"], {"category": "external_read"})

    call = ToolsService().request_call(
        ToolCallCreate(
            tool_id=write_tool["id"],
            input={"body": {"city": "Delhi"}},
        )
    )
    assert call.status == "pending_approval"
    assert call.approval_status == "pending"


def test_connector_ciphertext_is_bound_to_the_active_profile(
    connector_database: Path,
    tmp_path: Path,
) -> None:
    server, _ = import_openapi(
        OpenAPIImportRequest(name="Profile API", document=_openapi_document())
    )
    set_server_credential(
        server["id"],
        ConnectorCredentialWrite(
            auth_type="bearer",
            secret="profile-bound-secret",
        ),
    )

    token = active_profile_storage_dir.set(str(tmp_path / "different-profile"))
    try:
        with pytest.raises(ConnectorVaultError, match="could not be decrypted"):
            read_credential(server["id"])
    finally:
        active_profile_storage_dir.reset(token)


def test_production_vault_fails_closed_without_a_supplied_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEO_CONNECTOR_MASTER_KEY", raising=False)
    monkeypatch.setenv("NEO_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "NEO_CONNECTOR_MASTER_KEY_FILE",
        str(tmp_path / "missing-production-key"),
    )

    with pytest.raises(ConnectorVaultError, match="Production connector encryption"):
        master_key()


def test_oauth_credential_rotation_uses_compare_and_swap(
    connector_database: Path,
) -> None:
    server, _ = import_openapi(OpenAPIImportRequest(name="CAS API", document=_openapi_document()))
    set_server_credential(
        server["id"],
        ConnectorCredentialWrite(
            auth_type="bearer",
            secret="first-token",
        ),
    )
    record = store.get_connector_credential(server["id"])
    assert record is not None
    write_credential(
        server_id=server["id"],
        auth_type="bearer",
        label=None,
        public_config={},
        secret={"access_token": "second-token"},
        expected_updated_at=record["updated_at"],
    )

    with pytest.raises(ConnectorVaultError, match="changed during refresh"):
        write_credential(
            server_id=server["id"],
            auth_type="bearer",
            label=None,
            public_config={},
            secret={"access_token": "stale-third-token"},
            expected_updated_at=record["updated_at"],
        )


def test_http_mcp_initializes_discovers_and_calls_with_sse_compatibility(
    connector_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolsService()
    server = service.create_server(
        ToolServerCreate(
            name="Local MCP",
            server_type="http",
            url="http://127.0.0.1:8765/mcp",
            approval_required=True,
            metadata={"trusted_localhost": True, "connector_type": "mcp"},
        )
    ).model_dump()
    seen: list[str] = []

    def fake_mcp_request(_method: str, url: str, **kwargs) -> SafeResponse:
        request = kwargs["json_body"]
        rpc_method = request["method"]
        seen.append(rpc_method)
        if rpc_method == "initialize":
            message = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                },
            }
            return SafeResponse(
                url=url,
                status_code=200,
                headers={
                    "content-type": "application/json",
                    "mcp-session-id": "session-1",
                },
                body=json.dumps(message).encode(),
            )
        if rpc_method == "notifications/initialized":
            assert kwargs["headers"]["MCP-Session-Id"] == "session-1"
            return SafeResponse(url=url, status_code=202, headers={}, body=b"")
        if rpc_method == "tools/list":
            message = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {
                            "name": "lookup_weather",
                            "description": "Look up weather",
                            "inputSchema": {
                                "type": "object",
                                "required": ["city"],
                                "properties": {"city": {"type": "string"}},
                                "additionalProperties": False,
                            },
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                },
            }
            return SafeResponse(
                url=url,
                status_code=200,
                headers={"content-type": "text/event-stream"},
                body=f"event: message\ndata: {json.dumps(message)}\n\n".encode(),
            )
        assert rpc_method == "tools/call"
        assert request["params"] == {
            "name": "lookup_weather",
            "arguments": {"city": "Delhi"},
        }
        message = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "27 C"}]},
        }
        return SafeResponse(
            url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(message).encode(),
        )

    monkeypatch.setattr("app.services.tools.mcp.safe_request", fake_mcp_request)
    definitions = discover_tools(server)
    assert definitions[0]["category"] == "external_read"
    definition = store.upsert_tool(
        {
            **definitions[0],
            "created_at": store.now_iso(),
            "updated_at": store.now_iso(),
        }
    )
    result = execute_mcp_tool(server, definition, {"city": "Delhi"})
    assert result["result"]["content"][0]["text"] == "27 C"
    assert result["provenance"]["transport"] == "mcp_http"
    assert result["provenance"]["untrusted_external_content"] is True
    assert seen.count("initialize") == 2


def test_legacy_get_sse_mcp_transport(
    connector_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict] = []

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def __init__(self) -> None:
            self.start = len(posted)

        def iter_lines(self, decode_unicode=True):
            del decode_unicode
            yield "event: endpoint"
            yield "data: /messages?sessionId=fixture"
            yield ""
            index = self.start
            while True:
                request = posted[index]
                index += 1
                if "id" not in request:
                    continue
                method = request["method"]
                if method == "initialize":
                    result = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                    }
                elif method == "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": "legacy_echo",
                                "inputSchema": {"type": "object"},
                                "annotations": {"readOnlyHint": True},
                            }
                        ]
                    }
                else:
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": request["params"]["arguments"]["text"],
                            }
                        ]
                    }
                message = {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": result,
                }
                yield "event: message"
                yield f"data: {json.dumps(message)}"
                yield ""

        def close(self):
            return None

    class FakeSession:
        trust_env = True

        def get(self, *_args, **_kwargs):
            return FakeStreamResponse()

        def close(self):
            return None

    fake_requests = SimpleNamespace(
        Session=FakeSession,
        RequestException=Exception,
    )
    monkeypatch.setattr("app.services.tools.mcp.requests", fake_requests)

    def fake_post(_method: str, url: str, **kwargs) -> SafeResponse:
        assert url == "http://127.0.0.1:8765/messages?sessionId=fixture"
        posted.append(kwargs["json_body"])
        return SafeResponse(url=url, status_code=202, headers={}, body=b"")

    monkeypatch.setattr("app.services.tools.mcp.safe_request", fake_post)
    server = (
        ToolsService()
        .create_server(
            ToolServerCreate(
                name="Legacy SSE",
                server_type="http",
                url="http://127.0.0.1:8765/sse",
                metadata={
                    "connector_type": "mcp",
                    "transport": "legacy_sse",
                    "trusted_localhost": True,
                },
            )
        )
        .model_dump()
    )
    definitions = discover_tools(server)
    assert definitions[0]["name"] == "legacy_echo"
    result = execute_mcp_tool(server, definitions[0], {"text": "hello"})
    assert result["result"]["content"][0]["text"] == "hello"


def test_stdio_mcp_uses_argv_without_shell_and_enforces_trust(
    connector_database: Path,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "mcp_fixture.py"
    fixture.write_text(
        """
import json
import sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"},
                             "annotations": {"readOnlyHint": True}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": message["params"]["arguments"]["text"]}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    service = ToolsService()
    with pytest.raises(ValueError, match="trusted_stdio"):
        service.create_server(
            ToolServerCreate(
                name="Untrusted",
                server_type="stdio",
                command_json=[sys.executable, str(fixture)],
            )
        )
    server = service.create_server(
        ToolServerCreate(
            name="Trusted fixture",
            server_type="stdio",
            command_json=[sys.executable, str(fixture)],
            metadata={"trusted_stdio": True},
        )
    ).model_dump()
    health = health_check(server)
    assert health["ok"] is True
    definitions = discover_tools(server)
    output = execute_mcp_tool(server, definitions[0], {"text": "hello"})
    assert output["result"]["content"][0]["text"] == "hello"


def test_oauth_pkce_state_binding_refresh_and_revoke(
    connector_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = (
        ToolsService()
        .create_server(
            ToolServerCreate(
                name="OAuth API",
                server_type="http",
                url="https://api.example.com/mcp",
                metadata={"connector_type": "mcp"},
            )
        )
        .model_dump()
    )
    public = set_server_credential(
        server["id"],
        ConnectorCredentialWrite(
            auth_type="oauth2",
            client_id="neo-client",
            client_secret="client-secret",
            authorization_url="https://auth.example.com/authorize",
            token_url="https://auth.example.com/token",
            revocation_url="https://auth.example.com/revoke",
            redirect_uri="http://127.0.0.1:8000/api/tools/oauth/callback",
            scopes=["read", "write"],
        ),
    )
    assert public["client_id"] == "neo-client"
    assert "client-secret" not in json.dumps(public)

    token_responses = iter(
        [
            {
                "access_token": "access-one",
                "refresh_token": "refresh-one",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            {
                "access_token": "access-two",
                "refresh_token": "refresh-two",
                "token_type": "Bearer",
                "expires_in": 7200,
            },
        ]
    )
    requests: list[dict] = []

    def fake_oauth_request(method: str, url: str, **kwargs) -> SafeResponse:
        requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/revoke"):
            return SafeResponse(url=url, status_code=200, headers={}, body=b"")
        return SafeResponse(
            url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            body=json.dumps(next(token_responses)).encode(),
        )

    monkeypatch.setattr("app.services.tools.oauth.safe_request", fake_oauth_request)
    started = start_oauth(server, session_hash="session-a")
    query = parse_qs(urlparse(started["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["read write"]
    state = query["state"][0]
    with pytest.raises(ValueError, match="another session"):
        finish_oauth(
            server,
            state=state,
            code="authorization-code",
            session_hash="session-b",
        )
    authorized = finish_oauth(
        server,
        state=state,
        code="authorization-code",
        session_hash="session-a",
    )
    assert authorized["authorized"] is True
    token_form = requests[0]["data"]
    assert token_form["grant_type"] == "authorization_code"
    assert token_form["code_verifier"]
    assert token_form["client_secret"] == "client-secret"
    assert credential_status(server["id"])["has_refresh_token"] is True

    refreshed = refresh_oauth_token(server)
    assert refreshed["authorized"] is True
    assert requests[1]["data"]["refresh_token"] == "refresh-one"
    revoked = revoke_oauth_token(server)
    assert revoked == {
        "server_id": server["id"],
        "revoked": True,
        "configured": False,
    }
    assert requests[2]["data"]["token"] == "refresh-two"
    assert credential_status(server["id"])["configured"] is False
    assert b"access-one" not in connector_database.read_bytes()
    assert b"refresh-two" not in connector_database.read_bytes()


def test_ssrf_and_plaintext_secret_guards(connector_database: Path) -> None:
    with pytest.raises(ConnectorSecurityError, match="private"):
        validate_connector_url("https://169.254.169.254/latest/meta-data")
    with pytest.raises(ConnectorSecurityError, match="HTTPS"):
        validate_connector_url("http://example.com", resolve=False)
    assert (
        validate_connector_url(
            "http://127.0.0.1:9000/mcp",
            allow_trusted_localhost=True,
        )
        == "http://127.0.0.1:9000/mcp"
    )
    with pytest.raises(ValueError, match="credential vault"):
        ToolsService().create_server(
            ToolServerCreate(
                name="Leaky",
                server_type="http",
                url="https://api.example.com/mcp",
                metadata={"api_key": "do-not-store"},
            )
        )


def test_rest_health_uses_safe_head_and_accepts_headless_api_root(
    connector_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _ = import_openapi(
        OpenAPIImportRequest(name="Weather API", document=_openapi_document())
    )
    seen: list[str] = []

    def fake_head(method: str, url: str, **_kwargs) -> SafeResponse:
        seen.append(method)
        return SafeResponse(url=url, status_code=405, headers={}, body=b"")

    monkeypatch.setattr("app.services.tools.rest.safe_request", fake_head)
    health = rest_health_check(server)
    assert health == {
        "ok": True,
        "status": "ready",
        "transport": "rest",
        "operation_count": 2,
        "http_status": 405,
    }
    assert seen == ["HEAD"]


def test_chat_facing_selection_defaults_to_no_action_when_ambiguous(
    connector_database: Path,
) -> None:
    server, definitions = import_openapi(
        OpenAPIImportRequest(name="Weather API", document=_openapi_document())
    )
    original = next(item for item in definitions if item["name"] == "current_weather")
    duplicate = {
        **original,
        "id": f"{original['id']}.duplicate",
        "server_id": server["id"],
        "created_at": store.now_iso(),
        "updated_at": store.now_iso(),
    }
    store.insert_tool(duplicate)

    service = ToolsService()
    assert service.select_enabled_read_tool("weather") is None
    result = service.invoke_connector(capability="weather", arguments={"city": "Delhi"})
    assert result["status"] == "not_selected"


def test_connector_route_contracts_are_registered() -> None:
    paths = {route.path for route in router.routes}
    assert {
        "/tools/connectors/openapi/import",
        "/tools/connectors/openapi/file",
        "/tools/connectors/rest",
        "/tools/connectors/select",
        "/tools/servers/{server_id}/credentials",
        "/tools/servers/{server_id}/oauth/start",
        "/tools/servers/{server_id}/oauth/callback",
        "/tools/servers/{server_id}/oauth/refresh",
        "/tools/servers/{server_id}/oauth/revoke",
    } <= paths


def test_pkce_challenge_is_sha256_urlsafe() -> None:
    # Contract-level guard for the exact transformation used by OAuth servers.
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert "=" not in challenge
    assert len(challenge) == 43

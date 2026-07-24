from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from app.services.tools.credentials import apply_server_auth
from app.services.tools.security import (
    ConnectorSecurityError,
    safe_request,
    validate_connector_url,
)

MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_TIMEOUT_SECONDS = 15.0
MAX_STDIO_LINE_BYTES = 2 * 1024 * 1024


class MCPError(ValueError):
    pass


def health_check(server: dict) -> dict[str, Any]:
    if not server.get("enabled"):
        return {"ok": False, "status": "disabled"}
    if server["server_type"] == "builtin":
        return {"ok": True, "status": "ready"}
    try:
        tools = _MCPConnection(server).list_tools()
        return {
            "ok": True,
            "status": "ready",
            "protocol_version": MCP_PROTOCOL_VERSION,
            "tool_count": len(tools),
            "transport": server["server_type"],
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "transport": server.get("server_type"),
            "error": _safe_error(exc),
        }


def discover_tools(server: dict) -> list[dict]:
    if server.get("server_type") not in {"http", "stdio"}:
        return []
    discovered = _MCPConnection(server).list_tools()
    definitions: list[dict] = []
    for item in discovered:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
        read_only = annotations.get("readOnlyHint") is True
        destructive = annotations.get("destructiveHint") is True
        category = (
            "external_read"
            if read_only and not destructive
            else ("external_write_approval_required")
        )
        definitions.append(
            {
                "id": f"mcp.{server['id']}.{_slug(name)}",
                "server_id": server["id"],
                "name": name,
                "display_name": item.get("title") or name,
                "description": item.get("description") or "Discovered MCP tool.",
                "category": category,
                "input_schema": item.get("inputSchema") or {"type": "object"},
                "output_schema": item.get("outputSchema") or {},
                "permissions": {
                    "source": "mcp_discovery",
                    "requires_approval": category.endswith("approval_required"),
                },
                "enabled": True,
                "built_in": False,
                "metadata": {
                    "executor": "mcp",
                    "discovered": True,
                    "mcp_tool_name": name,
                    "annotations": annotations,
                    "capabilities": _capabilities(item),
                },
            }
        )
    return definitions


def execute_mcp_tool(server: dict, tool: dict, payload: dict[str, Any]) -> dict[str, Any]:
    name = str((tool.get("metadata") or {}).get("mcp_tool_name") or tool["name"])
    result = _MCPConnection(server).call_tool(name, payload)
    if not isinstance(result, dict):
        raise MCPError("MCP tools/call returned an invalid result.")
    return {
        "result": _bounded_json(result),
        "provenance": {
            "connector_id": server["id"],
            "connector_name": server["name"],
            "transport": f"mcp_{server['server_type']}",
            "tool_name": name,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "untrusted_external_content": True,
        },
    }


# Backward-compatible import used by existing executor callers.
execute_mcp_read_only = execute_mcp_tool


class _MCPConnection:
    def __init__(self, server: dict) -> None:
        self.server = server

    def list_tools(self) -> list[dict]:
        result = self._session_request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise MCPError("MCP server returned an invalid tools/list response.")
        return [item for item in tools if isinstance(item, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        return self._session_request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    def _session_request(self, method: str, params: dict[str, Any]) -> dict:
        if not self.server.get("enabled"):
            raise MCPError("MCP server is disabled.")
        if self.server["server_type"] == "http":
            transport = str((self.server.get("metadata") or {}).get("transport") or "")
            if transport in {"sse", "legacy_sse"}:
                return self._legacy_sse_session(method, params)
            return self._http_session(method, params)
        if self.server["server_type"] == "stdio":
            return self._stdio_session(method, params)
        raise MCPError("Unsupported MCP transport.")

    def _legacy_sse_session(self, method: str, params: dict[str, Any]) -> dict:
        with _LegacySSESession(self.server) as session:
            initialize = session.rpc(
                _request(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "neo", "version": "0.1.0"},
                    },
                ),
                expected_id=1,
            )
            if not isinstance(initialize, dict):
                raise MCPError("Legacy MCP initialize returned an invalid response.")
            session.rpc(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                notification=True,
            )
            return session.rpc(_request(2, method, params), expected_id=2)

    def _http_session(self, method: str, params: dict[str, Any]) -> dict:
        url = self.server.get("url")
        if not url:
            raise MCPError("MCP HTTP server URL is missing.")
        metadata = self.server.get("metadata") or {}
        trusted_localhost = bool(metadata.get("trusted_localhost"))
        base_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        base_headers, auth_params = apply_server_auth(
            self.server,
            headers=base_headers,
        )
        initialize, session_id = _http_rpc(
            self.server,
            url,
            _request(
                1,
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "neo", "version": "0.1.0"},
                },
            ),
            headers=base_headers,
            params=auth_params,
            trusted_localhost=trusted_localhost,
        )
        if not isinstance(initialize, dict):
            raise MCPError("MCP initialize returned an invalid response.")
        negotiated = str(initialize.get("protocolVersion") or "")
        if negotiated and negotiated not in {MCP_PROTOCOL_VERSION, "2024-11-05"}:
            raise MCPError(f"MCP server selected unsupported protocol {negotiated}.")
        session_headers = dict(base_headers)
        if session_id:
            session_headers["MCP-Session-Id"] = session_id
        _http_rpc(
            self.server,
            url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
            params=auth_params,
            trusted_localhost=trusted_localhost,
            notification=True,
        )
        result, _ = _http_rpc(
            self.server,
            url,
            _request(2, method, params),
            headers=session_headers,
            params=auth_params,
            trusted_localhost=trusted_localhost,
        )
        return result

    def _stdio_session(self, method: str, params: dict[str, Any]) -> dict:
        argv = _trusted_stdio_argv(self.server)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            shell=False,
            env=_stdio_env(self.server.get("env_json") or {}),
        )
        try:
            initialize = _stdio_rpc(
                process,
                _request(
                    1,
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "neo", "version": "0.1.0"},
                    },
                ),
                expected_id=1,
            )
            if not isinstance(initialize, dict):
                raise MCPError("MCP initialize returned an invalid response.")
            _stdio_send(
                process,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            return _stdio_rpc(process, _request(2, method, params), expected_id=2)
        finally:
            _terminate(process)


def _http_rpc(
    server: dict,
    url: str,
    payload: dict,
    *,
    headers: dict[str, str],
    params: dict[str, object],
    trusted_localhost: bool,
    notification: bool = False,
) -> tuple[dict, str | None]:
    try:
        response = safe_request(
            "POST",
            url,
            allow_trusted_localhost=trusted_localhost,
            headers=headers,
            params=params,
            json_body=payload,
            timeout_seconds=MCP_TIMEOUT_SECONDS,
        )
    except ConnectorSecurityError as exc:
        raise MCPError(str(exc)) from exc
    if notification and response.status_code in {200, 202, 204}:
        return {}, response.headers.get("mcp-session-id")
    if response.status_code >= 400:
        raise MCPError(f"MCP server returned HTTP {response.status_code}.")
    message = _decode_rpc_response(response.body, response.headers.get("content-type", ""))
    if notification:
        return {}, response.headers.get("mcp-session-id")
    if not isinstance(message, dict):
        raise MCPError("MCP server returned an invalid JSON-RPC response.")
    if message.get("error"):
        raise MCPError("MCP server returned a JSON-RPC error.")
    result = message.get("result")
    if not isinstance(result, dict):
        raise MCPError("MCP JSON-RPC response did not contain an object result.")
    return result, response.headers.get("mcp-session-id")


class _LegacySSESession:
    """MCP 2024-11-05 HTTP+SSE client.

    The GET event stream supplies a session-specific POST endpoint. Requests
    are posted there while JSON-RPC responses arrive on the original stream.
    """

    def __init__(self, server: dict) -> None:
        self.server = server
        self.response: requests.Response | None = None
        self.events = None
        self.post_url: str | None = None
        self.headers: dict[str, str] = {}
        self.params: dict[str, object] = {}
        self.trusted_localhost = bool((server.get("metadata") or {}).get("trusted_localhost"))

    def __enter__(self) -> _LegacySSESession:
        url = self.server.get("url")
        if not url:
            raise MCPError("Legacy SSE MCP server URL is missing.")
        validate_connector_url(
            url,
            allow_trusted_localhost=self.trusted_localhost,
        )
        self.headers, self.params = apply_server_auth(
            self.server,
            headers={"Accept": "text/event-stream"},
        )
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                url,
                headers=self.headers,
                params=self.params,
                stream=True,
                timeout=(5.0, MCP_TIMEOUT_SECONDS),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            session.close()
            raise MCPError(f"Legacy SSE connection failed: {exc}") from exc
        if response.status_code >= 400:
            response.close()
            session.close()
            raise MCPError(f"Legacy SSE server returned HTTP {response.status_code}.")
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            response.close()
            session.close()
            raise MCPError("Legacy MCP endpoint did not return an SSE stream.")
        self.response = response
        self._requests_session = session
        self.events = _sse_events(response)
        endpoint_data = None
        for _ in range(20):
            event, data = self._next_event()
            if event == "endpoint" and data:
                endpoint_data = data
                break
        if not endpoint_data:
            self.__exit__(None, None, None)
            raise MCPError("Legacy MCP stream did not provide a POST endpoint.")
        endpoint = urljoin(url, endpoint_data)
        _require_same_origin(url, endpoint)
        validate_connector_url(
            endpoint,
            allow_trusted_localhost=self.trusted_localhost,
        )
        self.post_url = endpoint
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.response is not None:
            self.response.close()
        session = getattr(self, "_requests_session", None)
        if session is not None:
            session.close()

    def rpc(
        self,
        payload: dict,
        *,
        expected_id: int | None = None,
        notification: bool = False,
    ) -> dict:
        if not self.post_url:
            raise MCPError("Legacy MCP POST endpoint is unavailable.")
        post_headers = {
            key: value for key, value in self.headers.items() if key.lower() != "accept"
        }
        post_headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        response = safe_request(
            "POST",
            self.post_url,
            allow_trusted_localhost=self.trusted_localhost,
            headers=post_headers,
            params=self.params,
            json_body=payload,
            timeout_seconds=MCP_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            raise MCPError(f"Legacy MCP POST returned HTTP {response.status_code}.")
        if notification:
            return {}
        if response.body:
            try:
                immediate = _decode_rpc_response(
                    response.body,
                    response.headers.get("content-type", ""),
                )
            except MCPError:
                immediate = None
            if isinstance(immediate, dict) and immediate.get("id") == expected_id:
                return _rpc_result(immediate)
        while True:
            event, data = self._next_event()
            if event not in {"message", None} or not data:
                continue
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == expected_id:
                return _rpc_result(message)

    def _next_event(self) -> tuple[str | None, str]:
        if self.events is None:
            raise MCPError("Legacy MCP event stream is unavailable.")
        try:
            return next(self.events)
        except StopIteration as exc:
            raise MCPError("Legacy MCP event stream closed unexpectedly.") from exc
        except requests.RequestException as exc:
            raise MCPError(f"Legacy MCP event stream failed: {exc}") from exc


def _decode_rpc_response(body: bytes, content_type: str) -> dict:
    text = body.decode("utf-8", errors="replace").strip()
    if "text/event-stream" in content_type.lower() or text.startswith(("event:", "data:")):
        data_lines: list[str] = []
        messages: list[dict] = []
        for line in text.splitlines():
            if not line.strip():
                if data_lines:
                    messages.append(json.loads("\n".join(data_lines)))
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            messages.append(json.loads("\n".join(data_lines)))
        if not messages:
            raise MCPError("MCP SSE response did not contain JSON data.")
        return messages[-1]
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPError("MCP server returned malformed JSON.") from exc
    if not isinstance(message, dict):
        raise MCPError("MCP server returned a non-object JSON-RPC message.")
    return message


def _stdio_rpc(
    process: subprocess.Popen,
    payload: dict,
    *,
    expected_id: int,
) -> dict:
    _stdio_send(process, payload)
    if process.stdout is None:
        raise MCPError("MCP stdio stdout is unavailable.")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + MCP_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            if not selector.select(remaining):
                break
            line = process.stdout.readline()
            if not line:
                break
            if len(line.encode("utf-8")) > MAX_STDIO_LINE_BYTES:
                raise MCPError("MCP stdio response exceeded the size limit.")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != expected_id:
                continue
            if message.get("error"):
                raise MCPError("MCP server returned a JSON-RPC error.")
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPError("MCP JSON-RPC response did not contain an object result.")
            return result
    finally:
        selector.close()
    raise MCPError("MCP stdio request timed out or the server exited.")


def _stdio_send(process: subprocess.Popen, payload: dict) -> None:
    if process.stdin is None:
        raise MCPError("MCP stdio stdin is unavailable.")
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _trusted_stdio_argv(server: dict) -> list[str]:
    metadata = server.get("metadata") or {}
    if metadata.get("trusted_stdio") is not True:
        raise MCPError("Stdio MCP servers must be explicitly marked trusted_stdio.")
    argv = server.get("command_json") or []
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise MCPError("MCP stdio command must be a non-empty argv array.")
    executable = argv[0]
    if Path(executable).name.lower() in {
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "powershell",
        "pwsh",
        "sh",
        "zsh",
    }:
        raise MCPError("MCP stdio commands may not invoke a command shell.")
    resolved = (
        str(Path(executable).expanduser().resolve())
        if Path(executable).expanduser().is_absolute()
        else shutil.which(executable)
    )
    if not resolved or not Path(resolved).is_file():
        raise MCPError("MCP stdio executable was not found.")
    return [resolved, *argv[1:]]


def _stdio_env(env_refs: dict[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    }
    for target, source_ref in env_refs.items():
        if not target or not source_ref:
            continue
        if source_ref not in os.environ:
            raise MCPError(f"Referenced environment variable '{source_ref}' is unavailable.")
        env[str(target)] = os.environ[source_ref]
    return env


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _request(request_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _capabilities(item: dict) -> list[str]:
    annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
    values = [str(item.get("name") or ""), str(item.get("description") or "")]
    title = annotations.get("title")
    if title:
        values.append(str(title))
    tokens: list[str] = []
    for token in " ".join(values).lower().replace("_", " ").replace("-", " ").split():
        normalized = "".join(character for character in token if character.isalnum())
        if len(normalized) >= 3 and normalized not in tokens:
            tokens.append(normalized)
    return tokens[:32]


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value.lower()
    ).strip("-")


def _bounded_json(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_STDIO_LINE_BYTES:
        raise MCPError("MCP tool result exceeded the size limit.")
    return value


def _safe_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:500] or exc.__class__.__name__


def _sse_events(response: requests.Response):
    event_name: str | None = None
    data_lines: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = raw_line or ""
        if len(line.encode("utf-8")) > MAX_STDIO_LINE_BYTES:
            raise MCPError("Legacy MCP SSE event exceeded the size limit.")
        if not line:
            if data_lines or event_name:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines or event_name:
        yield event_name, "\n".join(data_lines)


def _require_same_origin(source: str, target: str) -> None:
    left, right = urlparse(source), urlparse(target)
    if (left.scheme, left.hostname, left.port) != (
        right.scheme,
        right.hostname,
        right.port,
    ):
        raise MCPError("Legacy MCP POST endpoint must use the SSE connection origin.")


def _rpc_result(message: dict) -> dict:
    if message.get("error"):
        raise MCPError("MCP server returned a JSON-RPC error.")
    result = message.get("result")
    if not isinstance(result, dict):
        raise MCPError("MCP JSON-RPC response did not contain an object result.")
    return result

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import quote, urljoin

import yaml

from app.services.tools import store
from app.services.tools.credentials import apply_server_auth
from app.services.tools.security import safe_request, validate_connector_url
from app.services.tools.types import ManualRestToolRequest, OpenAPIImportRequest

HTTP_METHODS = {"get", "head", "post", "put", "patch", "delete"}
MAX_OPENAPI_BYTES = 2 * 1024 * 1024


class ConnectorImportError(ValueError):
    pass


def rest_health_check(server: dict) -> dict[str, Any]:
    if not server.get("enabled"):
        return {"ok": False, "status": "disabled", "transport": "rest"}
    definitions = store.list_tools(include_disabled=False, server_id=server["id"])
    if not definitions:
        return {
            "ok": False,
            "status": "no_definitions",
            "transport": "rest",
            "operation_count": 0,
        }
    try:
        headers, params = apply_server_auth(
            server,
            headers={"Accept": "application/json, text/plain, */*"},
        )
        response = safe_request(
            "HEAD",
            str(server.get("url") or ""),
            allow_trusted_localhost=bool((server.get("metadata") or {}).get("trusted_localhost")),
            headers=headers,
            params=params,
            max_bytes=16_384,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "transport": "rest",
            "operation_count": len(definitions),
            "error": " ".join(str(exc).split())[:500],
        }
    if response.status_code in {401, 403}:
        status = "authentication_failed"
        ok = False
    elif response.status_code >= 500:
        status = "upstream_error"
        ok = False
    else:
        # 404/405 still prove that the configured origin is reachable; many
        # APIs intentionally do not expose a root or HEAD route.
        status = "ready"
        ok = True
    return {
        "ok": ok,
        "status": status,
        "transport": "rest",
        "operation_count": len(definitions),
        "http_status": response.status_code,
    }


def import_openapi(payload: OpenAPIImportRequest) -> tuple[dict, list[dict]]:
    document = _load_document(payload)
    version = str(document.get("openapi") or "")
    if not version.startswith("3."):
        raise ConnectorImportError("Only OpenAPI 3.x documents are supported.")
    base_url = _base_url(document)
    validate_connector_url(
        base_url,
        allow_trusted_localhost=payload.allow_trusted_localhost,
        resolve=False,
    )
    now = store.now_iso()
    server_id = f"server.openapi.{_slug(payload.name) or 'connector'}.{uuid.uuid4().hex[:12]}"
    server = store.insert_server(
        {
            "id": server_id,
            "name": payload.name,
            "server_type": "http",
            "command_json": None,
            "url": base_url,
            "env_json": {},
            "enabled": payload.enabled,
            # Read endpoints remain automatic; write categories always override
            # this setting in the central approval resolver.
            "approval_required": payload.default_write_approval,
            "metadata": {
                "connector_type": "openapi",
                "openapi_version": version,
                "trusted_localhost": payload.allow_trusted_localhost,
                "source_url": payload.document_url,
            },
            "created_at": now,
            "updated_at": now,
        }
    )
    definitions: list[dict] = []
    try:
        for definition in _openapi_definitions(document, server):
            definitions.append(store.upsert_tool(definition))
    except Exception:
        store.update_server(server_id, {"enabled": False})
        raise
    if not definitions:
        store.update_server(server_id, {"enabled": False})
        raise ConnectorImportError("OpenAPI document contains no supported operations.")
    return server, definitions


def create_manual_rest_tool(payload: ManualRestToolRequest) -> tuple[dict, dict]:
    now = store.now_iso()
    if payload.server_id:
        server = store.get_server(payload.server_id)
        if not server:
            raise ConnectorImportError("Tool server not found.")
        if (server.get("metadata") or {}).get("connector_type") not in {"rest", "openapi"}:
            raise ConnectorImportError("Selected server is not a REST/OpenAPI connector.")
    else:
        if not payload.server_name or not payload.base_url:
            raise ConnectorImportError(
                "Creating a REST connector requires server_name and base_url."
            )
        validate_connector_url(
            payload.base_url,
            allow_trusted_localhost=payload.allow_trusted_localhost,
            resolve=False,
        )
        server_id = (
            f"server.rest.{_slug(payload.server_name) or 'connector'}.{uuid.uuid4().hex[:12]}"
        )
        server = store.insert_server(
            {
                "id": server_id,
                "name": payload.server_name,
                "server_type": "http",
                "command_json": None,
                "url": payload.base_url,
                "env_json": {},
                "enabled": True,
                "approval_required": True,
                "metadata": {
                    "connector_type": "rest",
                    "trusted_localhost": payload.allow_trusted_localhost,
                },
                "created_at": now,
                "updated_at": now,
            }
        )
    method = payload.method.upper()
    _validate_operation_path(payload.path)
    if payload.read_only is True and method not in {"GET", "HEAD"}:
        raise ConnectorImportError("Only GET and HEAD REST operations can be marked read-only.")
    read_only = method in {"GET", "HEAD"} if payload.read_only is None else payload.read_only
    category = "external_read" if read_only else "external_write_approval_required"
    tool_id = f"rest.{server['id']}.{_slug(payload.name) or uuid.uuid4()}"
    definition = store.insert_tool(
        {
            "id": tool_id,
            "server_id": server["id"],
            "name": payload.name,
            "display_name": payload.display_name or payload.name,
            "description": payload.description,
            "category": category,
            "input_schema": payload.input_schema,
            "output_schema": payload.output_schema,
            "permissions": {"requires_approval": category == "external_write_approval_required"},
            "enabled": True,
            "built_in": False,
            "metadata": {
                "executor": "rest",
                "method": method,
                "path": payload.path,
                "parameter_locations": payload.parameter_locations,
                "capabilities": _tokenize(
                    f"{payload.name} {payload.display_name or ''} {payload.description or ''}"
                ),
            },
            "created_at": now,
            "updated_at": now,
        }
    )
    return server, definition


def execute_rest_tool(server: dict, tool: dict, payload: dict[str, Any]) -> dict[str, Any]:
    metadata = tool.get("metadata") or {}
    method = str(metadata.get("method") or "").upper()
    if method not in {item.upper() for item in HTTP_METHODS}:
        raise ConnectorImportError("REST tool has an unsupported HTTP method.")
    path = str(metadata.get("path") or "")
    _validate_operation_path(path)
    locations = metadata.get("parameter_locations") or {}
    if not isinstance(locations, dict):
        raise ConnectorImportError("REST tool parameter mapping is invalid.")
    headers: dict[str, str] = {"Accept": "application/json, text/plain"}
    query: dict[str, object] = {}
    body: object | None = None
    unused: dict[str, Any] = {}
    for key, value in payload.items():
        location = locations.get(key)
        if location == "path":
            marker = "{" + key + "}"
            if marker not in path:
                raise ConnectorImportError(f"REST path parameter '{key}' is not present in path.")
            path = path.replace(marker, quote(str(value), safe=""))
        elif location == "query":
            query[key] = value
        elif location == "header":
            if "\n" in str(value) or "\r" in str(value):
                raise ConnectorImportError("REST header values may not contain newlines.")
            if key.lower() in {
                "authorization",
                "connection",
                "content-length",
                "cookie",
                "host",
                "proxy-authorization",
                "transfer-encoding",
            }:
                raise ConnectorImportError(f"REST header parameter '{key}' is not allowed.")
            headers[key] = str(value)
        elif location == "body" or key == "body":
            body = value
        else:
            unused[key] = value
    if re.search(r"\{[^{}]+\}", path):
        raise ConnectorImportError("REST call is missing one or more path parameters.")
    if body is None and unused:
        body = unused
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers, query = apply_server_auth(server, headers=headers, params=query)
    base_url = str(server.get("url") or "")
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    response = safe_request(
        method,
        url,
        allow_trusted_localhost=bool((server.get("metadata") or {}).get("trusted_localhost")),
        headers=headers,
        params=query,
        json_body=body,
    )
    content_type = response.headers.get("content-type", "").lower()
    value: Any
    if not response.body:
        value = None
    elif "json" in content_type:
        try:
            value = response.json()
        except Exception as exc:
            raise ConnectorImportError("REST connector returned malformed JSON.") from exc
    else:
        value = response.body.decode("utf-8", errors="replace")
    if response.status_code >= 400:
        raise ConnectorImportError(f"REST connector returned HTTP {response.status_code}.")
    return {
        "result": value,
        "provenance": {
            "connector_id": server["id"],
            "connector_name": server["name"],
            "transport": "rest",
            "tool_name": tool["name"],
            "method": method,
            "url": response.url,
            "status_code": response.status_code,
            "untrusted_external_content": True,
        },
    }


def _load_document(payload: OpenAPIImportRequest) -> dict:
    if (payload.document is None) == (payload.document_url is None):
        raise ConnectorImportError("Provide exactly one OpenAPI document or document_url.")
    raw: str | dict
    if payload.document_url:
        response = safe_request(
            "GET",
            payload.document_url,
            allow_trusted_localhost=payload.allow_trusted_localhost,
            headers={"Accept": "application/json, application/yaml, text/yaml, text/plain"},
            max_bytes=MAX_OPENAPI_BYTES,
        )
        if response.status_code >= 400:
            raise ConnectorImportError(
                f"OpenAPI document fetch returned HTTP {response.status_code}."
            )
        raw = response.body.decode("utf-8", errors="strict")
    else:
        raw = payload.document  # type: ignore[assignment]
    if isinstance(raw, dict):
        document = raw
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_OPENAPI_BYTES:
            raise ConnectorImportError("OpenAPI document exceeds the size limit.")
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConnectorImportError("OpenAPI document is not valid JSON or YAML.") from exc
    else:
        raise ConnectorImportError("OpenAPI document must be an object or JSON/YAML string.")
    if not isinstance(document, dict):
        raise ConnectorImportError("OpenAPI document must contain an object.")
    return document


def _base_url(document: dict) -> str:
    servers = document.get("servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], dict):
        raise ConnectorImportError("OpenAPI document must define an absolute server URL.")
    url = str(servers[0].get("url") or "")
    if "{" in url or "}" in url:
        raise ConnectorImportError("OpenAPI server variables must be resolved before import.")
    return url


def _openapi_definitions(document: dict, server: dict) -> list[dict]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ConnectorImportError("OpenAPI paths must be an object.")
    definitions: list[dict] = []
    identifiers: set[str] = set()
    now = store.now_iso()
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        _validate_operation_path(path)
        shared_parameters = path_item.get("parameters") or []
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = [*shared_parameters, *(operation.get("parameters") or [])]
            input_schema, locations = _operation_input_schema(
                document,
                parameters,
                operation.get("requestBody"),
            )
            operation_id = str(
                operation.get("operationId")
                or f"{method.lower()}_{path.strip('/').replace('/', '_') or 'root'}"
            )
            category = (
                "external_read"
                if method.lower() in {"get", "head"}
                else "external_write_approval_required"
            )
            description = operation.get("description") or operation.get("summary")
            definition = {
                "id": f"openapi.{server['id']}.{_slug(operation_id)}",
                "server_id": server["id"],
                "name": operation_id,
                "display_name": operation.get("summary") or operation_id,
                "description": description,
                "category": category,
                "input_schema": input_schema,
                "output_schema": _response_schema(document, operation),
                "permissions": {
                    "requires_approval": category == "external_write_approval_required"
                },
                "enabled": True,
                "built_in": False,
                "metadata": {
                    "executor": "rest",
                    "method": method.upper(),
                    "path": path,
                    "parameter_locations": locations,
                    "operation_id": operation_id,
                    "capabilities": _tokenize(
                        f"{operation_id} {operation.get('summary') or ''} {description or ''} "
                        f"{' '.join(operation.get('tags') or [])}"
                    ),
                },
                "created_at": now,
                "updated_at": now,
            }
            if definition["id"] in identifiers:
                raise ConnectorImportError(f"OpenAPI operationId must be unique: {operation_id}")
            identifiers.add(definition["id"])
            definitions.append(definition)
    return definitions


def _operation_input_schema(
    document: dict,
    parameters: list,
    request_body: object,
) -> tuple[dict, dict[str, str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    locations: dict[str, str] = {}
    for raw in parameters:
        parameter = _resolve_local_ref(document, raw)
        if not isinstance(parameter, dict):
            continue
        name = str(parameter.get("name") or "")
        location = str(parameter.get("in") or "")
        if not name or location not in {"path", "query", "header"}:
            continue
        if name in properties and locations.get(name) != location:
            raise ConnectorImportError(f"OpenAPI parameter '{name}' occurs in multiple locations.")
        schema = _resolve_schema(document, parameter.get("schema") or {"type": "string"})
        properties[name] = schema
        locations[name] = location
        if parameter.get("required") or location == "path":
            required.append(name)
    if request_body:
        body = _resolve_local_ref(document, request_body)
        if isinstance(body, dict):
            content = body.get("content") or {}
            media = content.get("application/json") if isinstance(content, dict) else None
            if isinstance(media, dict):
                properties["body"] = _resolve_schema(
                    document, media.get("schema") or {"type": "object"}
                )
                locations["body"] = "body"
                if body.get("required"):
                    required.append("body")
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(set(required))
    return schema, locations


def _response_schema(document: dict, operation: dict) -> dict:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return {}
    for key in ("200", "201", "202", "204", "default"):
        response = _resolve_local_ref(document, responses.get(key))
        if not isinstance(response, dict):
            continue
        content = response.get("content") or {}
        media = content.get("application/json") if isinstance(content, dict) else None
        if isinstance(media, dict):
            return _resolve_schema(document, media.get("schema") or {})
    return {}


def _resolve_local_ref(document: dict, value: object) -> object:
    if not isinstance(value, dict) or "$ref" not in value:
        return value
    ref = str(value["$ref"])
    if not ref.startswith("#/"):
        raise ConnectorImportError("External OpenAPI references are not supported.")
    current: object = document
    for component in ref[2:].split("/"):
        key = component.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            raise ConnectorImportError(f"OpenAPI reference was not found: {ref}")
        current = current[key]
    return current


def _resolve_schema(document: dict, value: object, depth: int = 0) -> dict:
    if depth > 12:
        raise ConnectorImportError("OpenAPI schema reference depth exceeded the limit.")
    resolved = _resolve_local_ref(document, value)
    if not isinstance(resolved, dict):
        return {}
    if resolved is value:
        return resolved
    return _resolve_schema(document, resolved, depth + 1)


def _tokenize(value: str) -> list[str]:
    result: list[str] = []
    for token in value.lower().replace("_", " ").replace("-", " ").split():
        clean = "".join(character for character in token if character.isalnum())
        if len(clean) >= 3 and clean not in result:
            result.append(clean)
    return result[:48]


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value.lower()
    ).strip("-")


def _validate_operation_path(path: str) -> None:
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise ConnectorImportError("REST operation paths must be absolute URL paths.")

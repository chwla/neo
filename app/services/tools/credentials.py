from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.tools import store
from app.services.tools.types import ConnectorCredentialStatus, ConnectorCredentialWrite
from app.services.tools.vault import read_credential, write_credential


class ConnectorCredentialError(ValueError):
    pass


def set_server_credential(server_id: str, payload: ConnectorCredentialWrite) -> dict:
    if not store.get_server(server_id):
        raise ConnectorCredentialError("Tool server not found.")
    data = payload.model_dump()
    auth_type = data.pop("auth_type")
    label = data.pop("label")
    secret_value = data.pop("secret")
    client_secret = data.pop("client_secret")
    forbidden_extra = {
        str(key).lower().replace("-", "_") for key in (data.get("extra_token_params") or {})
    } & {
        "access_token",
        "api_key",
        "client_secret",
        "password",
        "refresh_token",
        "secret",
    }
    if forbidden_extra:
        raise ConnectorCredentialError(
            "OAuth extra_token_params may not contain credential values."
        )

    if auth_type == "none":
        secret: dict[str, Any] = {}
    elif auth_type == "api_key_header":
        if not secret_value or not data.get("header_name"):
            raise ConnectorCredentialError("Header API-key auth requires header_name and secret.")
        secret = {"api_key": secret_value}
    elif auth_type == "api_key_query":
        if not secret_value or not data.get("query_name"):
            raise ConnectorCredentialError("Query API-key auth requires query_name and secret.")
        secret = {"api_key": secret_value}
    elif auth_type == "bearer":
        if not secret_value:
            raise ConnectorCredentialError("Bearer auth requires a token.")
        secret = {"access_token": secret_value, "token_type": "Bearer"}
    elif auth_type == "oauth2":
        required = ("client_id", "authorization_url", "token_url", "redirect_uri")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ConnectorCredentialError(f"OAuth configuration is missing: {', '.join(missing)}.")
        secret = {"client_secret": client_secret} if client_secret else {}
    else:
        raise ConnectorCredentialError("Unsupported connector authentication type.")

    public_config = {key: value for key, value in data.items() if value not in (None, [], {})}
    return _public_status(
        write_credential(
            server_id=server_id,
            auth_type=auth_type,
            label=label,
            public_config=public_config,
            secret=secret,
        )
    )


def credential_status(server_id: str) -> dict:
    if not store.get_server(server_id):
        raise ConnectorCredentialError("Tool server not found.")
    record = store.get_connector_credential(server_id)
    if record is None:
        return ConnectorCredentialStatus(
            server_id=server_id,
            configured=False,
            auth_type="none",
        ).model_dump()
    return _public_status(record)


def delete_server_credential(server_id: str) -> bool:
    if not store.get_server(server_id):
        raise ConnectorCredentialError("Tool server not found.")
    return store.delete_connector_credential(server_id)


def apply_server_auth(
    server: dict,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, object] | None = None,
) -> tuple[dict[str, str], dict[str, object]]:
    """Apply a stored credential without returning it to API callers."""

    result_headers = dict(headers or {})
    result_params = dict(params or {})
    resolved = read_credential(server["id"])
    if resolved is None:
        return result_headers, result_params
    record, secret = resolved
    auth_type = record["auth_type"]
    config = record.get("public_config") or {}
    if auth_type == "api_key_header":
        result_headers[str(config["header_name"])] = str(secret["api_key"])
    elif auth_type == "api_key_query":
        result_params[str(config["query_name"])] = str(secret["api_key"])
    elif auth_type in {"bearer", "oauth2"}:
        if auth_type == "oauth2" and _expired(record.get("expires_at")):
            from app.services.tools.oauth import refresh_oauth_token

            refresh_oauth_token(server)
            record, secret = read_credential(server["id"]) or (record, secret)
        access_token = secret.get("access_token")
        if not access_token:
            raise ConnectorCredentialError("Connector OAuth authorization is incomplete.")
        token_type = str(secret.get("token_type") or "Bearer")
        result_headers["Authorization"] = f"{token_type} {access_token}"
    return result_headers, result_params


def _expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        expires = datetime.fromisoformat(value)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC) + timedelta(seconds=30)
    except ValueError:
        return True


def _public_status(record: dict) -> dict:
    config = record.get("public_config") or {}
    has_refresh_token = False
    try:
        resolved = read_credential(record["server_id"])
        has_refresh_token = bool(resolved and resolved[1].get("refresh_token"))
    except Exception:
        # Status endpoints must not expose decryption failures or ciphertext.
        has_refresh_token = False
    return ConnectorCredentialStatus(
        server_id=record["server_id"],
        configured=True,
        auth_type=record["auth_type"],
        label=record.get("label"),
        client_id=config.get("client_id"),
        header_name=config.get("header_name"),
        query_name=config.get("query_name"),
        scopes=list(config.get("scopes") or []),
        expires_at=record.get("expires_at"),
        has_refresh_token=has_refresh_token,
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
    ).model_dump()

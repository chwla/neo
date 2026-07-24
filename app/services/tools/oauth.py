from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

from app.services.tools import store
from app.services.tools.security import safe_request, validate_connector_url
from app.services.tools.vault import (
    open_json,
    read_credential,
    scoped_aad,
    seal_json,
    write_credential,
)

OAUTH_STATE_TTL_MINUTES = 10
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


class ConnectorOAuthError(ValueError):
    pass


def session_binding(raw_session_token: str | None) -> str:
    if not raw_session_token:
        raise ConnectorOAuthError("OAuth setup requires an authenticated local profile session.")
    return hashlib.sha256(raw_session_token.encode("utf-8")).hexdigest()


def start_oauth(server: dict, *, session_hash: str) -> dict:
    record, secret = _oauth_credential(server)
    config = record.get("public_config") or {}
    trusted_localhost = bool((server.get("metadata") or {}).get("trusted_localhost"))
    authorization_url = validate_connector_url(
        config["authorization_url"],
        allow_trusted_localhost=trusted_localhost,
        resolve=False,
    )
    redirect_uri = config["redirect_uri"]
    _validate_redirect_uri(redirect_uri)

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state_hash = hashlib.sha256(state.encode("ascii")).hexdigest()
    nonce, ciphertext = seal_json(
        {"verifier": verifier},
        aad=scoped_aad(f"oauth-state:{state_hash}"),
    )
    now = datetime.now(UTC)
    store.delete_expired_oauth_states(now.isoformat())
    store.insert_oauth_state(
        {
            "state_hash": state_hash,
            "server_id": server["id"],
            "session_hash": session_hash,
            "verifier_nonce": nonce,
            "verifier_ciphertext": ciphertext,
            "redirect_uri": redirect_uri,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES)).isoformat(),
        }
    )

    query = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    scopes = list(config.get("scopes") or [])
    if scopes:
        query["scope"] = " ".join(scopes)
    return {
        "authorization_url": f"{authorization_url}{'&' if '?' in authorization_url else '?'}"
        f"{urlencode(query)}",
        "expires_at": (now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES)).isoformat(),
        "pkce_method": "S256",
        "server_id": server["id"],
        "client_secret_configured": bool(secret.get("client_secret")),
    }


def finish_oauth(
    server: dict,
    *,
    state: str,
    code: str,
    session_hash: str,
) -> dict:
    state_hash = hashlib.sha256(state.encode("ascii")).hexdigest()
    now = datetime.now(UTC)
    state_record = store.consume_oauth_state(state_hash, session_hash, now.isoformat())
    if state_record is None or state_record["server_id"] != server["id"]:
        raise ConnectorOAuthError(
            "OAuth state is invalid, expired, used, or belongs to another session."
        )

    sealed = open_json(
        state_record["verifier_nonce"],
        state_record["verifier_ciphertext"],
        aad=scoped_aad(f"oauth-state:{state_hash}"),
    )
    record, secret = _oauth_credential(server)
    config = record.get("public_config") or {}
    if state_record["redirect_uri"] != config.get("redirect_uri"):
        raise ConnectorOAuthError("OAuth redirect URI no longer matches the configured value.")
    token = _token_request(
        server,
        config,
        secret,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": state_record["redirect_uri"],
            "code_verifier": sealed["verifier"],
        },
    )
    return _save_token(server, record, secret, token)


def refresh_oauth_token(server: dict) -> dict:
    with _refresh_lock(server["id"]):
        # Re-read inside the lock so concurrent callers never overwrite a
        # freshly rotated refresh token with stale state.
        record, secret = _oauth_credential(server)
        refresh_token = secret.get("refresh_token")
        if not refresh_token:
            raise ConnectorOAuthError("Connector has no OAuth refresh token.")
        config = record.get("public_config") or {}
        token = _token_request(
            server,
            config,
            secret,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        return _save_token(server, record, secret, token)


def revoke_oauth_token(server: dict) -> dict:
    record, secret = _oauth_credential(server)
    config = record.get("public_config") or {}
    revocation_url = config.get("revocation_url")
    token = secret.get("refresh_token") or secret.get("access_token")
    if revocation_url and token:
        trusted_localhost = bool((server.get("metadata") or {}).get("trusted_localhost"))
        form = {"token": str(token), "client_id": str(config["client_id"])}
        if secret.get("client_secret"):
            form["client_secret"] = str(secret["client_secret"])
        response = safe_request(
            "POST",
            revocation_url,
            allow_trusted_localhost=trusted_localhost,
            headers={"Accept": "application/json"},
            data=form,
        )
        if response.status_code >= 400:
            raise ConnectorOAuthError(f"OAuth revocation failed with HTTP {response.status_code}.")
    store.delete_connector_credential(server["id"])
    return {"server_id": server["id"], "revoked": True, "configured": False}


def _oauth_credential(server: dict) -> tuple[dict, dict]:
    resolved = read_credential(server["id"])
    if resolved is None or resolved[0].get("auth_type") != "oauth2":
        raise ConnectorOAuthError("OAuth is not configured for this connector.")
    return resolved


def _token_request(
    server: dict,
    config: dict,
    secret: dict,
    grant_params: dict[str, str],
) -> dict:
    trusted_localhost = bool((server.get("metadata") or {}).get("trusted_localhost"))
    form = {
        **{str(key): str(value) for key, value in (config.get("extra_token_params") or {}).items()},
        **grant_params,
        "client_id": str(config["client_id"]),
    }
    if secret.get("client_secret"):
        form["client_secret"] = str(secret["client_secret"])
    response = safe_request(
        "POST",
        config["token_url"],
        allow_trusted_localhost=trusted_localhost,
        headers={"Accept": "application/json"},
        data=form,
    )
    try:
        token = response.json()
    except Exception as exc:
        raise ConnectorOAuthError("OAuth token endpoint returned invalid JSON.") from exc
    if response.status_code >= 400 or not isinstance(token, dict) or not token.get("access_token"):
        raise ConnectorOAuthError("OAuth token exchange failed.")
    return token


def _save_token(server: dict, record: dict, old_secret: dict, token: dict) -> dict:
    now = datetime.now(UTC)
    try:
        expires_in = max(0, int(token.get("expires_in") or 0))
    except (TypeError, ValueError):
        expires_in = 0
    expires_at = (now + timedelta(seconds=expires_in)).isoformat() if expires_in else None
    secret = {
        "client_secret": old_secret.get("client_secret"),
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or old_secret.get("refresh_token"),
        "token_type": token.get("token_type") or "Bearer",
    }
    updated = write_credential(
        server_id=server["id"],
        auth_type="oauth2",
        label=record.get("label"),
        public_config=record.get("public_config") or {},
        secret={key: value for key, value in secret.items() if value},
        expires_at=expires_at,
        expected_updated_at=record.get("updated_at"),
    )
    return {
        "server_id": server["id"],
        "authorized": True,
        "expires_at": updated.get("expires_at"),
        "has_refresh_token": bool(secret.get("refresh_token")),
        "token_type": secret["token_type"],
    }


def _validate_redirect_uri(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectorOAuthError("OAuth redirect URI must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ConnectorOAuthError("OAuth redirect URI is invalid.")
    hostname = parsed.hostname.rstrip(".").lower()
    localhost = hostname == "localhost" or hostname.endswith(".localhost")
    if parsed.scheme != "https" and not localhost:
        try:
            import ipaddress

            localhost = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            localhost = False
    if parsed.scheme != "https" and not localhost:
        raise ConnectorOAuthError("OAuth redirect URI must use HTTPS except on localhost.")


def _refresh_lock(server_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        if server_id not in _refresh_locks:
            _refresh_locks[server_id] = threading.Lock()
        return _refresh_locks[server_id]

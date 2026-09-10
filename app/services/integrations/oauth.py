"""Authorization Code with PKCE, for a provider Neo talks to on the user's behalf.

Restored from ``app/services/tools/oauth.py`` as it stood at ``19d174e^`` and
narrowed: it is driven by a ``Provider`` declaration rather than by a
user-defined connector record, and its HTTP goes through ``http.py`` rather than
a general SSRF-guarded fetcher. The security properties are carried over
deliberately and each earns its place:

* **PKCE S256.** Neo is a public client -- its client secret ships to every
  install and RFC 8252 section 8.5 is explicit that this cannot be otherwise --
  so the proof that the caller who redeems a code is the caller who requested it
  is the verifier, not the secret.
* **State bound to the profile session.** The callback arrives as a top-level
  navigation carrying whatever cookie the browser has. Binding the state to
  ``sha256(session_token)`` is what stops a state minted under one profile from
  completing under another and attaching somebody else's mailbox to the wrong
  account.
* **The verifier is sealed at rest.** It sits in the database for up to ten
  minutes; unsealed, it would be the one value that turns a stolen code into a
  token.
* **Single use, enforced by the write.** See ``store.consume_oauth_state``.
* **Refresh is serialised and compare-and-swapped.** Two callers noticing an
  expired token both ask for a new one; the loser must not write its superseded
  rotation over the winner's, because the provider may already have invalidated
  it.

Nothing here formats a provider response into an error. Everything raised is
either ``IntegrationOAuthError`` with fixed text or an ``IntegrationHttpError``
from the transport, for the reason given in ``http.py``'s docstring.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

from app.core.origins import is_allowed_return_origin
from app.services.integrations import capabilities as capability_vocabulary
from app.services.integrations import store
from app.services.integrations.http import request_json
from app.services.integrations.providers.base import (
    OAuthClient,
    Provider,
    UnknownProviderError,
    get_provider,
)
from app.services.integrations.vault import open_json, scoped_aad, seal_json

#: Long enough to read a consent screen and pick an account, short enough that an
#: abandoned attempt is not a credential sitting in the database all week.
OAUTH_STATE_TTL_MINUTES = 10

#: Refresh this far before the provider's stated expiry. A token that expires
#: mid-request costs a retry and an error the user sees; spending a refresh
#: slightly early costs nothing.
EXPIRY_SKEW_SECONDS = 60

_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


class IntegrationOAuthError(ValueError):
    """An authorization flow could not be started, completed, or refreshed."""


def session_binding(raw_session_token: str | None) -> str:
    if not raw_session_token:
        raise IntegrationOAuthError(
            "Connecting an account requires an active profile session."
        )
    return hashlib.sha256(raw_session_token.encode("utf-8")).hexdigest()


def _validate_redirect_uri(value: str) -> None:
    """Loopback over plain HTTP, or HTTPS anywhere else.

    Carried over unchanged from the deleted implementation. Loopback is exempt
    from the HTTPS requirement because the request never leaves the machine,
    which is the same reasoning that makes it the redirect Google recommends for
    desktop clients.
    """

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IntegrationOAuthError("Redirect URI must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise IntegrationOAuthError("Redirect URI is invalid.")
    hostname = parsed.hostname.rstrip(".").lower()
    localhost = hostname == "localhost" or hostname.endswith(".localhost")
    if parsed.scheme != "https" and not localhost:
        try:
            localhost = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            localhost = False
    if parsed.scheme != "https" and not localhost:
        raise IntegrationOAuthError("Redirect URI must use HTTPS except on loopback.")


def resolve_client(provider: Provider) -> OAuthClient | None:
    """The OAuth client to identify Neo with, or ``None`` if there is none.

    A client the user supplied through the interface wins over one the build
    shipped. That ordering is the point: somebody who has gone to the trouble of
    pasting in their own Google Cloud project means to use it, and silently
    preferring a built-in would connect them to the wrong one.

    Returning ``None`` is a supported state rather than an error -- an install
    with neither offers the setup form instead of a Connect button.
    """

    stored = store.read_oauth_client(provider.id)
    if stored is not None and stored[0]:
        return OAuthClient(client_id=stored[0], client_secret=stored[1])
    return provider.default_client()


def _client(provider: Provider) -> OAuthClient:
    client = resolve_client(provider)
    if client is None:
        raise IntegrationOAuthError(
            f"{provider.display_name} has no OAuth client configured. Add one in "
            "Connected accounts to continue."
        )
    return client


def _now() -> datetime:
    return datetime.now(UTC)


def start_oauth(
    provider: Provider,
    *,
    capability_ids: tuple[str, ...],
    session_token: str | None,
    redirect_uri: str,
    return_origin: str,
    self_origin: str | None = None,
) -> dict:
    """Mint a state and return the URL to send the user to."""

    session_hash = session_binding(session_token)
    requested = capability_vocabulary.validate(capability_ids)
    unsupported = sorted(set(requested) - set(provider.supported_capabilities))
    if unsupported:
        raise IntegrationOAuthError(
            f"{provider.display_name} does not offer '{unsupported[0]}'."
        )
    _validate_redirect_uri(redirect_uri)
    if not is_allowed_return_origin(return_origin, self_origin=self_origin):
        # Where a browser goes next, chosen by the caller: an allowlist is the
        # only thing between this and an open redirect.
        raise IntegrationOAuthError("Return origin is not one Neo will redirect to.")

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state_hash = hashlib.sha256(state.encode("ascii")).hexdigest()
    nonce, ciphertext = seal_json(
        {"verifier": verifier}, aad=scoped_aad(f"oauth-state:{state_hash}")
    )

    now = _now()
    store.delete_expired_oauth_states(now.isoformat())
    store.insert_oauth_state(
        {
            "state_hash": state_hash,
            "provider": provider.id,
            "session_hash": session_hash,
            "capabilities_json": json.dumps(list(requested)),
            "verifier_nonce": nonce,
            "verifier_ciphertext": ciphertext,
            "redirect_uri": redirect_uri,
            "return_origin": return_origin,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES)).isoformat(),
        }
    )

    client = _client(provider)
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": " ".join(provider.scopes_for(requested)),
        **provider.authorization_params(),
    }
    separator = "&" if "?" in provider.authorization_url else "?"
    return {
        "authorization_url": f"{provider.authorization_url}{separator}{urlencode(query)}",
        "expires_at": (now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES)).isoformat(),
        "provider": provider.id,
    }


def _token_request(provider: Provider, grant: dict[str, str]) -> dict:
    client = _client(provider)
    form = {**grant, "client_id": client.client_id}
    if client.client_secret:
        form["client_secret"] = client.client_secret
    payload, _ = request_json(
        "POST",
        provider.token_url,
        allowed_hosts=provider.allowed_hosts,
        form=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not payload.get("access_token"):
        # Deliberately says nothing about what the provider returned: on this
        # endpoint the response echoes the request.
        raise IntegrationOAuthError("The provider did not return an access token.")
    return payload


def _expires_at(payload: dict, *, now: datetime) -> str | None:
    try:
        seconds = max(0, int(payload.get("expires_in") or 0))
    except (TypeError, ValueError):
        seconds = 0
    return (now + timedelta(seconds=seconds)).isoformat() if seconds else None


def _granted_capabilities(
    provider: Provider, token: dict, requested: tuple[str, ...]
) -> tuple[str, ...]:
    """What the user actually approved, not what was asked for.

    The token response names the scopes that were granted. A consent screen is
    editable, so those can be fewer than the ones requested -- and recording the
    request would leave Neo claiming a permission the user had just declined.

    When a provider returns no scope list there is nothing to check against, so
    the request stands. That is the honest fallback rather than the safe-looking
    one: refusing everything would break a provider that simply does not report
    scopes, and inventing a narrower set would be a guess.
    """

    raw = token.get("scope")
    if not isinstance(raw, str) or not raw.strip():
        return requested
    return provider.capabilities_from_scopes(frozenset(raw.split()), requested)


def finish_oauth(*, state: str, code: str, session_token: str | None) -> dict:
    """Redeem a code and store the connection. Returns the connection row.

    The provider is read off the consumed state rather than taken as an argument.
    The callback URL cannot carry it -- a redirect URI is a fixed registered
    string -- and taking it from the caller would mean trusting a claim about
    which provider a state belongs to. The state knows; nothing else has to.
    """

    session_hash = session_binding(session_token)
    state_hash = hashlib.sha256(str(state).encode("ascii")).hexdigest()
    now = _now()
    record = store.consume_oauth_state(state_hash, session_hash, now.isoformat())
    if record is None:
        # Unknown, expired, replayed, or another session's: one message, because
        # distinguishing them tells a caller which guess was closest.
        raise IntegrationOAuthError(
            "This sign-in link is no longer valid. Start connecting the account again."
        )
    try:
        provider = get_provider(record["provider"])
    except UnknownProviderError:
        # A state written by a build that had a provider this one does not.
        raise IntegrationOAuthError(
            "This sign-in link is no longer valid. Start connecting the account again."
        ) from None

    sealed = open_json(
        record["verifier_nonce"],
        record["verifier_ciphertext"],
        aad=scoped_aad(f"oauth-state:{state_hash}"),
    )
    token = _token_request(
        provider,
        {
            "grant_type": "authorization_code",
            "code": str(code),
            "redirect_uri": record["redirect_uri"],
            "code_verifier": sealed["verifier"],
        },
    )

    identity_payload, _ = request_json(
        "GET",
        provider.identity_url,
        allowed_hosts=provider.allowed_hosts,
        access_token=token["access_token"],
    )
    try:
        identity = provider.parse_identity(identity_payload)
    except ValueError:
        raise IntegrationOAuthError("Could not read which account was connected.") from None

    requested = tuple(json.loads(record["capabilities_json"] or "[]"))
    granted = _granted_capabilities(provider, token, requested)
    connection = store.upsert_connection(
        connection_id=secrets.token_hex(16),
        provider=provider.id,
        account_email=identity.email,
        account_subject=identity.subject,
        capabilities=granted,
        client_mode="builtin",
    )
    store.write_credential(
        connection_id=connection["id"],
        secret={
            "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token") or "",
            "token_type": token.get("token_type") or "Bearer",
        },
        expires_at=_expires_at(token, now=now),
    )
    return connection


def _refresh_lock(connection_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        if connection_id not in _refresh_locks:
            _refresh_locks[connection_id] = threading.Lock()
        return _refresh_locks[connection_id]


def _is_stale(expires_at: str | None, *, now: datetime) -> bool:
    if not expires_at:
        return False
    try:
        moment = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    return moment <= now + timedelta(seconds=EXPIRY_SKEW_SECONDS)


def access_token_for(provider: Provider, connection_id: str) -> str:
    """A usable access token, refreshing first if the stored one is about to expire.

    The whole read-check-refresh-write cycle happens under the connection's lock,
    and the credential is re-read *inside* it: two threads that both saw an
    expired token must not both refresh from the same stale refresh token, since
    a provider that rotates them will have invalidated the first one by the time
    the second lands.
    """

    with _refresh_lock(connection_id):
        stored = store.read_credential(connection_id)
        if stored is None:
            raise IntegrationOAuthError("This account is not connected.")
        secret, updated_at, expires_at = stored
        now = _now()
        if not _is_stale(expires_at, now=now):
            return str(secret.get("access_token") or "")

        refresh_token = str(secret.get("refresh_token") or "")
        if not refresh_token:
            store.set_connection_status(
                connection_id, "needs_reauth", error_category="no_refresh_token"
            )
            raise IntegrationOAuthError(
                "This account needs to be reconnected before Neo can use it again."
            )

        token = _token_request(
            provider, {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        try:
            store.write_credential(
                connection_id=connection_id,
                secret={
                    "access_token": token["access_token"],
                    # Providers commonly omit the refresh token on a refresh,
                    # meaning "keep the one you have". Treating that as a
                    # rotation to empty would disconnect the account.
                    "refresh_token": token.get("refresh_token") or refresh_token,
                    "token_type": token.get("token_type") or "Bearer",
                },
                expires_at=_expires_at(token, now=now),
                expected_updated_at=updated_at,
            )
        except store.CredentialRaceError:
            # Another refresh landed first. Its token is the live one; ours may
            # already be void. Take theirs rather than overwriting.
            latest = store.read_credential(connection_id)
            if latest is None:
                raise IntegrationOAuthError("This account is not connected.") from None
            return str(latest[0].get("access_token") or "")
        return str(token["access_token"])


def revoke(provider: Provider, connection_id: str) -> None:
    """Tell the provider to forget the token, then forget it locally.

    Local removal happens even when the remote call fails. A user who disconnects
    an account has decided Neo should not hold their credentials, and leaving the
    row behind because a network call failed would be the wrong way to disagree.
    """

    stored = store.read_credential(connection_id)
    if stored is not None and provider.revoke_url:
        secret = stored[0]
        token = secret.get("refresh_token") or secret.get("access_token")
        if token:
            client = provider.default_client()
            form = {"token": str(token)}
            if client is not None:
                form["client_id"] = client.client_id
            try:
                request_json(
                    "POST",
                    provider.revoke_url,
                    allowed_hosts=provider.allowed_hosts,
                    form=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except Exception:
                # Already-revoked and unreachable look the same here, and neither
                # changes what happens next.
                pass
    store.delete_connection(connection_id)

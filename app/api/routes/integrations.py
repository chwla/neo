"""Connecting a real account to Neo, over HTTP.

Thin by design, like ``appearance.py``: parse, authorise, hand off. Nothing here
authenticates -- ``ProfileSessionMiddleware`` gates every ``/api`` path centrally
and binds the request to the signed-in profile's database, which is also what
scopes every connection listed below to that profile without a single WHERE
clause saying so. A connection belonging to another profile is not filtered out;
it is in a different database file.

Three things about this router are load-bearing and easy to lose:

**The callback is a gated path on purpose.** ``/api/integrations/oauth/callback``
is not in ``PUBLIC_API_PREFIXES``, so a browser arriving without a profile cookie
gets a 401 rather than a chance to complete somebody's authorization. That works
because the provider returns the user by top-level GET navigation, which
``SameSite=Lax`` permits -- and it is exactly why the flow must never be
configured with ``response_mode=form_post``, which would arrive as a POST and
carry no cookie at all.

**The redirect URI is never taken from the caller.** It comes from settings,
because it is a string registered with the provider that has to match exactly.
Accepting one from the request would make it attacker-chosen, which is the
classic way an authorization code gets delivered somewhere else.

**Failure says as little as possible.** Nothing a provider returned reaches a
response body, and the callback's error page reflects no input at all, so there
is nothing to inject into it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.api.routes.accounts import SESSION_COOKIE, session_for
from app.core.config import get_settings
from app.services.integrations import service
from app.services.integrations.capabilities import UnknownCapabilityError
from app.services.integrations.http import IntegrationHttpError
from app.services.integrations.oauth import IntegrationOAuthError
from app.services.integrations.vault import IntegrationVaultError

router = APIRouter(prefix="/integrations", tags=["integrations"])

#: What the callback appends when it sends the user back to the interface. Read
#: by the Connected Accounts screen to know it should refresh and what to say.
CONNECTED_QUERY = "integration=connected"
FAILED_QUERY = "integration=failed"


class ConnectionView(BaseModel):
    """Exactly what the browser may know about a connected account.

    Mirrors ``store.PUBLIC_CONNECTION_FIELDS``. Declared as a response model so
    FastAPI drops anything the service layer might grow later: a field has to be
    added in two places to become visible, and one of them is this file.
    """

    id: str
    provider: str
    account_email: str
    capabilities: list[str]
    status: str
    sync_enabled: bool
    expires_at: str | None = None


class ConnectionsResponse(BaseModel):
    connections: list[ConnectionView]


class StartRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    capabilities: list[str] = Field(min_length=1, max_length=32)
    #: Where to return the user afterwards. Checked against the shared origin
    #: allowlist before a state is minted; see ``app/core/origins.py``.
    return_origin: str = Field(min_length=1, max_length=2048)


class StartResponse(BaseModel):
    authorization_url: str
    expires_at: str
    provider: str


class SyncRequest(BaseModel):
    enabled: bool


class OAuthClientRequest(BaseModel):
    """An OAuth client the user registered with the provider themselves.

    ``client_secret`` is optional because a native-app client does not always
    have one, and where it does it is not confidential -- PKCE is what secures
    the flow. It is still stored sealed rather than in the clear.
    """

    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(default="", max_length=512)


def _self_origin(request: Request) -> str:
    """The API's own origin as this browser reached it.

    Taken from the live request rather than from settings because in a container
    the server binds ``0.0.0.0`` while the browser arrives at ``127.0.0.1``;
    settings would name an origin nothing actually uses.
    """

    url = request.url
    return f"{url.scheme}://{url.netloc}"


def _handle(exc: Exception) -> HTTPException:
    """Map a failure onto a status and a message that is safe to show.

    Every branch returns wording Neo chose. Nothing from a provider response,
    and nothing from an exception's own text where that text could have come
    from one, is interpolated -- see ``app/services/integrations/http.py``.
    """

    if isinstance(exc, service.IntegrationPermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, service.UnknownConnectionError):
        return HTTPException(status_code=404, detail="No such connected account.")
    if isinstance(exc, UnknownCapabilityError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, IntegrationOAuthError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, IntegrationVaultError):
        # The stored credential cannot be opened -- most often because the data
        # directory moved, which changes the AAD. Reconnecting is the fix.
        return HTTPException(
            status_code=409,
            detail="Neo could not read this account's stored credentials. Reconnect it.",
        )
    if isinstance(exc, IntegrationHttpError):
        return HTTPException(status_code=502, detail=str(exc))
    raise exc


@router.get("/catalog")
def catalog() -> dict:
    return service.catalog()


@router.get("/connections", response_model=ConnectionsResponse)
def connections() -> ConnectionsResponse:
    return ConnectionsResponse(connections=service.connections())


@router.post("/oauth/start", response_model=StartResponse)
def start(payload: StartRequest, request: Request) -> StartResponse:
    try:
        result = service.begin_connection(
            provider_id=payload.provider,
            capability_ids=tuple(payload.capabilities),
            session=session_for(request),
            session_token=request.cookies.get(SESSION_COOKIE),
            redirect_uri=get_settings().integration_oauth_redirect_uri,
            return_origin=payload.return_origin,
            self_origin=_self_origin(request),
        )
    except Exception as exc:
        raise _handle(exc) from None
    return StartResponse(**result)


def _callback_error_page(message: str) -> HTMLResponse:
    """A dead end that reflects nothing.

    The user is sent here only when there is no trustworthy origin to return
    them to -- which is precisely when a redirect would be the dangerous thing
    to do. ``message`` is always one of this module's own constants; no request
    value reaches the markup.
    """

    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<title>Neo</title>"
        "<body style='font:14px system-ui;padding:2rem;max-width:34rem'>"
        f"<h1 style='font-size:1.1rem'>{message}</h1>"
        "<p>You can close this tab and try connecting the account again from Neo.</p>",
        status_code=400,
    )


# ``response_model=None``: this route returns a redirect or an error page, and
# FastAPI would otherwise try to build a response schema from that union.
@router.get("/oauth/callback", response_model=None)
def callback(request: Request) -> RedirectResponse | HTMLResponse:
    """Where the provider returns the user.

    Note what is absent: no logging of ``code`` or ``state``, and no echo of
    either into the response. The query string of this one request is the most
    sensitive URL in the flow.
    """

    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    denied = request.query_params.get("error")

    if not state:
        return _callback_error_page("That sign-in link was incomplete.")

    return_origin = service.peek_return_origin(state)

    if denied or not code:
        # The user pressed Cancel, or the provider returned no code. Burn the
        # state either way so an abandoned attempt cannot be resumed later.
        service.abandon_connection(state, session_token=request.cookies.get(SESSION_COOKIE))
        if return_origin:
            return RedirectResponse(f"{return_origin}/?{FAILED_QUERY}", status_code=303)
        return _callback_error_page("That account was not connected.")

    try:
        service.complete_connection(
            state=state,
            code=code,
            session=session_for(request),
            session_token=request.cookies.get(SESSION_COOKIE),
        )
    except Exception:
        # Deliberately broad and deliberately silent: whatever went wrong, the
        # useful thing for the user is the interface they started from, and the
        # detail could have come from the provider.
        if return_origin:
            return RedirectResponse(f"{return_origin}/?{FAILED_QUERY}", status_code=303)
        return _callback_error_page("That account could not be connected.")

    if return_origin:
        return RedirectResponse(f"{return_origin}/?{CONNECTED_QUERY}", status_code=303)
    return _callback_error_page("That account is connected, but Neo lost track of this tab.")


@router.put("/providers/{provider_id}/client")
def set_client(provider_id: str, payload: OAuthClientRequest, request: Request) -> dict:
    """Register an OAuth client from inside the app.

    Neo is installed rather than hosted, so somebody has to supply the client it
    identifies itself with. Doing it here rather than through a file and a
    restart is the difference between a setting and a chore.
    """

    try:
        return service.set_oauth_client(
            provider_id=provider_id,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            session=session_for(request),
        )
    except ValueError as exc:
        # Field-level complaints ("that has a space in it") are the user's own
        # text, not a provider's, so they are safe to show verbatim.
        if isinstance(exc, service.UnknownConnectionError):
            raise _handle(exc) from None
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise _handle(exc) from None


@router.delete("/providers/{provider_id}/client")
def clear_client(provider_id: str, request: Request) -> dict:
    try:
        return service.clear_oauth_client(
            provider_id=provider_id, session=session_for(request)
        )
    except Exception as exc:
        raise _handle(exc) from None


@router.post("/connections/{connection_id}/sync", response_model=ConnectionView)
def set_sync(connection_id: str, payload: SyncRequest) -> ConnectionView:
    try:
        return ConnectionView(**service.set_sync_enabled(connection_id, payload.enabled))
    except Exception as exc:
        raise _handle(exc) from None


@router.delete("/connections/{connection_id}")
def disconnect(connection_id: str) -> dict:
    try:
        service.disconnect(connection_id)
    except Exception as exc:
        raise _handle(exc) from None
    return {"disconnected": True}

"""The one place a credential is attached to an outbound request.

Everything the integration layer sends goes through here, and nothing else in
``app/services/integrations`` or ``app/services/google`` imports an HTTP client.
That is enforced by a test, and the reason is narrow and specific.

``ToolRegistry.execute`` turns *any* exception a tool raises into
``content=f"{type(exc).__name__}: {exc}"``. That string is written to
``workspace_agent_tool_calls`` in the profile database, replayed into the model's
transcript, and streamed to the browser. So a single exception that carries a
token leaks it to disk, to the model, and to the screen in one step -- and
``requests`` puts the request URL into the message of anything raised by
``raise_for_status``.

Hence the rule this module exists to keep: **an error leaving here is built from
a fixed table of messages, never from a response body, a header, or a URL.**
``IntegrationHttpError`` carries a category and a status code, both safe to show,
and nothing else. Callers wanting to explain a failure to a user pick words from
the category; they never interpolate the cause.

The second job is the host allowlist. Every URL the integration layer sends to is
a constant declared on a provider, so membership can be exact rather than a
guess about whether an address looks internal. A general "is this URL safe"
validator would be both larger and weaker: it has to permit the whole public
internet, while this permits seven hostnames.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import requests

_LOG = logging.getLogger("neo.integrations.http")

#: Long enough for a slow token exchange, short enough that a wedged provider
#: cannot pin a sweep thread for minutes. Applies to connect and read alike.
DEFAULT_TIMEOUT_SECONDS = 30

#: Every message a failure can carry. Chosen so the category alone tells the
#: caller what to do -- reconnect, back off, or give up -- without any detail
#: from the provider's response reaching a log, a model, or a screen.
_MESSAGES: dict[str, str] = {
    "blocked_host": "Refused a request to a host this provider is not allowed to use.",
    "timeout": "The provider did not respond in time.",
    "unreachable": "Could not reach the provider.",
    "unauthorized": "The provider rejected the stored credentials.",
    "forbidden": "The provider refused this request for this account.",
    "not_found": "The provider has no such item.",
    "conflict": "The provider already has this item.",
    "precondition_failed": "The item changed at the provider since it was last read.",
    "rate_limited": "The provider is asking Neo to slow down.",
    "provider_error": "The provider reported an error.",
    "invalid_response": "The provider returned a response Neo could not read.",
}

_STATUS_CATEGORIES: dict[int, str] = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    410: "not_found",
    412: "precondition_failed",
    429: "rate_limited",
}


class IntegrationHttpError(RuntimeError):
    """A request failed. Carries a category and a status, and nothing else.

    Deliberately not a subclass of anything ``requests`` raises: catching this
    must not accidentally catch something that still has a URL in its message.
    """

    def __init__(self, category: str, status: int | None = None) -> None:
        self.category = category if category in _MESSAGES else "provider_error"
        self.status = status
        super().__init__(_MESSAGES[self.category])

    @property
    def retryable(self) -> bool:
        """Whether waiting and trying again could plausibly work."""

        return self.category in {"timeout", "unreachable", "rate_limited", "provider_error"}


def _category_for_status(status: int) -> str:
    if status in _STATUS_CATEGORIES:
        return _STATUS_CATEGORIES[status]
    return "provider_error"


def assert_host_allowed(url: str, *, allowed_hosts: frozenset[str]) -> str:
    """Return the host, having checked it is one this provider may be reached on."""

    host = (urlparse(url).hostname or "").strip().rstrip(".").lower()
    if host not in allowed_hosts:
        _LOG.warning("integration_http_blocked_host host=%s", host or "(none)")
        raise IntegrationHttpError("blocked_host")
    return host


def request_json(
    method: str,
    url: str,
    *,
    allowed_hosts: frozenset[str],
    access_token: str | None = None,
    params: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    json_body: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Send one request and return ``(parsed_json, response_headers)``.

    Every failure path converges on ``IntegrationHttpError``. Nothing that
    ``requests`` raises is allowed to escape, because those messages contain the
    URL -- which for a token exchange is the one URL whose query string may hold
    an authorization code.
    """

    host = assert_host_allowed(url, allowed_hosts=allowed_hosts)
    sent = {"Accept": "application/json", **(headers or {})}
    if access_token:
        sent["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.request(
            method.upper(),
            url,
            params=params,
            data=form,
            json=json_body,
            headers=sent,
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise IntegrationHttpError("timeout") from _scrub(exc)
    except requests.RequestException as exc:
        raise IntegrationHttpError("unreachable") from _scrub(exc)

    if response.status_code >= 400:
        category = _category_for_status(response.status_code)
        # Host and status only. The body is exactly what must not be recorded:
        # for a token endpoint it can echo the request, and for a mail endpoint
        # it can contain the message.
        _LOG.warning(
            "integration_http_failed host=%s status=%s category=%s",
            host,
            response.status_code,
            category,
        )
        raise IntegrationHttpError(category, status=response.status_code)

    if not response.content:
        return {}, dict(response.headers)
    try:
        payload = response.json()
    except ValueError as exc:
        raise IntegrationHttpError("invalid_response", status=response.status_code) from _scrub(exc)
    if not isinstance(payload, dict):
        raise IntegrationHttpError("invalid_response", status=response.status_code)
    return payload, dict(response.headers)


def _scrub(exc: BaseException) -> BaseException:
    """Strip an exception's own message before it becomes a ``__cause__``.

    A chained cause is still printed by ``traceback`` and still reaches any log
    configured with ``exc_info``. Keeping the type -- which is genuinely useful
    when debugging -- while dropping the arguments is what makes ``raise ... from``
    safe here.
    """

    try:
        exc.args = ()
    except Exception:
        return exc
    return exc

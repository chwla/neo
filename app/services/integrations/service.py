"""Orchestration for connected accounts: the layer the HTTP routes talk to.

Everything above this line is provider-agnostic. The routes name a provider by
id and never import one, so adding Microsoft means adding a provider module and
nothing else. Everything below is split the way the rest of the package is:
``oauth.py`` runs the authorization dance, ``store.py`` persists, this module
decides *whether the caller may* and assembles what the browser sees.

Two rules live here because they are authorization rather than mechanism, and
authorization belongs where the caller is known:

**Guests may not connect accounts.** A guest profile is explicitly temporary --
its directory is deleted when the session or the application ends. A refresh
token is the opposite: a long-lived credential for somebody's real mailbox.
Putting one inside a directory whose whole contract is "this goes away" means
that if cleanup ever fails, or a guest profile is recovered from a backup, a
live credential outlives the session that was told it would not. Refusing is
cheaper than guaranteeing the cleanup.

**A capability is checked before a request is built, not after it fails.**
``authorized_access_token`` is the only way a caller obtains a token, and it will
not hand one out for a capability the user did not grant. That is deliberately
earlier than the provider's own scope check: a 403 from Google is an answer that
has already cost a network round trip and arrives as an error the agent has to
interpret, whereas this is a refusal Neo can explain in the user's own words and
point at the toggle that would fix it.

Nothing here returns a credential. ``store.public_connection`` builds every
outbound shape from an allowlist, so a column added to the connections table
later cannot reach the browser by existing.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.origins import is_allowed_return_origin
from app.services.integrations import capabilities as capability_vocabulary
from app.services.integrations import oauth, store
from app.services.integrations.providers.base import (
    Provider,
    UnknownProviderError,
    get_provider,
    list_providers,
)


class IntegrationPermissionError(PermissionError):
    """The caller may not do this, regardless of whether it would work."""


class UnknownConnectionError(LookupError):
    """No connection with that id exists in this profile."""


def ensure_not_guest(session: dict | None) -> None:
    """Refuse a guest session before any state is minted.

    Checked at the start of the flow rather than at the callback: a guest who
    got as far as Google's consent screen and was refused afterwards would have
    already authorised Neo at the provider, leaving a grant behind with nothing
    on this side to revoke it.
    """

    if session is None:
        raise IntegrationPermissionError("Connecting an account requires a signed-in profile.")
    if session.get("is_guest"):
        raise IntegrationPermissionError(
            "Guest profiles cannot connect accounts, because a guest profile and "
            "everything in it is deleted when the session ends. Create a profile to "
            "connect an account."
        )


def provider_for(provider_id: str) -> Provider:
    try:
        return get_provider(provider_id)
    except UnknownProviderError as exc:
        raise UnknownConnectionError(str(exc)) from exc


def _capability_view(capability_id: str, provider: Provider) -> dict:
    """One capability as the settings screen renders it.

    ``tier`` is the provider's own classification of the scopes behind it, not a
    property of the capability: the same question ("read your mail") can be a
    routine grant on one provider and a reviewed one on another.
    """

    capability = capability_vocabulary.get(capability_id)
    return {
        "id": capability.id,
        "label": capability.label,
        "description": capability.description,
        "grade": capability.grade,
        "tier": provider.tier_for(capability_id),
    }


def catalog() -> dict:
    """What the settings screen renders before anything is connected.

    ``connectable`` is the honest answer to "can I press Connect": false when no
    built-in OAuth client is configured, which is a supported state rather than
    an error -- a fork, or an install predating client registration, offers the
    bring-your-own path instead. Reported rather than inferred so the interface
    can say why the button is absent.
    """

    providers = []
    for provider in list_providers():
        stored = store.oauth_client_summary(provider.id)
        client = oauth.resolve_client(provider)
        providers.append(
            {
                "id": provider.id,
                "display_name": provider.display_name,
                "connectable": client is not None,
                # Which of the two supplied it, so the interface can say "using
                # your own client" rather than leaving the user guessing whether
                # the thing they pasted in took effect.
                "client_mode": "byo" if stored else ("builtin" if client else "none"),
                "client_id": stored["client_id"] if stored else "",
                # The redirect URI the user has to register with the provider.
                # Reported rather than documented: it has to match exactly, and a
                # value you can copy beats one you retype from a README.
                "redirect_uri": get_settings().integration_oauth_redirect_uri,
                "capabilities": [
                    _capability_view(capability_id, provider)
                    for capability_id in sorted(provider.supported_capabilities)
                ],
            }
        )
    return {"providers": providers}


def connections() -> list[dict]:
    """Every connected account in this profile, in the shape the browser sees."""

    return [
        store.public_connection(row, expires_at=store.credential_expiry(row["id"]))
        for row in store.list_connections()
    ]


def get_connection_or_raise(connection_id: str) -> dict:
    """Fetch a connection, scoped to the active profile by the database itself.

    There is no owner column to check. The request is already bound to one
    profile's database by ``ProfileSessionMiddleware``, so a connection id from
    another profile simply is not here -- which is a stronger guarantee than a
    predicate somebody could forget to write.
    """

    row = store.get_connection(connection_id)
    if row is None:
        raise UnknownConnectionError("No such connected account.")
    return row


def begin_connection(
    *,
    provider_id: str,
    capability_ids: tuple[str, ...],
    session: dict | None,
    session_token: str | None,
    redirect_uri: str,
    return_origin: str,
    self_origin: str | None,
) -> dict:
    ensure_not_guest(session)
    provider = provider_for(provider_id)
    return oauth.start_oauth(
        provider,
        capability_ids=capability_ids,
        session_token=session_token,
        redirect_uri=redirect_uri,
        return_origin=return_origin,
        self_origin=self_origin,
    )


def complete_connection(
    *, state: str, code: str, session: dict | None, session_token: str | None
) -> dict:
    ensure_not_guest(session)
    connection = oauth.finish_oauth(state=state, code=code, session_token=session_token)
    return store.public_connection(
        connection, expires_at=store.credential_expiry(connection["id"])
    )


def disconnect(connection_id: str) -> None:
    row = get_connection_or_raise(connection_id)
    oauth.revoke(provider_for(row["provider"]), connection_id)


def set_sync_enabled(connection_id: str, enabled: bool) -> dict:
    get_connection_or_raise(connection_id)
    store.set_sync_enabled(connection_id, enabled)
    row = get_connection_or_raise(connection_id)
    return store.public_connection(row, expires_at=store.credential_expiry(connection_id))


def authorized_access_token(connection_id: str, capability_id: str) -> str:
    """The only way to obtain a token, and only for a granted capability.

    Callers pass the capability the *call* needs, not the one the connection has,
    so a tool cannot widen its own reach by reading mail with a calendar grant.
    """

    row = get_connection_or_raise(connection_id)
    if capability_id not in (row.get("capabilities") or ()):
        capability = capability_vocabulary.get(capability_id)
        raise IntegrationPermissionError(
            f"This account has not been given permission to {capability.label.lower()}. "
            "Turn it on in Connected Accounts to continue."
        )
    return oauth.access_token_for(provider_for(row["provider"]), connection_id)


def peek_return_origin(state: str) -> str | None:
    """Where a flow said it wanted the user returned, if that is still allowed.

    Re-validated here rather than trusted because it was validated when the
    state was written. The value has been sitting in a database in between, and
    a redirect target is the one field where "it was safe when we stored it" is
    not an argument worth making. Anything that does not pass now yields
    ``None``, which callers turn into a dead-end page rather than a redirect.
    """

    if not state:
        return None
    record = store.get_oauth_state(hashlib.sha256(str(state).encode("ascii")).hexdigest())
    if record is None:
        return None
    candidate = record.get("return_origin")
    return candidate if is_allowed_return_origin(candidate) else None


def abandon_connection(state: str, *, session_token: str | None) -> None:
    """Burn a state the user walked away from.

    A cancelled consent screen leaves a usable state behind otherwise, and a
    state is only single-use once something has used it. Failure is ignored on
    purpose: every reason this can fail -- already used, expired, never existed,
    another session's -- ends with the state being unusable, which is the whole
    intent.
    """

    if not state or not session_token:
        return
    try:
        store.consume_oauth_state(
            hashlib.sha256(str(state).encode("ascii")).hexdigest(),
            hashlib.sha256(session_token.encode("utf-8")).hexdigest(),
            datetime.now(UTC).isoformat(),
        )
    except Exception:
        return


#: A client identifier is pasted in by hand, so it is worth refusing the obvious
#: mistakes early: a blank field, something implausibly long, or a value that
#: still has whitespace in it from a bad copy.
MAX_CLIENT_FIELD = 512


def _clean_client_field(value: str, *, label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > MAX_CLIENT_FIELD:
        raise ValueError(f"{label} is too long to be valid.")
    if any(character.isspace() for character in cleaned):
        raise ValueError(f"{label} should not contain spaces. Check what was pasted.")
    return cleaned


def set_oauth_client(
    *, provider_id: str, client_id: str, client_secret: str, session: dict | None
) -> dict:
    """Store an OAuth client the user supplied.

    Guest-refused for the same reason connecting is: the credentials would live
    in a directory that is deleted when the session ends, so the setup would
    silently undo itself.
    """

    ensure_not_guest(session)
    provider = provider_for(provider_id)
    store.write_oauth_client(
        provider.id,
        client_id=_clean_client_field(client_id, label="Client ID"),
        # A secret is optional: not every provider issues one for a native
        # client, and PKCE is what secures the flow either way.
        client_secret=(client_secret or "").strip(),
    )
    return catalog()


def clear_oauth_client(*, provider_id: str, session: dict | None) -> dict:
    """Forget a stored client.

    Existing connections are deliberately left alone. Their tokens were issued
    to the old client and will fail on the next refresh, at which point they
    report themselves as needing reconnection -- which is more honest than
    deleting accounts the user did not ask to disconnect.
    """

    ensure_not_guest(session)
    provider = provider_for(provider_id)
    store.delete_oauth_client(provider.id)
    return catalog()

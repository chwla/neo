"""Google, declared: endpoints, scopes, and the quirks of its authorization call.

Every Google-specific string in Neo lives in this file. A test asserts that
``googleapis.com`` appears nowhere else under the integrations packages, because
the moment a scope or a URL leaks upward the provider boundary stops being real
and the second provider becomes a rewrite instead of a new file.

**On scope classification.** The tiers below decide what Neo may offer before its
OAuth client is verified, so they are recorded the way ``docs/external-agents``
records a CLI capability: as a measurement with a date, not as a belief. They
were read from Google's published Gmail API scope documentation in September
2026. Two of them are load-bearing and surprising:

* ``gmail.send`` is *sensitive*, not restricted -- it needs verification but no
  third-party security assessment. That is what makes a compose-only phase
  shippable well before anything that reads mail.
* every Gmail read scope is *restricted*, including ``gmail.metadata``. There is
  no way to look at a mailbox on a lesser grade, so triage cannot be built
  around a cheaper scope; it waits for verification instead.

``drive.file`` is deliberately chosen over ``drive`` or ``drive.readonly``. It is
non-sensitive because it grants access only to files the user picked or Neo
created, which is also exactly the access Neo wants -- the broader scopes would
hand over the user's entire Drive to gain nothing.

A claim here whose evidence is older than the installed client is a claim to
re-check, not one to trust.
"""

from __future__ import annotations

from app.core.config import get_base_settings
from app.services.integrations.providers.base import AccountIdentity, OAuthClient

_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_REVOKE = "https://oauth2.googleapis.com/revoke"
_IDENTITY = "https://openidconnect.googleapis.com/v1/userinfo"

#: Asked for on every connection so Neo can name the account it just linked, and
#: so a second connection to the same mailbox updates the first rather than
#: silently creating a duplicate. Neither is sensitive.
_BASE_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)

_CAPABILITY_SCOPES: dict[str, tuple[str, ...]] = {
    "calendar.read": ("https://www.googleapis.com/auth/calendar.readonly",),
    "calendar.write": ("https://www.googleapis.com/auth/calendar.events",),
    "docs.read": (
        "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/drive.file",
    ),
    "docs.write": (
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.file",
    ),
    "mail.read": ("https://www.googleapis.com/auth/gmail.readonly",),
    "mail.send": ("https://www.googleapis.com/auth/gmail.send",),
    "mail.organize": ("https://www.googleapis.com/auth/gmail.modify",),
}

#: Google's own classification, read 2026-09. See the module docstring.
_CAPABILITY_TIERS: dict[str, str] = {
    "calendar.read": "sensitive",
    "calendar.write": "sensitive",
    "docs.read": "sensitive",
    "docs.write": "sensitive",
    "mail.read": "restricted",
    "mail.send": "sensitive",
    "mail.organize": "restricted",
}


class GoogleProvider:
    id = "google"
    display_name = "Google"
    authorization_url = _AUTH
    token_url = _TOKEN
    revoke_url = _REVOKE
    identity_url = _IDENTITY
    allowed_hosts = frozenset(
        {
            "accounts.google.com",
            "oauth2.googleapis.com",
            "openidconnect.googleapis.com",
            "www.googleapis.com",
            "gmail.googleapis.com",
            "calendar.googleapis.com",
            "docs.googleapis.com",
        }
    )
    supported_capabilities = frozenset(_CAPABILITY_SCOPES)

    def scopes_for(self, capability_ids: tuple[str, ...]) -> list[str]:
        """The scopes to request for a capability set, base scopes included.

        Sorted and de-duplicated because ``docs.read`` and ``docs.write`` share
        ``drive.file``: asking for it twice is not wrong, but a stable list makes
        the request comparable in a test and readable in a log.
        """

        scopes = set(_BASE_SCOPES)
        for capability_id in capability_ids:
            scopes.update(_CAPABILITY_SCOPES.get(capability_id, ()))
        return sorted(scopes)

    def capabilities_from_scopes(
        self, granted_scopes: frozenset[str], requested: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Narrow a requested capability set to what the user actually approved.

        Consent screens are editable: someone can be asked for calendar and mail
        and hand over only calendar. Recording what was *asked for* would leave
        Neo telling the user it has a permission they explicitly withheld, and
        the tools that permission gates would fail at the provider instead of
        being refused here with an explanation.

        A capability counts as granted only when every scope behind it is
        present -- a partial grant is not a lesser version of the capability, it
        is a capability that does not work.
        """

        return tuple(
            capability_id
            for capability_id in requested
            if set(_CAPABILITY_SCOPES.get(capability_id, ())) <= granted_scopes
            and capability_id in _CAPABILITY_SCOPES
        )

    def authorization_params(self) -> dict[str, str]:
        """The three parameters Google needs beyond the standard ones.

        ``access_type=offline`` is what produces a refresh token at all; without
        it Neo would lose the account an hour later. ``prompt=consent`` is needed
        because Google omits the refresh token on a re-authorisation that the
        user has already granted, which would otherwise leave a reconnect
        succeeding with no way to refresh. ``include_granted_scopes`` is what
        makes capabilities incremental -- granting mail later keeps calendar
        rather than replacing it.
        """

        return {
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }

    def default_client(self) -> OAuthClient | None:
        """Neo's own shipped client, or ``None`` when none is configured.

        Returning ``None`` is a supported state, not an error: a fork, or an
        install predating client registration, has no built-in client, and the
        settings screen offers the bring-your-own path instead of a Connect
        button. That mirrors how voice reports itself unavailable rather than
        erroring when its optional extra is missing.
        """

        settings = get_base_settings()
        client_id = (settings.google_oauth_client_id or "").strip()
        if not client_id:
            return None
        return OAuthClient(
            client_id=client_id,
            client_secret=(settings.google_oauth_client_secret or "").strip(),
        )

    def parse_identity(self, payload: dict) -> AccountIdentity:
        email = str((payload or {}).get("email") or "").strip()
        subject = str((payload or {}).get("sub") or "").strip()
        if not email or not subject:
            raise ValueError("Google identity response did not name an account.")
        return AccountIdentity(email=email, subject=subject)

    def tier_for(self, capability_id: str) -> str:
        return _CAPABILITY_TIERS.get(capability_id, "restricted")


PROVIDER = GoogleProvider()

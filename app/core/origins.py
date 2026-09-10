"""The browser origins Neo is willing to talk to, in one place.

Two features read this list and they must never drift apart. CORS decides which
origin may call the API with the profile cookie attached. The OAuth callback
decides which origin it may send a user back to once the provider returns them.
An origin that is safe for one is exactly the origin that is safe for the other,
so a second copy of the list would eventually disagree with the first -- and the
two ways it could disagree are "the dev server stops working" and "Neo has an
open redirect".

Membership is always an exact string match. Prefix or suffix matching would
accept ``http://127.0.0.1:5173.evil.com``, which *contains* an allowed origin
without being one.
"""

from __future__ import annotations

#: The Vite dev server (5173) and its preview server (4173), on both spellings of
#: loopback. These are cross-origin to the API and so need a CORS entry.
#:
#: A production install serves the SPA from the API's own origin, which needs no
#: CORS entry at all; ``is_allowed_return_origin`` covers that case separately
#: rather than guessing the deployed host and port here.
ALLOWED_BROWSER_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)


def _normalize(origin: str | None) -> str:
    """An origin has no trailing slash; ``urljoin`` and friends readily add one."""

    return (origin or "").strip().rstrip("/")


def is_allowed_return_origin(candidate: str | None, *, self_origin: str | None = None) -> bool:
    """Whether the OAuth callback may redirect a user back to ``candidate``.

    ``self_origin`` is the API's own origin as the browser reached it, which the
    caller reads from the live request rather than from settings -- in Docker the
    server binds ``0.0.0.0`` but the browser arrives at ``127.0.0.1``, so settings
    would name an origin no browser ever uses.

    Anything not on the list is refused. This function never falls back to "looks
    local enough": a redirect target is attacker-supplied until proven otherwise.
    """

    value = _normalize(candidate)
    if not value:
        return False
    if value in ALLOWED_BROWSER_ORIGINS:
        return True
    return bool(self_origin) and value == _normalize(self_origin)

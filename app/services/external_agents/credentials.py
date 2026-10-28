"""Giving an engine just enough credential to work, and nothing more.

The sandbox denies a run everything that is not its own, and that includes the
user's keychain and the CLIs' credential files. An engine started that way reports
"Not logged in", which is correct and useless -- so Neo has to hand it a credential
deliberately. The question is which one.

What Neo hands over is the **access token only**. Measured on this machine:

* Claude Code accepts ``CLAUDE_CODE_OAUTH_TOKEN`` and then reports
  ``loggedIn: true, authMethod: oauth_token`` with the keychain denied outright.
* Codex reads ``auth.json`` from ``CODEX_HOME``, and that file works without its
  ``refresh_token`` field.

The refresh token is deliberately withheld. An access token expires in hours and is
scoped to what the engine already does on the user's behalf; a refresh token mints
new ones indefinitely, so handing one to a process running arbitrary code would turn
one compromised run into a standing hold on the account. Keeping it back is the
difference between lending a key and copying it.

Neo also refuses to start a run that cannot finish inside the credential's lifetime,
and refreshes beforehand where the CLI can do that for itself -- unsandboxed, with no
prompt and no workspace, which is the same thing ``detect`` already does to read a
version string.

Nothing here is written to Neo's storage. The token lives in the child's environment
for the length of one run.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: Refuse a run that could not finish before the credential expires. A run cut off
#: mid-edit is worse than one that never started.
MIN_REMAINING_SECONDS = 10 * 60

#: Claude Code's keychain service and the field inside it, from its own strings.
_CLAUDE_SERVICE = "Claude Code-credentials"


class CredentialUnavailable(RuntimeError):
    """No usable credential, with a message naming what the user should do."""


@dataclass
class Credential:
    """What one run gets, and for how long."""

    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    expires_at: float | None = None

    def remaining(self, now: float | None = None) -> float | None:
        if self.expires_at is None:
            return None
        return self.expires_at - (now if now is not None else time.time())


def _keychain_secret(service: str, account: str) -> str | None:
    """Read one generic password. Runs unsandboxed, in Neo's own process."""

    try:
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", account, "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.info("keychain_read_failed service=%s err=%s", service, exc)
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _refresh_claude(binary: str) -> None:
    """Let the CLI refresh its own token, unsandboxed and without a workspace.

    ``auth status`` is a credential-only command: no prompt, no repository, no
    network access to anything but the vendor. The CLI refreshes when the token is
    within five minutes of expiry (its own threshold, read from the binary), so this
    is how Neo gets a fresh one without ever holding the refresh token itself.
    """

    try:
        subprocess.run(
            [binary, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.info("claude_refresh_attempt_failed err=%s", exc)


def _claude(binary: str, *, now: float | None = None) -> Credential:
    account = os.environ.get("USER") or "claude-code-user"
    raw = _keychain_secret(_CLAUDE_SERVICE, account)
    if not raw:
        raise CredentialUnavailable(
            "Claude Code is not signed in on this machine. Run `claude auth login` in "
            "a terminal, then try again."
        )
    try:
        oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {}
    except ValueError as exc:
        raise CredentialUnavailable(
            "Claude Code's stored credential could not be read. Run `claude auth login` "
            "in a terminal to refresh it."
        ) from exc

    token = oauth.get("accessToken")
    expires_ms = oauth.get("expiresAt")
    expires_at = float(expires_ms) / 1000.0 if expires_ms else None
    clock = now if now is not None else time.time()

    if token and expires_at and expires_at - clock < MIN_REMAINING_SECONDS:
        # Too little left to run with. Ask the CLI to refresh, then re-read.
        _refresh_claude(binary)
        raw = _keychain_secret(_CLAUDE_SERVICE, account) or raw
        try:
            oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {}
        except ValueError:
            oauth = {}
        token = oauth.get("accessToken")
        expires_ms = oauth.get("expiresAt")
        expires_at = float(expires_ms) / 1000.0 if expires_ms else None

    if not token:
        raise CredentialUnavailable(
            "Claude Code's stored credential has no usable access token. Run "
            "`claude auth login` in a terminal, then try again."
        )
    if expires_at and expires_at - clock < MIN_REMAINING_SECONDS:
        raise CredentialUnavailable(
            "Claude Code's session is about to expire and could not be refreshed. Run "
            "`claude auth status` in a terminal, then try again."
        )
    # Only the access token. The refresh token stays in the keychain, which the run
    # cannot read.
    return Credential(env={"CLAUDE_CODE_OAUTH_TOKEN": token}, expires_at=expires_at)


def _jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return float(claims["exp"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _codex(home: str, *, now: float | None = None) -> Credential:
    """Codex's ``auth.json``, minus the refresh token, for a per-run home."""

    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    try:
        stored = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialUnavailable(
            "Codex is not signed in on this machine. Run `codex login` in a terminal, "
            "then try again."
        ) from exc

    tokens = dict(stored.get("tokens") or {})
    access = tokens.get("access_token")
    if not access and not stored.get("OPENAI_API_KEY"):
        raise CredentialUnavailable(
            "Codex's stored credential has no usable token. Run `codex login` in a "
            "terminal, then try again."
        )

    # Measured: Codex refreshes at startup and its stored id_token is routinely
    # expired, so an access-token-only credential is refused by the API with a 401.
    # Making it work means putting the refresh token where the run can read it, and
    # a refresh token is indefinite account access. Refused unless the user has
    # explicitly accepted that, which is a decision about their own account.
    from app.core.config import get_settings

    if not bool(getattr(get_settings(), "codex_allow_refresh_token_handoff", False)):
        raise CredentialUnavailable(
            "Codex cannot be run with a scoped credential. Unlike Claude Code it "
            "authenticates by refreshing, so it needs its refresh token -- which "
            "would let a compromised run keep access to your OpenAI account until "
            "you revoked it by hand. Neo refuses by default. Set "
            "NEO_CODEX_ALLOW_REFRESH_TOKEN_HANDOFF=true if you accept that risk."
        )
    reduced = {**stored, "tokens": tokens}
    expires_at = _jwt_expiry(access) if access else None
    clock = now if now is not None else time.time()
    if expires_at and expires_at - clock < MIN_REMAINING_SECONDS:
        raise CredentialUnavailable(
            "Codex's session has expired. Run `codex login` in a terminal, then try again."
        )
    return Credential(
        files={str(Path(home) / "auth.json"): json.dumps(reduced)},
        env={"CODEX_HOME": str(home)},
        expires_at=expires_at,
    )


def for_executor(executor: str, *, binary: str, home: str | None = None) -> Credential:
    """The credential one run of ``executor`` gets.

    An engine with no handoff implemented returns an empty credential, which means
    "run with nothing" -- and such a run will report itself signed out rather than
    silently using the user's own credential store.
    """

    if executor == "claude_code":
        return _claude(binary)
    if executor == "codex":
        if not home:
            raise CredentialUnavailable("Codex needs a per-run home directory.")
        return _codex(home)
    return Credential()


def write_files(credential: Credential) -> None:
    """Materialise a credential's files, owner-readable only."""

    for path, contents in credential.files.items():
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, contents.encode("utf-8"))
        finally:
            os.close(fd)


__all__ = [
    "MIN_REMAINING_SECONDS",
    "Credential",
    "CredentialUnavailable",
    "for_executor",
    "write_files",
]

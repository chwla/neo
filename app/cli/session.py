"""Where the CLI keeps its sign-in.

Every API outside the profile picker and the health probes needs a profile
session, which the browser holds as a cookie. The CLI holds the same cookie, and
the CSRF value that goes with it, in one small file under the user's own
configuration directory, readable by that user only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.core.paths import restrict_to_owner

FILE_NAME = "cli-session.json"


def session_path() -> Path:
    override = os.environ.get("NEO_CLI_SESSION_FILE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "neo" / FILE_NAME


def load(api_url: str) -> dict | None:
    """The stored session for ``api_url``, or ``None`` when not signed in there."""

    path = session_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("api_url") != api_url.rstrip("/"):
        return None
    if not data.get("session"):
        return None
    return data


def save(api_url: str, *, session: str, csrf: str, profile: dict) -> Path:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        restrict_to_owner(path.parent)
    except OSError:
        pass
    payload = {
        "api_url": api_url.rstrip("/"),
        "session": session,
        "csrf": csrf,
        "profile": {"id": profile.get("id"), "username": profile.get("username")},
    }
    # Created with owner-only permission before anything is written into it.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    try:
        restrict_to_owner(path)
    except OSError:
        pass
    return path


def clear() -> None:
    try:
        session_path().unlink()
    except FileNotFoundError:
        pass

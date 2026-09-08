"""How much of each engine's subscription window is spent, insofar as it is knowable.

Neither CLI has a usage subcommand. ``claude auth status --json`` answers who you
are and says nothing about limits; ``codex login status`` prints one line of prose.
So the question "how much have I got left?" cannot be *asked* of either binary
without either an interactive session or the account credentials -- and Neo does not
read those. The boundary in ``env`` is the whole reason this module exists in the
shape it does: it never opens ``~/.claude/.credentials.json`` or ``~/.codex/auth.json``,
never mints a token, and never calls a vendor API.

What it reads instead is what each CLI has already written down for itself, the same
move ``models`` makes with Codex's ``models_cache.json``:

* **Claude Code's own usage cache** -- ``$HOME/.claude.json`` holds
  ``cachedUsageUtilization``, the CLI's stored copy of its ``/api/oauth/usage``
  reply, rewritten whenever it runs. This is the same number ``/usage`` prints.
* **Claude Code's rate-limit notice** -- ``stream-json`` emits ``rate_limit_event``
  mid-run and Neo already records it on the session row. During and just after a run
  it is fresher than the cache, so whichever was observed later wins.
* **Codex's session logs** -- ``codex exec`` writes a rollout under
  ``sessions/YYYY/MM/DD/`` for every run, Neo's own included, and each carries
  ``token_count`` records with the ``rate_limits`` the service returned. Codex's
  ``exec --json`` stream does *not* carry them, so this file is the only place a
  Codex number can be had without credentials.

Every source is a cache or a log, which means every figure was true at some past
moment rather than now. So a reading is never returned bare: ``observed_at`` and
``source`` ride along with it, and the interface is expected to show them. A stale
percentage presented as current is the one genuinely harmful thing this module could
do -- someone would plan a long run against a number from last Tuesday.

A source that is missing, unreadable or malformed yields no windows and a ``reason``
saying so. That is the honest answer, and it is the same rule ``models`` follows: a
file Neo cannot parse is not evidence of anything.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.external_agents import detect

_LOG = logging.getLogger(__name__)

#: Reading a config file is cheap, but walking Codex's session directory is not, and
#: the composer asks on every load. Cached for the life of the process like the other
#: probes in this package; ``refresh=True`` is the way to get a new answer.
_CACHE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

#: Where a window stops being background information and starts being something to
#: say out loud. Derived here, once, so the composer's warning and the panel's colour
#: cannot drift apart -- and so the threshold is a fact about Neo rather than a
#: number written into two components.
WARNING_PERCENT = 80.0
EXHAUSTED_PERCENT = 100.0

#: Claude Code's window keys, in the order a person reads them, with the titles its
#: own ``/usage`` uses. Windows absent from the payload are simply not shown: the API
#: reports the ones that apply to the account, and inventing a "Weekly (Opus) 0%" row
#: for a plan that has no such limit would be asserting a limit that does not exist.
_CLAUDE_WINDOWS = (
    ("five_hour", "Session (5h)"),
    ("seven_day", "Weekly (7 day)"),
    ("seven_day_sonnet", "Weekly (Sonnet)"),
    ("seven_day_opus", "Weekly (Opus)"),
)

#: Codex names its windows by duration rather than by key -- ``primary`` is a 5-hour
#: window on one plan and a monthly one on another, as the free plan's 43200 shows.
#: So the title comes from ``window_minutes`` and never from the slot it arrived in.
_CODEX_WINDOW_TITLES = {
    300: "Session (5h)",
    1440: "Daily",
    10080: "Weekly (7 day)",
    43200: "Monthly (30 day)",
}


def _severity(percent: float) -> str:
    if percent >= EXHAUSTED_PERCENT:
        return "exhausted"
    if percent >= WARNING_PERCENT:
        return "warning"
    return "normal"


def _window(key: str, title: str, percent: Any, resets_at: Any) -> dict[str, Any] | None:
    """One bar, or None when the number is not a number.

    Percentages arrive from three sources on two scales and reset times in two
    formats; normalising at the single point where a window is built is what keeps
    that from leaking into the interface.
    """

    try:
        value = float(percent)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN, which float() accepts and no bar can draw
        return None
    value = max(0.0, value)
    return {
        "key": key,
        "title": title,
        "used_percent": round(value, 1),
        "resets_at": _epoch(resets_at),
        "severity": _severity(value),
    }


def _epoch(value: Any) -> float | None:
    """Unix seconds, from either an epoch number or an ISO 8601 string.

    Claude Code's cache writes ISO with an offset, its stream writes epoch integers,
    and Codex writes epoch integers. One representation reaches the frontend.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _home(executor: str, fallback: Path) -> Path:
    """That CLI's config directory, honouring an explicitly configured one.

    Same rule as ``models._config_path``: reading a directory the CLI will not be
    using would report some other installation's state.
    """

    spec = detect.SPECS.get(executor)
    configured = str(getattr(get_settings(), spec.home_setting, "") or "").strip() if spec else ""
    return Path(configured).expanduser() if configured else fallback


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

#: Claude Code's global state file. Note the location: it sits at the *home root*,
#: not inside ``~/.claude``. Only when ``CLAUDE_CONFIG_DIR`` is set does it move into
#: that directory -- and then it is a different file for a different configuration,
#: which is exactly the trap ``env`` documents. A machine can hold both; the one Neo
#: wants is whichever belongs to the configuration a run would actually use.
_CLAUDE_STATE_FILE = ".claude.json"


def _claude_state_path() -> Path:
    return _home("claude_code", Path.home()) / _CLAUDE_STATE_FILE


def _claude_from_cache() -> tuple[list[dict[str, Any]], float | None, str | None]:
    """(windows, observed_at, reason) from Claude Code's own usage cache."""

    path = _claude_state_path()
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return [], None, "Claude Code has not written a usage cache yet"
    except (OSError, ValueError):
        return [], None, "Claude Code's usage cache could not be read"
    if not isinstance(data, dict):
        return [], None, "Claude Code's usage cache could not be read"

    cached = data.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return [], None, "Claude Code has not recorded usage yet -- run it once"
    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return [], None, "Claude Code has not recorded usage yet -- run it once"

    windows: list[dict[str, Any]] = []
    for key, title in _CLAUDE_WINDOWS:
        entry = utilization.get(key)
        if not isinstance(entry, dict):
            continue
        # Already a percentage here, unlike the stream's fraction below.
        window = _window(key, title, entry.get("utilization"), entry.get("resets_at"))
        if window:
            windows.append(window)

    fetched = cached.get("fetchedAtMs")
    observed = float(fetched) / 1000.0 if isinstance(fetched, (int, float)) else None
    if not windows:
        return [], observed, "Claude Code has not recorded usage yet -- run it once"
    return windows, observed, None


#: The stream's ``utilization`` is a fraction of the window, not a percentage. The two
#: Claude sources disagree on this and the difference is invisible at low usage, so it
#: is converted at the one place the stream is read rather than trusted to look wrong.
_FRACTION_TO_PERCENT = 100.0


def _claude_from_run() -> tuple[list[dict[str, Any]], float | None]:
    """(windows, observed_at) from the last ``rate_limit_event`` a run recorded."""

    from app.services.agent_core import store as agent_store

    info, at = agent_store.latest_external_meta("claude_code", "rate_limit")
    if not isinstance(info, dict):
        return [], None

    unified = info.get("unifiedWindows")
    windows: list[dict[str, Any]] = []
    if isinstance(unified, dict):
        for key, title in _CLAUDE_WINDOWS:
            entry = unified.get(key)
            if not isinstance(entry, dict):
                continue
            fraction = entry.get("utilization")
            percent = (
                fraction * _FRACTION_TO_PERCENT if isinstance(fraction, (int, float)) else None
            )
            window = _window(key, title, percent, entry.get("resetsAt"))
            if window:
                windows.append(window)

    if not windows:
        # Older events, and gateway ones, carry only the currently limiting window.
        # One real bar beats none, so it is reported under whatever name it gave.
        limit_type = str(info.get("rateLimitType") or "")
        fraction = info.get("utilization")
        title = dict(_CLAUDE_WINDOWS).get(limit_type)
        if title and isinstance(fraction, (int, float)):
            window = _window(
                limit_type, title, fraction * _FRACTION_TO_PERCENT, info.get("resetsAt")
            )
            if window:
                windows.append(window)

    return windows, _epoch(at)


def _claude_usage() -> dict[str, Any]:
    """The better of Claude Code's two sources, with which one it was.

    Neither is live. The cache is rewritten by any run of the CLI, the stream notice
    by a run through Neo, so which is fresher depends on how the user has been
    working -- and the only defensible tie-break is the timestamp.
    """

    cached, cached_at, reason = _claude_from_cache()
    streamed, streamed_at = _claude_from_run()

    if streamed and (not cached or (streamed_at or 0) > (cached_at or 0)):
        return {"windows": streamed, "observed_at": streamed_at, "source": "run", "reason": None}
    if cached:
        return {
            "windows": cached,
            "observed_at": cached_at,
            "source": "cli_cache",
            "reason": None,
        }
    return {"windows": [], "observed_at": None, "source": None, "reason": reason}


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

#: How far back to look for a run that reported limits. A rollout exists per session
#: and a short or failed one carries no ``token_count`` at all, so the newest file is
#: often not the newest *answer*; a handful of files covers that without turning a
#: panel load into a directory walk.
_CODEX_ROLLOUT_SCAN = 25

#: Rollouts run to megabytes and the record wanted is the last one in the file, so
#: only the tail is read. Generous enough to clear several turns of transcript.
_CODEX_TAIL_BYTES = 512 * 1024


def _codex_rollouts(sessions: Path) -> list[Path]:
    try:
        found = [path for path in sessions.rglob("rollout-*.jsonl") if path.is_file()]
    except OSError:
        return []
    found.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
    return found[:_CODEX_ROLLOUT_SCAN]


def _codex_rate_limits(path: Path) -> tuple[dict[str, Any], str] | None:
    """The last ``token_count`` in one rollout that actually carried limits."""

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _CODEX_TAIL_BYTES))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return None

    found: tuple[dict[str, Any], str] | None = None
    # A partial first line is expected whenever the file was longer than the tail.
    for line in tail.splitlines()[1:] if size > _CODEX_TAIL_BYTES else tail.splitlines():
        if '"token_count"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            found = (limits, str(record.get("timestamp") or ""))
    return found


def _codex_usage() -> dict[str, Any]:
    sessions = _home("codex", Path("~/.codex").expanduser()) / "sessions"
    if not sessions.is_dir():
        return {
            "windows": [],
            "observed_at": None,
            "source": None,
            "plan": None,
            "reason": "Codex has not recorded a session yet",
        }

    for path in _codex_rollouts(sessions):
        found = _codex_rate_limits(path)
        if not found:
            continue
        limits, stamp = found
        windows: list[dict[str, Any]] = []
        for slot in ("primary", "secondary"):
            entry = limits.get(slot)
            if not isinstance(entry, dict):
                continue
            minutes = entry.get("window_minutes")
            title = _CODEX_WINDOW_TITLES.get(minutes) or (
                f"{int(minutes) // 60}h window" if isinstance(minutes, (int, float)) else slot
            )
            window = _window(slot, title, entry.get("used_percent"), entry.get("resets_at"))
            if window:
                windows.append(window)
        if not windows:
            continue
        plan = limits.get("plan_type")
        return {
            "windows": windows,
            "observed_at": _epoch(stamp),
            "source": "session_log",
            "plan": plan if isinstance(plan, str) and plan.strip() else None,
            "reason": None,
        }

    return {
        "windows": [],
        "observed_at": None,
        "source": None,
        "plan": None,
        # Said plainly, because it is a real property of this engine rather than a
        # failure: `codex exec --json` streams no limits, so a run through Neo only
        # leaves them in the rollout the CLI writes for itself.
        "reason": "Codex reports usage only after a completed run",
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

_READERS = {"claude_code": _claude_usage, "codex": _codex_usage}


def _account(row: dict[str, Any], plan: str | None) -> dict[str, Any] | None:
    """Who the CLI says it is signed in as, when it says anything at all.

    Claude Code's ``auth status --json`` names the account outright. Codex prints
    prose that identifies only the method, so the plan from its rollout is all there
    is -- and a block with three empty fields would read as a failure rather than as
    a CLI that does not answer the question.
    """

    fields = {
        "auth_method": row.get("auth_method"),
        "email": row.get("email"),
        "organization": row.get("organization"),
        "plan": row.get("plan") or plan,
    }
    return fields if any(value for value in fields.values()) else None


def _snapshot(executor: str) -> dict[str, Any]:
    row = detect.status(executor)
    spec = detect.SPECS.get(executor)
    base: dict[str, Any] = {
        "id": executor,
        "name": row.get("name") or (spec.name if spec else executor),
        "available": bool(row.get("available")),
        "auth": row.get("auth"),
        "account": None,
        "windows": [],
        "observed_at": None,
        "source": None,
        "reason": row.get("reason"),
    }
    if not base["available"]:
        # Nothing is read while the engine is unusable. The reason detection already
        # gave -- signed out, not installed, switched off -- is the useful thing to
        # say, and is more specific than anything this module could add.
        return base

    reader = _READERS.get(executor)
    if reader is None:
        return base
    try:
        found = reader()
    except Exception as exc:  # noqa: BLE001 - a usage panel must not break a composer
        _LOG.info("external_agent_usage_failed executor=%s", executor, exc_info=exc)
        return {**base, "reason": "usage could not be read from this machine"}

    return {
        **base,
        "account": _account(row, found.get("plan")),
        "windows": found["windows"],
        "observed_at": found["observed_at"],
        "source": found["source"],
        "reason": found["reason"],
    }


def snapshot(executor: str, *, refresh: bool = False) -> dict[str, Any]:
    """One engine's usage, as far as this machine records it."""

    if executor not in detect.SPECS:
        return {
            "id": executor,
            "name": executor,
            "available": False,
            "auth": None,
            "account": None,
            "windows": [],
            "observed_at": None,
            "source": None,
            "reason": "unknown executor",
        }

    with _LOCK:
        if not refresh and executor in _CACHE:
            return dict(_CACHE[executor])
    row = _snapshot(executor)
    with _LOCK:
        _CACHE[executor] = row
    return dict(row)


def snapshots(*, refresh: bool = False) -> list[dict[str, Any]]:
    return [snapshot(name, refresh=refresh) for name in detect.SPECS]


def warnings(executor: str) -> list[dict[str, Any]]:
    """The windows of one engine that are worth interrupting someone about."""

    return [
        window
        for window in snapshot(executor).get("windows") or []
        if window.get("severity") != "normal"
    ]


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = [
    "EXHAUSTED_PERCENT",
    "WARNING_PERCENT",
    "clear_cache",
    "snapshot",
    "snapshots",
    "warnings",
]

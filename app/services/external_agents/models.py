"""Which models an engine can be asked for, insofar as the machine will say.

Neither CLI will *tell* you. There is no ``models`` subcommand on either, and an
unknown model is rejected without naming the known ones -- ``claude -p x --model
nonsense`` answers ``unrecognized_model`` and stops. So a picker that made up a
catalogue would be Neo asserting something it cannot know, and asserting
something false the first time a vendor shipped or retired a model.

What one of them does do is leave the answer on disk anyway. Codex caches the
account's own model list where it can be read; Claude Code does not, and gets
the shorter answer it has earned rather than a padded one.

So what this reports is only what the machine actually said, each answer
carrying where it came from:

* **the default** -- the model the CLI's own configuration is set to, read out
  of that config. This is what a run uses today when Neo sends no ``--model``,
  so it is both a real option and the honest label for "leave it alone".
* **the account's own list** -- Codex writes one to ``~/.codex/models_cache.json``
  when it talks to the service, entries marked ``visibility: "list"`` being the
  ones it shows a person. That file *is* the answer to "which models do I have",
  and it comes from the vendor rather than from Neo. Claude Code keeps no
  equivalent, so it has none.
* **aliases** -- the shorthand the installed CLI documents in its own
  ``--help``. Claude Code names them there; Codex does not.
* **seen** -- every model id Neo has watched a run on this engine report. Not
  a catalogue, but the strongest possible claim: these ran, on this account.

Effort is discovered the same way and reported beside it. Both CLIs have a real
one -- ``claude --effort <level>`` and Codex's ``model_reasoning_effort`` -- and
both name their levels somewhere on disk, so neither list is written down here.

A sparse list is the correct output where nothing on the machine says more, and
the picker shows what there is rather than padding itself out.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import tomllib
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.external_agents import detect
from app.services.external_agents import env as env_module

_LOG = logging.getLogger(__name__)

#: A help probe is one process spawn and the answer changes only when the CLI is
#: upgraded, so it is cached for the life of the process like the detect probes.
_CACHE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

#: Where each CLI keeps the model it uses when nobody overrides it. Both are the
#: file the CLI itself writes, read rather than guessed at.
_CONFIG = {
    "claude_code": ("settings.json", "~/.claude"),
    "codex": ("config.toml", "~/.codex"),
}


def _config_path(executor: str) -> Path | None:
    spec = detect.SPECS.get(executor)
    entry = _CONFIG.get(executor)
    if spec is None or entry is None:
        return None
    filename, fallback = entry
    # An explicitly configured home wins, exactly as it does for a real run --
    # reading the default out of a directory the CLI will not be using would be
    # reporting another installation's configuration.
    configured = str(getattr(get_settings(), spec.home_setting, "") or "").strip()
    home = Path(configured).expanduser() if configured else Path(fallback).expanduser()
    return home / filename


#: Which key in each CLI's own config names the model, and which names the
#: reasoning effort. Both CLIs keep both, under names only they decide.
_CONFIG_KEYS = {
    "claude_code": {"model": "model", "effort": "effortLevel"},
    "codex": {"model": "model", "effort": "model_reasoning_effort"},
}


def _config(executor: str) -> dict[str, Any]:
    """That CLI's own configuration, or an empty one if it cannot be read.

    A config Neo cannot parse is not evidence of anything -- the picker then
    names no default, which is still correct, because sending no flag is still
    what happens.
    """

    path = _config_path(executor)
    if path is None or not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8")) if path.suffix == ".toml" else json.loads(raw)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _configured(executor: str, kind: str) -> str | None:
    key = (_CONFIG_KEYS.get(executor) or {}).get(kind)
    value = _config(executor).get(key) if key else None
    return value.strip() if isinstance(value, str) and value.strip() else None


#: Codex writes the account's own model list here whenever it talks to the
#: service. It is the vendor's answer to "which models do I have", cached by the
#: CLI itself -- so reading it is reporting what the machine was told, not
#: guessing. Entries are marked with a ``visibility``; ``list`` is the set Codex
#: itself would show a person, and the others are internal (a reserve model, the
#: one its own auto-review uses) that nobody chose and nobody should be offered.
_MODEL_CACHE = {"codex": ("models_cache.json", "~/.codex")}
_LISTED = "list"


def _account_models(executor: str) -> list[dict[str, str]]:
    """The account's own models, per the list that CLI cached from the service."""

    entry = _MODEL_CACHE.get(executor)
    spec = detect.SPECS.get(executor)
    if entry is None or spec is None:
        return []
    filename, fallback = entry
    configured = str(getattr(get_settings(), spec.home_setting, "") or "").strip()
    home = Path(configured).expanduser() if configured else Path(fallback).expanduser()
    try:
        data = json.loads((home / filename).read_bytes())
    except (OSError, ValueError):
        return []

    found: list[dict[str, str]] = []
    for record in data.get("models") or []:
        if not isinstance(record, dict) or record.get("visibility") != _LISTED:
            continue
        slug = record.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        found.append(
            {
                "id": slug.strip(),
                "source": "account",
                # Each model declares its own reasoning levels, and they differ:
                # one of these supports `ultra` and the others stop at `max`.
                "efforts": [
                    level.get("effort")
                    for level in record.get("supported_reasoning_levels") or []
                    if isinstance(level, dict) and isinstance(level.get("effort"), str)
                ],
            }
        )
    return found


def _effort_levels(executor: str, help_text: str) -> list[str]:
    """The effort levels this engine accepts, from whatever it says on disk.

    Claude Code documents them in its own ``--help`` -- ``--effort <level>``
    followed by a parenthesised list. Codex names them per model in the cache
    above, so the union of what its models accept is the honest set.
    """

    if executor == "codex":
        levels: list[str] = []
        for model in _account_models(executor):
            for level in model.get("efforts") or []:
                if level not in levels:
                    levels.append(level)
        return levels

    block = re.search(r"^\s+--effort[ =<].*?(?=\n\s+-{1,2}\w|\Z)", help_text, re.S | re.M)
    if not block:
        return []
    inside = re.search(r"\(([^)]*)\)", re.sub(r"\s+", " ", block.group(0)))
    if not inside:
        return []
    return [part.strip() for part in inside.group(1).split(",") if part.strip().isalpha()]


#: The sentence Claude Code's help uses to introduce its aliases, up to the
#: point where it switches to giving a *full name* example -- which is an
#: example of a different thing and must not land in the list.
_FULL_NAME_MARKER = "full name"


def _help_text(executor: str) -> str:
    """That CLI's own ``--help``, which is where both lists below come from.

    One probe rather than two: the aliases and the effort levels are documented
    in the same output, and spawning the process twice to read it twice would be
    two spawns for one answer.
    """

    spec = detect.SPECS.get(executor)
    binary = detect.resolve_binary(executor)
    if spec is None or not binary:
        return ""
    try:
        result = subprocess.run(  # noqa: S603 - argv is built here, never user input
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=detect.PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env=env_module.build_env(
                home_env=spec.home_env,
                home_dir=str(getattr(get_settings(), spec.home_setting, "") or ""),
            ),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.info("external_agent_help_probe_failed executor=%s", executor, exc_info=exc)
        return ""
    return f"{result.stdout}\n{result.stderr}"


def _aliases(help_text: str) -> list[str]:
    """The shorthand the installed CLI documents for ``--model``, if it does.

    Parsed from that CLI's own help rather than written down here, so a vendor
    adding an alias adds it to Neo too, and Neo never offers one the installed
    version has not heard of. A help text that says nothing about aliases yields
    nothing, which is the correct answer for Codex.
    """

    # The option's own paragraph: from the flag to the next flag at the same
    # indent. Help output wraps, so the aliases are several lines below the flag.
    block = re.search(
        r"^\s+(?:-\w,\s+)?--model[ =<].*?(?=\n\s+-{1,2}\w|\Z)", help_text, re.S | re.M
    )
    if not block:
        return []
    head = re.sub(r"\s+", " ", block.group(0)).split(_FULL_NAME_MARKER)[0]
    # Quoted lowercase tokens only. Prose in these paragraphs is unquoted, and a
    # model id is never capitalised or spaced.
    return list(dict.fromkeys(re.findall(r"'([a-z0-9][a-z0-9._\[\]-]*)'", head)))


_EMPTY = {"default": None, "options": [], "effort_default": None, "efforts": []}


def catalogue(executor: str, *, refresh: bool = False) -> dict[str, Any]:
    """Everything Neo can honestly say about this engine's models and effort.

    ``options`` is ordered the way a person would want to scan it: the account's
    own models first, then anything Neo has watched run, then the shorthand the
    CLI documents. The configured default is reported *separately* and kept out
    of the list, so it appears exactly once -- it is not one choice among
    several, it is what happens when no choice is made, and it stays correct
    when the list is empty.
    """

    if executor not in detect.SPECS:
        return {"executor": executor, **_EMPTY}

    with _LOCK:
        cached = None if refresh else _CACHE.get(executor)
    if cached is None:
        help_text = _help_text(executor)
        cached = {
            "default": _configured(executor, "model"),
            "effort_default": _configured(executor, "effort"),
            "aliases": _aliases(help_text),
            "account": _account_models(executor),
            "efforts": _effort_levels(executor, help_text),
        }
        with _LOCK:
            _CACHE[executor] = cached

    from app.services.agent_core import store as agent_store

    # Deliberately outside the cache: this one grows as runs happen, and a stale
    # answer would hide the model used a minute ago.
    seen = agent_store.models_seen(executor)
    default = cached["default"]

    options: list[dict[str, str]] = []

    def offer(identifier: str, source: str) -> None:
        # The configured default already has a row of its own, and a model named
        # twice in one menu reads as two models.
        if identifier == default or any(option["id"] == identifier for option in options):
            return
        options.append({"id": identifier, "source": source})

    for model in cached["account"]:
        offer(model["id"], "account")
    for model in seen:
        offer(model, "seen")
    for alias in cached["aliases"]:
        offer(alias, "documented")

    return {
        "executor": executor,
        "name": detect.SPECS[executor].name,
        #: What the CLI runs when Neo sends no override. May be None when the
        #: config names none -- then the CLI's own built-in default applies and
        #: Neo has no business naming it.
        "default": default,
        "options": options,
        #: The same story for reasoning effort, which both CLIs expose for real.
        "effort_default": cached["effort_default"],
        "efforts": [level for level in cached["efforts"] if level != cached["effort_default"]],
    }


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def chosen(per_engine: dict[str, Any] | None, executor: str) -> str | None:
    """What this chat asked for on this engine, or None to leave it alone.

    Used for both the model and the effort maps, which are shaped the same and
    keyed by engine for the same reason: the values are not interchangeable.
    ``opus`` means nothing to Codex, and a level one engine accepts may not be
    one the other does -- so switching engines keeps each engine's own choice
    rather than carrying one across to where it would fail.
    """

    if not isinstance(per_engine, dict):
        return None
    value = per_engine.get(executor)
    return value.strip() if isinstance(value, str) and value.strip() else None


def env_override(executor: str) -> str | None:
    """The end-to-end harness's escape hatch, kept for exactly that.

    ``scripts/e2e_external_agents.py`` needs to tell "the integration is broken"
    apart from "this machine's config pins a model the account cannot use", and
    it has no chat to set a model on.
    """

    if executor == "codex":
        return os.environ.get("NEO_CODEX_MODEL") or None
    if executor == "claude_code":
        return os.environ.get("NEO_CLAUDE_CODE_MODEL") or None
    return None


__all__ = ["catalogue", "chosen", "clear_cache", "env_override"]

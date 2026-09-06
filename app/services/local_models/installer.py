"""Downloading a model and making it the one Neo is using.

This is the step that turns "here is what would run well" into a model the user can
actually talk to. Everything before it is advice; without it the feature asks someone
who has never opened a terminal to open a terminal.

Progress is reported as events rather than returned at the end, because the download is
measured in gigabytes and a screen that sits still for ten minutes reads as broken.
Every message is already in plain language -- the caller relays it, it does not compose
it, so the wizard and any future interface say the same thing.

The one thing Neo cannot do for the user is install Ollama itself: that needs a
host-level installer and an administrator. So that case is a first-class outcome with
its own guidance rather than an error.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Generator
from typing import Any

import requests

from app.core.config import get_settings
from app.services.llm import LLMRegistry
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.types import ProviderCreate

_LOG = logging.getLogger(__name__)

# Where to send someone who has no engine yet.
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"

# A pull of a 40 GB model on a slow connection is a long wait, but a connection that has
# gone away should not hold the screen forever. Applied per chunk, not to the whole
# download, so a live transfer is never cut off.
_CHUNK_TIMEOUT_SECONDS = 120
_REACHABILITY_TIMEOUT_SECONDS = 3

Event = dict[str, Any]

# One flag per in-progress install, keyed by the catalog model id. A model download
# runs for minutes and the browser tab driving it can be closed or navigated away from
# long before it finishes -- without a way to signal cancellation the pull just keeps
# consuming bandwidth and disk on Ollama's side with nobody watching it.
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def cancel(key: str) -> bool:
    """Ask the install running under ``key`` to stop. Returns whether one was running."""

    with _cancel_lock:
        event = _cancel_events.get(key)
    if event is None:
        return False
    event.set()
    return True


def _base_url() -> str:
    return get_settings().ollama_url.rstrip("/")


def engine_available() -> bool:
    """Whether there is an Ollama for Neo to talk to."""

    try:
        response = requests.get(f"{_base_url()}/api/tags", timeout=_REACHABILITY_TIMEOUT_SECONDS)
        return response.status_code == 200
    except requests.RequestException:
        return False


def installed_tags() -> set[str]:
    """Which models are already downloaded, so the interface can offer to use them."""

    try:
        response = requests.get(f"{_base_url()}/api/tags", timeout=_REACHABILITY_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        _LOG.debug("Could not list installed models: %s", exc)
        return set()

    tags: set[str] = set()
    for entry in payload.get("models") or []:
        name = entry.get("name") or entry.get("model")
        if name:
            tags.add(name)
            # Ollama answers to "qwen3:8b" and lists it as "qwen3:8b", but a catalog row
            # may name the default tag bare. Record both so either matches.
            if name.endswith(":latest"):
                tags.add(name.rsplit(":", 1)[0])
    return tags


def _human_bytes(value: float) -> str:
    if value >= 1e9:
        return f"{value / 1e9:.1f} GB"
    if value >= 1e6:
        return f"{value / 1e6:.0f} MB"
    return f"{value / 1e3:.0f} KB"


def _progress_message(status: str, completed: float, total: float) -> str:
    """Ollama's own status strings are jargon; these are not."""

    if total and completed:
        return f"Downloading — {_human_bytes(completed)} of {_human_bytes(total)}"
    if "verif" in status or "sha" in status.lower():
        return "Checking the download is intact"
    if "manifest" in status:
        return "Looking the model up"
    if "extract" in status or "unpack" in status:
        return "Unpacking"
    return "Getting the model ready"


def _pull(tag: str, cancel_event: threading.Event) -> Generator[Event, None, bool]:
    """Stream Ollama's pull, yielding progress. Returns True if the model is present."""

    try:
        response = requests.post(
            f"{_base_url()}/api/pull",
            json={"model": tag, "stream": True},
            stream=True,
            timeout=_CHUNK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        yield {
            "type": "error",
            "message": "The download could not be started. Check your internet "
            "connection and try again.",
            "detail": str(exc),
        }
        return False

    last_percent = -1
    for line in response.iter_lines():
        if cancel_event.is_set():
            # Closing the connection is what actually stops Ollama pulling more of the
            # model -- returning from this generator without it would leave the
            # download running on Ollama's side with nothing left reading the stream.
            response.close()
            yield {"type": "cancelled", "message": "Download cancelled."}
            return False
        if not line:
            continue
        try:
            update = json.loads(line)
        except ValueError:
            continue

        if update.get("error"):
            yield {"type": "error", "message": f"The download failed: {update['error']}"}
            return False

        status = str(update.get("status") or "")
        completed = float(update.get("completed") or 0)
        total = float(update.get("total") or 0)
        percent = round(completed / total * 100) if total else None

        # Ollama reports progress far more often than a person can read it. Only send
        # an event when the number a user would see has actually changed.
        if percent is not None and percent == last_percent:
            continue
        if percent is not None:
            last_percent = percent

        yield {
            "type": "progress",
            "message": _progress_message(status, completed, total),
            "completed": completed,
            "total": total,
            "percent": percent,
        }

        if status == "success":
            return True

    # The stream ended without saying so; trust what is actually on disk.
    return tag in installed_tags()


def _ensure_provider(registry: LLMRegistryService) -> dict[str, Any]:
    """Find the Ollama provider Neo should register into, creating one if needed."""

    base_url = _base_url()
    for provider in registry.list_providers():
        if provider.get("provider_type") != "ollama":
            continue
        if str(provider.get("base_url") or "").rstrip("/") == base_url:
            return provider

    return registry.create_provider(
        ProviderCreate(
            name="Ollama",
            provider_type="ollama",
            base_url=base_url,
            metadata={"created_by": "local_models"},
        )
    )


def _select_in_picker(tag: str) -> None:
    """Make the newly installed model the one the composer's dropdown shows.

    Best effort. The model is already downloaded and routed by this point, so failing
    to move a dropdown is not worth failing the install over -- the user can pick it
    themselves, and the message they get says so.
    """

    try:
        picker = LLMRegistry()
        configs, _ = picker.load()
        base = _base_url()
        match = next(
            (
                config
                for config in configs
                if config.model == tag and config.base_url.rstrip("/") == base and config.enabled
            ),
            None,
        )
        if match is not None:
            picker.select(match.id)
    except Exception as exc:  # noqa: BLE001 - a dropdown is not worth an install failure
        _LOG.warning("Could not move the model picker to %s: %s", tag, exc)


def install(model: dict[str, Any]) -> Generator[Event, None, None]:
    """Download ``model`` and make it the model Neo is using.

    A generator so the caller can stream each event as it happens: a multi-gigabyte
    download that reports nothing until it finishes reads as a hung screen. Never
    raises -- the caller is a streaming HTTP response, and an exception there would
    close the connection with nothing on screen to explain it.
    """

    name = model.get("display_name") or model.get("id") or "this model"
    tag = model.get("ollama")
    key = model.get("id") or tag or name

    if not tag:
        yield {
            "type": "manual_only",
            "message": f"{name} has to be set up by hand. Neo can show you how.",
        }
        return

    yield {"type": "checking", "message": "Checking your computer is ready"}

    if not engine_available():
        # Not an error. Neo cannot install this itself -- it needs an administrator --
        # so this is the one point where the user may have to leave the app, and it
        # deserves explaining rather than reporting as a failure.
        yield {
            "type": "needs_engine",
            "message": (
                "Neo runs local models through a free program called Ollama, which is "
                "not installed yet. Install it, then come back to this page and try "
                "again."
            ),
            "download_url": OLLAMA_DOWNLOAD_URL,
        }
        return

    if tag in installed_tags():
        yield {"type": "progress", "message": "Already downloaded", "percent": 100}
    else:
        cancel_event = threading.Event()
        with _cancel_lock:
            _cancel_events[key] = cancel_event
        try:
            yield {"type": "progress", "message": "Starting the download", "percent": 0}
            downloaded = yield from _pull(tag, cancel_event)
        finally:
            with _cancel_lock:
                _cancel_events.pop(key, None)
        if not downloaded:
            return

    yield {"type": "registering", "message": f"Setting {name} up in Neo"}

    # Registration happens only after the download has succeeded, so a failed or
    # abandoned pull cannot leave a model registered that is not actually there.
    try:
        registry = LLMRegistryService()
        provider = _ensure_provider(registry)
        registry.discover_provider_models(provider["id"])
        # Two stores have to agree, and updating only one is worse than updating
        # neither: the composer's dropdown reads the legacy picker, while generation
        # resolves through the registry route. Setting the route alone would leave the
        # dropdown naming one model while a different one answered. This is the same
        # order the /llms/active/select route uses.
        _select_in_picker(tag)
        selected = registry.bind_picker_routes(tag, _base_url())
    except Exception as exc:  # noqa: BLE001 - the download worked; say so honestly
        _LOG.exception("Could not register %s after downloading it", tag)
        yield {
            "type": "error",
            "message": (
                f"{name} downloaded, but Neo could not switch to it automatically. You "
                "can pick it from the model menu in the chat box."
            ),
            "detail": str(exc),
        }
        return

    if selected:
        message = f"{name} is ready, and Neo is now using it."
    else:
        message = f"{name} is ready. Pick it from the model menu in the chat box to start using it."
    yield {"type": "done", "message": message, "model_id": model.get("id"), "tag": tag}

"""Which models exist, and how Neo would install each one.

Two layers. The seed catalog ships with Neo and is hand-verified, so the feature works
on first run, offline, on a machine that has never spoken to a model host. A live
refresh layers newer metadata on top of it later; a refresh that fails must leave the
seed serving rather than emptying the screen.

Every row is sanity-checked on load. Published parameter counts are wrong often enough
to matter -- placeholder values, counts that describe a configuration stub rather than
the released weights, counts that quietly exclude a vision encoder the file still
carries. Since the parameter count drives every memory estimate, one bad row produces a
confidently wrong recommendation, which is the exact failure this feature exists to
prevent. A row that contradicts itself is corrected rather than trusted, and the
correction is recorded so the interface can say the estimate is less certain.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

from app.services.local_models.types import Machine

_LOG = logging.getLogger(__name__)

_SEED_PATH = os.path.join(os.path.dirname(__file__), "data", "seed_models.json")

# "Qwen3-8B", "Llama-3.2-3B-Instruct", "gemma-3-27b-it". The size is a standalone token
# so that "Llama-3.2" does not read as a 3.2-billion-parameter model.
_SIZE_IN_NAME = re.compile(r"[-_/](\d+(?:\.\d+)?)\s*[bB](?=[-_/]|$)")

# How far a published count may sit from the one in the model's own name before the name
# is believed instead. Twice is generous: real rounding ("8B" for 8.03) is nowhere near
# it, and the failures seen in practice are off by orders of magnitude.
_DISAGREEMENT_FACTOR = 2.0

# How long a live refresh stays good. Model metadata changes on the order of weeks, and
# the seed is never wrong enough to make a stale refresh worse than a network round trip
# on every page load.
REFRESH_TTL_SECONDS = 86400

_lock = threading.Lock()
_cache: list[dict[str, Any]] | None = None
_refreshed_at: float = 0.0


def _size_from_name(model_id: str) -> float | None:
    """The parameter count the model's own name claims, in billions."""

    matches = _SIZE_IN_NAME.findall(model_id)
    if not matches:
        return None
    # "Qwen3-Coder-30B-A3B" names both its total and its active size. The total is the
    # larger and is the one a published count should be compared against.
    return max(float(value) for value in matches)


def sanity_check(model: dict[str, Any]) -> dict[str, Any]:
    """Correct a row that contradicts itself, and record that we did."""

    row = dict(model)
    claimed = row.get("parameters_b")
    named = _size_from_name(str(row.get("id") or ""))

    if not claimed or float(claimed) <= 0:
        if named:
            row["parameters_b"] = named
            row["data_caveat"] = (
                "This model did not publish its size, so Neo used the size in its name."
            )
        else:
            row["data_caveat"] = (
                "This model did not publish its size, so Neo could not estimate what it needs."
            )
        return row

    claimed = float(claimed)
    if named and (claimed > named * _DISAGREEMENT_FACTOR or claimed * _DISAGREEMENT_FACTOR < named):
        row["parameters_b"] = named
        row["data_caveat"] = (
            f"The published size for this model ({claimed:g} billion) disagreed with its "
            f"name ({named:g} billion), so Neo used the name."
        )
    return row


def _load_seed() -> list[dict[str, Any]]:
    """The catalog that ships with Neo. A broken file is empty, never an exception."""

    try:
        with open(_SEED_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.error("Could not read the local model catalog at %s: %s", _SEED_PATH, exc)
        return []

    rows = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        _LOG.error("The local model catalog at %s is not a list of models.", _SEED_PATH)
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def refresh(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Layer live metadata over the seed.

    Merges *into* the seed by id, so a model the refresh does not mention keeps its
    shipped row and a model the refresh has never heard of does not disappear. Any
    failure leaves the seed serving: an empty screen is worse than a slightly old one.

    Currently a no-op placeholder for the network fetch; the merge semantics and the
    failure behaviour are what the rest of the system depends on, and they are tested.
    """

    try:
        live = _fetch_live_metadata()
    except Exception as exc:  # noqa: BLE001 - a refresh must never break the catalog
        _LOG.warning("Live model refresh failed, serving the shipped catalog: %s", exc)
        return models

    if not live:
        return models

    by_id = {row["id"]: dict(row) for row in models}
    for update in live:
        model_id = update.get("id")
        if not model_id:
            continue
        if model_id in by_id:
            by_id[model_id].update({k: v for k, v in update.items() if v is not None})
        else:
            by_id[model_id] = dict(update)
    return [sanity_check(row) for row in by_id.values()]


def _fetch_live_metadata() -> list[dict[str, Any]]:
    """Newer metadata from the model host.

    Separate from ``refresh`` so the merge and the fetch can be tested apart -- the
    merge is the part with rules, and it should be verifiable without a network.
    """

    return []


def get_models(*, fresh: bool = False) -> list[dict[str, Any]]:
    """Every known model, sanity-checked. Cached per process."""

    global _cache, _refreshed_at
    with _lock:
        if _cache is not None and not fresh:
            return _cache

    rows = [sanity_check(row) for row in _load_seed()]

    stale = time.time() - _refreshed_at > REFRESH_TTL_SECONDS
    if rows and (fresh or stale):
        rows = refresh(rows)
        with _lock:
            _refreshed_at = time.time()

    with _lock:
        _cache = rows
    return rows


def get_model(model_id: str) -> dict[str, Any] | None:
    for row in get_models():
        if row.get("id") == model_id:
            return row
    return None


def reset_cache() -> None:
    global _cache, _refreshed_at
    with _lock:
        _cache = None
        _refreshed_at = 0.0


def install_options(model: dict[str, Any], machine: Machine) -> list[dict[str, Any]]:
    """How this model could be set up here, easiest path first.

    Always returns at least one option, and the last one is always the hand-rollable
    path. No model may be a dead end: the catalog shows everything the machine can run,
    so every row has to end somewhere a determined person could follow.
    """

    options: list[dict[str, Any]] = []

    tag = model.get("ollama")
    if tag:
        options.append(
            {
                "kind": "one_click",
                "target": tag,
                "label": "Set this up for me",
                "detail": "Neo downloads it and switches to it. Nothing else to do.",
            }
        )

    options.append(
        {
            "kind": "manual",
            "target": model.get("id"),
            "label": "Set it up myself",
            "detail": "Step-by-step instructions you can copy.",
        }
    )
    return options


def manual_steps(model: dict[str, Any]) -> list[dict[str, str]]:
    """Copyable steps for someone who would rather do it themselves.

    Every model has this path, which is what lets the catalog list models Neo cannot
    install for you rather than hiding them.
    """

    tag = model.get("ollama")
    if tag:
        return [
            {
                "step": "Install Ollama",
                "detail": "Download it from ollama.com and run the installer.",
                "command": "",
            },
            {
                "step": "Download the model",
                "detail": "Run this in a terminal. The first time will take a while.",
                "command": f"ollama pull {tag}",
            },
            {
                "step": "Point Neo at it",
                "detail": (
                    "In Settings, under LLM Providers, add an Ollama provider at "
                    "http://127.0.0.1:11434 and choose this model."
                ),
                "command": "",
            },
        ]

    repo = model.get("id") or ""
    return [
        {
            "step": "Install a runtime",
            "detail": (
                "llama.cpp runs compressed models on most computers. Install it with "
                "your package manager."
            ),
            "command": "brew install llama.cpp",
        },
        {
            "step": "Download the model",
            "detail": f"Fetch a compressed build of {model.get('display_name') or repo}.",
            "command": f"hf download {repo}",
        },
        {
            "step": "Start it",
            "detail": "This runs a local server Neo can talk to.",
            "command": "llama-server -m <path-to-the-downloaded-file> --port 8080",
        },
        {
            "step": "Point Neo at it",
            "detail": (
                "In Settings, under LLM Providers, add an OpenAI-compatible provider at "
                "http://127.0.0.1:8080/v1."
            ),
            "command": "",
        },
    ]

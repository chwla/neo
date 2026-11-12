"""How fast a model actually runs on this machine, and the time limits that follow from it.

Neo's time limits were written as constants: 240 s for a model call, 20 minutes for an agent
run, 30 for standard research. Those suit a GPU generating 40-100 tokens a second. The same
model on a CPU-only machine generates 3-5, so the identical request takes ten to twenty times
as long, and a constant that was generous on one machine fails every long call on the other.

Nothing has to be guessed: every request already records its token counts and the provider's
own timings in ``workspace_provider_requests``. This module reads those back, per model, and
turns them into limits that are **never lower than the configured ones** -- a fast machine,
or a model with no history yet, keeps exactly the behaviour it had. Only a machine measured to
be slow gets more room, and only as much as its measured speed says the work needs, up to a cap.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass

from app.core.config import get_settings

#: Recent requests a model's pace is measured from. Recent, because hardware and load change.
SAMPLE_SIZE = 40
#: Fewer completed requests than this is no measurement; the configured limits apply.
MIN_SAMPLES = 3
#: How long a measurement is reused before the log is read again.
CACHE_SECONDS = 120.0
#: Headroom over the expected time: a call slower than usual is still a call that works.
SAFETY = 1.5
#: Fixed slack for connection setup and scheduling, which do not scale with tokens.
SLACK_SECONDS = 60
#: How long a model with no history on this machine is waited for. Before its first answer
#: nothing is known: it may be a small model on a GPU or a large one loading from a slow disk
#: onto a CPU, and the default limit would fail the second case on the very call that would
#: have measured it. Local replies are streamed, so this bounds the wait for the first token.
FIRST_CALL_SECONDS = 900
#: No single model call waits longer than this, however slow the machine. A guard against a
#: call that will never finish, not a judgement on how long a call may reasonably take: a large
#: model on a modest CPU writing a long answer can need well over an hour.
MAX_CALL_SECONDS = 6 * 3600
#: No run's time ceiling is raised past this. The work a run does is bounded by its mode (calls,
#: searches, pages, steps); time only has to stop a run that has stopped making progress, and
#: someone who chose a large model on a slow machine chose accuracy over speed.
MAX_RUN_SECONDS = 24 * 3600


@dataclass(frozen=True)
class ModelPace:
    """Medians over a model's recent completed requests on this machine."""

    model: str
    samples: int
    #: Generated tokens per second, from the provider's own decode timing.
    decode_tps: float | None
    #: Prompt tokens processed per second.
    prefill_tps: float | None
    #: Seconds spent loading the model into memory, when a request had to.
    load_seconds: float
    #: Wall-clock seconds a completed request took, end to end.
    call_seconds: float
    #: Of ``samples``, how many came from the route asked about. When too few did, the model's
    #: requests on other routes stood in: its speed still holds, its call size may not.
    route_samples: int = 0
    #: The mean wall-clock time per completed request. Budgets add calls up, so they use the
    #: mean: research mixes short calls with very long ones, and the median of those (72 s on
    #: a CPU-only machine whose calls averaged five minutes) sized a run at a quarter of its time.
    mean_call_seconds: float = 0.0

    def budget_seconds(self) -> float:
        """Seconds per call to plan a run's time with."""
        return self.mean_call_seconds or self.call_seconds

    def expected_seconds(self, prompt_tokens: int, output_tokens: int) -> float | None:
        """How long a request of this size should take here, or None when unmeasured."""
        if not self.decode_tps:
            return None
        prefill = prompt_tokens / self.prefill_tps if self.prefill_tps else 0.0
        return self.load_seconds + prefill + output_tokens / self.decode_tps


_cache: dict[tuple[str, str, str], tuple[float, ModelPace | None]] = {}
_lock = threading.Lock()


def _database() -> str:
    url = get_settings().database_url
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"


def _rows(database: str, model: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(database, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT route_name, latency_ms, provider_usage_json FROM workspace_provider_requests"
            " WHERE model_name = ? AND status = 'completed' AND latency_ms IS NOT NULL"
            " ORDER BY created_at DESC LIMIT ?",
            (model, SAMPLE_SIZE * 3),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _measure(model: str, route: str, rows: list[sqlite3.Row]) -> ModelPace | None:
    # The route's own requests describe its typical size best (research prompts are long,
    # chat replies short); the model's requests on any route still measure its speed.
    own = [row for row in rows if row["route_name"] == route][:SAMPLE_SIZE]
    chosen = own if len(own) >= MIN_SAMPLES else rows[:SAMPLE_SIZE]
    if len(chosen) < MIN_SAMPLES:
        return None
    decode, prefill, load, wall = [], [], [], []
    for row in chosen:
        wall.append(row["latency_ms"] / 1000)
        try:
            usage = json.loads(row["provider_usage_json"] or "{}")
        except ValueError:
            continue
        completion, decode_ms = usage.get("completion_tokens"), usage.get("decode_ms")
        if isinstance(completion, int) and completion > 0 and decode_ms:
            decode.append(completion / (decode_ms / 1000))
        prompt, prefill_ms = usage.get("prompt_tokens"), usage.get("prefill_ms")
        if isinstance(prompt, int) and prompt > 0 and prefill_ms:
            prefill.append(prompt / (prefill_ms / 1000))
        load.append((usage.get("load_ms") or 0) / 1000)
    return ModelPace(
        model=model,
        samples=len(chosen),
        decode_tps=statistics.median(decode) if decode else None,
        prefill_tps=statistics.median(prefill) if prefill else None,
        load_seconds=statistics.median(load) if load else 0.0,
        call_seconds=statistics.median(wall),
        route_samples=len(own) if chosen is own else 0,
        mean_call_seconds=statistics.fmean(wall),
    )


def measured(model: str | None, route: str | None = None) -> ModelPace | None:
    """This machine's pace for ``model``, or None when there is not enough history."""
    if not model:
        return None
    database = _database()
    key = (database, model, route or "")
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    pace = _measure(model, route or "", _rows(database, model))
    with _lock:
        _cache[key] = (now + CACHE_SECONDS, pace)
    return pace


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def call_timeout(
    configured: int,
    pace: ModelPace | None,
    *,
    prompt_tokens: int,
    max_output_tokens: int | None,
) -> int:
    """Seconds to wait for one model call: the configured limit, or what this machine needs.

    Sized for a request that writes its whole output allowance, because that is the request
    a short limit kills. A model never measured here gets ``FIRST_CALL_SECONDS``. Never below
    ``configured``.
    """
    if pace is None:
        return max(configured, FIRST_CALL_SECONDS)
    expected = pace.expected_seconds(prompt_tokens, max_output_tokens or 0)
    if expected is None:
        return max(configured, FIRST_CALL_SECONDS)
    needed = math.ceil(expected * SAFETY) + SLACK_SECONDS
    return max(configured, min(MAX_CALL_SECONDS, needed))


def scaled_ceiling(
    configured: float, pace: ModelPace | None, *, calls: int, extra_seconds: float = 0.0
) -> float:
    """A run's time ceiling: as configured, or long enough for its work at this machine's pace.

    ``calls`` is the model calls the run's work takes, ``extra_seconds`` the rest of it
    (searching, reading). Never below ``configured``, never above ``MAX_RUN_SECONDS``.
    """
    if pace is None or calls <= 0:
        return configured
    needed = (calls * pace.budget_seconds() + extra_seconds) * SAFETY
    return max(configured, min(float(MAX_RUN_SECONDS), needed))

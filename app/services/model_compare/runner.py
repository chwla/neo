"""Running one comparison across several models at once.

The design is about wall-clock time, because a comparison nobody waits for is a
comparison nobody runs. Four decisions do most of that work.

Models run in parallel and tasks run in sequence within a model. Putting the parallelism
on the model axis is what makes a two-model comparison cost roughly what asking one model
costs; putting it on the task axis instead would have every model competing with itself
for the same machine.

Local models are capped at two at a time. Ollama evicts a model when too many are
resident, and an evicted model reloads from disk on its next task -- so a third parallel
local model does not make the run faster, it makes every task in it pay a cold load.
Models reached over the network have no such ceiling and are not counted against it.

Every model is warmed before anything is timed. The first call to a model that is not
resident spends seconds loading weights, which has nothing to do with how fast it
answers; timing that would mean whichever model happened to already be loaded wins.

And answers are generated as a stream, so a cell can report when the model *started*
producing separately from how long the whole answer took, and so the grid fills in front
of the user rather than appearing all at once at the end.

Nothing in this module ranks anything. It measures, and it reports what it measured.
"""

from __future__ import annotations

import json
import logging
import queue
import statistics
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from app.core.config import get_settings
from app.services.llm import LLMConfig, LLMMessage, OllamaClient, OpenAICompatibleClient
from app.services.model_compare.grading import grade
from app.services.model_compare.types import (
    Comparison,
    Contender,
    ContenderSummary,
    GenerationSettings,
    RunConfig,
    Task,
    TaskOutcome,
    now,
)

_LOG = logging.getLogger(__name__)

#: How many local models may generate at once. Two is what fits alongside each other on
#: an ordinary machine; a third mostly buys reloads. See the module docstring.
MAX_LOCAL_PARALLEL = 2

#: A ceiling for models reached over the network, where the limit is politeness to the
#: far end rather than memory on this machine.
MAX_REMOTE_PARALLEL = 4

#: A single task that has not answered by now is not going to rescue the run. Applied per
#: task, so one stuck model costs one cell rather than the whole comparison.
TASK_DEADLINE_SECONDS = 90

#: Loading a large model off a cold disk is slow, but not this slow. Past this the model
#: is reported as unreachable rather than held open.
WARMUP_DEADLINE_SECONDS = 120

#: Rough conversion from characters of reasoning to tokens of it, used only to report how
#: much of a model's wait went on thinking rather than answering. The providers count
#: thinking inside the completion total and do not break it out, so this is an estimate
#: and is labelled as one wherever it is shown.
CHARS_PER_TOKEN = 4.0

#: What warming a model asks it. Short enough to cost nothing, real enough that the
#: provider actually loads the weights rather than short-circuiting.
WARMUP_PROMPT = "Say ok."

#: How long to spend asking a provider which models it already has resident. Used only to
#: sharpen the time estimate, so it is never worth waiting on.
RESIDENCY_TIMEOUT_SECONDS = 2

# One cancel flag per in-flight run. A comparison runs for tens of seconds in server-side
# threads that outlive the browser's connection, so closing the tab has to be able to
# reach them -- aborting the fetch alone would leave every model still generating.
_cancels: dict[str, threading.Event] = {}
_cancels_lock = threading.Lock()

Event = dict[str, Any]


def cancel(run_id: str) -> bool:
    """Ask a run to stop. Returns whether there was one to stop."""

    with _cancels_lock:
        flag = _cancels.get(run_id)
    if flag is None:
        return False
    flag.set()
    return True


def active_runs() -> list[str]:
    with _cancels_lock:
        return sorted(_cancels)


def contender_from_config(config: LLMConfig, *, version: str = "") -> Contender:
    """One registry entry as a comparison sees it."""

    return Contender(
        id=config.id,
        # The model tag is what is actually being compared. Two registry entries often
        # share a name ("Ollama") and differ only in model, so leading with the name
        # would put two indistinguishable columns on screen.
        display_name=config.model or config.name,
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        local=is_local(config.base_url),
        version=version,
        config_name=config.name,
    )


#: Addresses that mean "this machine", and so count against the local parallelism cap.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "host.docker.internal")


def is_local(base_url: str) -> bool:
    lowered = (base_url or "").lower()
    return any(host in lowered for host in _LOCAL_HOSTS)


def resident_models(base_url: str) -> set[str]:
    """Which models a local provider already has loaded.

    Used only to sharpen the time estimate: a resident model skips the multi-second load
    that dominates a cold run, and promising that wait when it will not happen makes the
    estimate wrong in the direction that puts people off pressing the button. Failure is
    an empty set -- an estimate that assumes everything is cold is merely pessimistic.
    """

    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/api/ps", timeout=RESIDENCY_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return {
            str(item.get("name") or item.get("model") or "")
            for item in response.json().get("models") or []
        }
    except (requests.RequestException, ValueError):
        return set()


def build_client(
    config: LLMConfig, *, max_tokens: int, timeout: int, allow_thinking: bool = False
):
    """A client bound to one registry entry, tuned for a benchmark rather than a chat.

    Two departures from the ordinary chat path.

    Ollama is asked to keep the model resident, so the tasks after the first do not each
    pay a load -- which on a three-task run is most of what the run would otherwise cost.

    And thinking is off unless it was asked for. A reasoning model given a task-sized
    token budget will spend the whole of it thinking and return an empty answer: measured
    here, gemma4 used all 400 tokens of a judging budget on its reasoning and answered
    with nothing at all. Under a cap, leaving thinking on does not measure the model, it
    measures the cap -- so it is a choice the user makes deliberately, and when they make
    it the tokens it costs are reported back to them.
    """

    common = {
        "model": config.model,
        "base_url": config.base_url,
        "timeout": timeout,
        "num_predict": max_tokens,
    }
    if config.provider == "ollama":
        return OllamaClient(
            **common,
            keep_alive=get_settings().ollama_keep_alive,
            disable_thinking=not allow_thinking,
        )
    return OpenAICompatibleClient(**common, api_key=config.resolved_api_key())


def _estimate_thinking_tokens(thinking: str | None) -> int | None:
    if not thinking:
        return None
    return max(1, int(len(thinking) / CHARS_PER_TOKEN))


def warm(
    config: LLMConfig,
    contender: Contender,
    stop: threading.Event,
    *,
    allow_thinking: bool = False,
) -> tuple[bool, int, str]:
    """Load the model and confirm it answers. Returns (ready, milliseconds, problem)."""

    started = time.perf_counter()
    client = build_client(
        config, max_tokens=4, timeout=WARMUP_DEADLINE_SECONDS, allow_thinking=allow_thinking
    )
    try:
        client.chat_with_metadata([LLMMessage(role="user", content=WARMUP_PROMPT)], temperature=0)
    except requests.Timeout:
        return False, 0, "This model took too long to start up."
    except requests.ConnectionError:
        where = "on this computer" if contender.local else "at its address"
        return False, 0, f"Neo could not reach this model {where}."
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 404:
            return False, 0, "This model is not installed."
        return False, 0, "This model refused the request."
    except Exception as exc:
        _LOG.debug("Warm-up failed for %s: %s", contender.id, exc)
        return False, 0, "This model could not be started."
    if stop.is_set():
        return False, 0, "Cancelled."
    return True, int((time.perf_counter() - started) * 1000), ""


def _failed(contender_id, task_id, status, started, message, **extra) -> TaskOutcome:
    return TaskOutcome(
        contender_id=contender_id,
        task_id=task_id,
        status=status,
        state="cancelled" if status == "cancelled" else "error",
        duration_ms=int((time.perf_counter() - started) * 1000),
        message=message,
        started_at=extra.pop("started_at", ""),
        **extra,
    )


def run_task(
    config: LLMConfig,
    contender: Contender,
    task: Task,
    settings: GenerationSettings | None = None,
    *,
    deterministic: bool = True,
) -> TaskOutcome:
    """Ask one model one question and grade what comes back.

    Generated as a stream rather than in one call. The answer is identical either way;
    what streaming buys is the moment the first token arrived, which is a different
    measurement from how long the whole answer took and the one that decides whether a
    model *feels* responsive.
    """

    settings = settings or GenerationSettings()
    # A run-wide ceiling overrides the task's own. Left unset -- the default -- each task
    # keeps the cap it was written with, which is sized to the answer it asks for.
    max_tokens = settings.max_output_tokens or task.max_tokens
    client = build_client(
        config,
        max_tokens=max_tokens,
        timeout=TASK_DEADLINE_SECONDS,
        allow_thinking=settings.allow_thinking,
    )
    messages = [LLMMessage(role="user", content=task.prompt)]
    if task.system:
        messages.insert(0, LLMMessage(role="system", content=task.system))

    stamp = now()
    started = time.perf_counter()
    first_token: float | None = None
    body: list[str] = []
    reasoning: list[str] = []
    done: dict[str, Any] = {}
    try:
        for event in client.chat_stream(
            messages, temperature=settings.temperature, num_predict=max_tokens
        ):
            kind = event.get("type")
            if kind in {"chunk", "thinking"} and first_token is None:
                first_token = time.perf_counter()
            if kind == "chunk":
                body.append(str(event.get("content") or ""))
            elif kind == "thinking":
                reasoning.append(str(event.get("content") or ""))
            elif kind == "done":
                done = event
    except requests.Timeout:
        return _failed(
            contender.id, task.id, "timed_out", started,
            f"No answer within {TASK_DEADLINE_SECONDS} seconds.", started_at=stamp,
        )
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        # The provider answered, but not in a shape the client could read. Reported as
        # its own failure because "the model is broken" and "the reply was garbled" send
        # someone to different places.
        _LOG.debug("Malformed stream from %s on %s: %s", contender.id, task.id, exc)
        return _failed(
            contender.id, task.id, "failed", started,
            "This model sent a reply Neo could not read.", started_at=stamp,
        )
    except requests.ConnectionError:
        return _failed(
            contender.id, task.id, "failed", started,
            "The connection to this model dropped part way through.", started_at=stamp,
        )
    except Exception as exc:
        _LOG.debug("Task %s failed on %s: %s", task.id, contender.id, exc)
        return _failed(
            contender.id, task.id, "failed", started, "This model did not answer.",
            started_at=stamp,
        )

    elapsed = done.get("duration_ms") or int((time.perf_counter() - started) * 1000)
    ttft = int((first_token - started) * 1000) if first_token is not None else None
    raw = "".join(body)
    answer = client.clean_response(raw)
    thinking = "".join(reasoning).strip() or (client.extract_thinking(raw) or "")
    finish = str(done.get("finish_reason") or "stop")
    completion = done.get("completion_tokens")

    if not answer:
        # Two different nothings. A model that spent its budget reasoning never reached
        # an answer; one that stopped cleanly with nothing to say gave an empty one.
        # Neither is a wrong answer, and grading either as one would be a lie.
        spent_thinking = finish == "length" and bool(thinking)
        return TaskOutcome(
            contender_id=contender.id,
            task_id=task.id,
            status="empty",
            state="error",
            thinking=thinking,
            duration_ms=elapsed,
            time_to_first_token_ms=ttft,
            completion_tokens=completion,
            thinking_tokens=_estimate_thinking_tokens(thinking),
            finish_reason=finish,
            started_at=stamp,
            message=(
                "Used its whole budget thinking and never answered."
                if spent_thinking
                else "This model returned an empty answer."
            ),
        )

    checks, score = ((), None)
    evaluation = "none"
    if deterministic and task.gradeable:
        checks, score = grade(answer, task.checks)
        evaluation = "deterministic"
    rate = (completion / (elapsed / 1000)) if completion and elapsed > 0 else None
    return TaskOutcome(
        contender_id=contender.id,
        task_id=task.id,
        status="ok",
        state="complete",
        answer=answer,
        thinking=thinking,
        checks=tuple(checks),
        score=score,
        evaluation=evaluation,
        duration_ms=elapsed,
        time_to_first_token_ms=ttft,
        completion_tokens=completion,
        thinking_tokens=_estimate_thinking_tokens(thinking),
        tokens_per_second=rate,
        finish_reason=finish,
        started_at=stamp,
    )


def workers(contenders: list[Contender]) -> int:
    """How many models may run at once, counting local and remote separately.

    Local models are limited by what fits in this machine's memory at once; remote ones
    are not, so a comparison of one local and two hosted models runs all three rather
    than being held to the local ceiling.
    """

    local = sum(1 for item in contenders if item.local)
    remote = len(contenders) - local
    return max(1, min(local, MAX_LOCAL_PARALLEL) + min(remote, MAX_REMOTE_PARALLEL))


#: What a mid-sized local model generates at, per second. Deliberately low: the figure
#: this feeds is shown as a ceiling the run should come in under.
ESTIMATE_TOKENS_PER_SECOND = 15.0

#: Loading one model that is not already resident.
ESTIMATE_WARMUP_SECONDS = 8

#: Roughly how long a *right* answer is, per use case. Estimating from each task's token
#: cap instead does not work: the caps are generous on purpose, to leave a rambling model
#: room to finish, so a pack whose answers are a single number carries the same cap as
#: one that asks for a function. What actually varies is what is being asked for, and
#: that is exactly the axis a use case names. Measured against real runs of each pack.
ESTIMATE_ANSWER_TOKENS: dict[str, int] = {
    "reasoning": 15,
    "chat": 20,
    "writing": 35,
    "structured": 40,
    "coding": 150,
    # Measured, not guessed: three custom questions across two local models took 79s,
    # against the 48s a 200-token assumption promised. A question of the user's own has
    # no format instruction reining the answer in, so it runs long.
    "custom": 350,
}
ESTIMATE_FALLBACK_TOKENS = 60

#: What one judging pass over one task's answers costs, in tokens of rating. Reading
#: several answers before writing a verdict costs more than the verdict itself.
ESTIMATE_JUDGE_TOKENS = 130


def estimate_seconds(
    contenders: list[Contender],
    tasks: list[Task],
    *,
    generation: GenerationSettings | None = None,
    judge_enabled: bool = False,
    warm_ids: frozenset[str] = frozenset(),
) -> int:
    """A deliberately pessimistic guess at the wait, derived from this configuration.

    Pessimistic because a run that finishes sooner than promised is a good surprise, and
    the reverse is what makes someone stop trusting the number and stop pressing the
    button. Over-promising is the failure mode this is tuned against.

    Everything the caller can change is accounted for: how many models, how many tasks,
    what is being asked for, whether a token ceiling has been lowered onto it, whether a
    judging pass follows, and which models are already loaded. Where a fact is not
    available -- residency on a hosted provider, say -- the pessimistic assumption is
    made rather than a confident one.
    """

    if not contenders or not tasks:
        return 0
    settings = generation or GenerationSettings()
    ceiling = settings.max_output_tokens

    def answer_tokens(task: Task) -> int:
        typical = ESTIMATE_ANSWER_TOKENS.get(task.use_case, ESTIMATE_FALLBACK_TOKENS)
        # A lowered ceiling caps the wait; a raised one does not lengthen it, because
        # the answer stops when the model is finished, not when the budget runs out.
        return min(typical, ceiling) if ceiling else typical

    budget = sum(answer_tokens(task) for task in tasks)
    if settings.allow_thinking:
        # Reasoning is generated before the answer and is often several times its length.
        budget = int(budget * 2.5)

    rounds = -(-len(contenders) // workers(contenders))  # ceiling division
    seconds = rounds * budget / ESTIMATE_TOKENS_PER_SECOND

    # Only the models that still have to be loaded, and only as many at a time as will
    # actually run at once.
    cold = [item for item in contenders if item.id not in warm_ids]
    if cold:
        cold_rounds = -(-len(cold) // workers(contenders))
        seconds += cold_rounds * ESTIMATE_WARMUP_SECONDS

    if judge_enabled:
        # One pass per task, in sequence, on one model.
        seconds += len(tasks) * ESTIMATE_JUDGE_TOKENS / ESTIMATE_TOKENS_PER_SECOND
    return max(1, int(seconds))


def _median_int(values: list[int]) -> int:
    return int(statistics.median(values)) if values else 0


def summarise(
    contenders: list[Contender],
    outcomes: list[TaskOutcome],
    warmups: dict[str, int],
    errors: dict[str, str],
    warm_ids: frozenset[str] = frozenset(),
) -> list[ContenderSummary]:
    """Roll a model's outcomes into its measurements.

    Measurements only. Nothing here compares one model with another, because the moment
    a summary knows about its rivals it is one step from ranking them.
    """

    summaries: list[ContenderSummary] = []
    for contender in contenders:
        mine = [item for item in outcomes if item.contender_id == contender.id]
        answered = [item for item in mine if item.status == "ok"]
        scored = [item.score for item in answered if item.score is not None]
        judged = [item.judge_score for item in answered if item.judge_score is not None]
        rates = [item.tokens_per_second for item in answered if item.tokens_per_second]
        ttfts = [
            item.time_to_first_token_ms
            for item in answered
            if item.time_to_first_token_ms is not None
        ]
        applied = [
            check
            for item in answered
            for check in item.checks
            if check.status != "skipped"
        ]
        summaries.append(
            ContenderSummary(
                contender=contender,
                score=(sum(scored) / len(scored)) if scored else None,
                judge_score=(sum(judged) / len(judged)) if judged else None,
                tasks_answered=len(answered),
                tasks_attempted=len(mine),
                tasks_graded=len(scored),
                checks_passed=sum(1 for check in applied if check.status == "passed"),
                checks_applied=len(applied),
                median_duration_ms=_median_int([item.duration_ms for item in answered]),
                median_time_to_first_token_ms=_median_int(ttfts) or None,
                median_tokens_per_second=statistics.median(rates) if rates else None,
                total_duration_ms=sum(item.duration_ms for item in mine),
                total_completion_tokens=sum(item.completion_tokens or 0 for item in answered),
                warmup_ms=warmups.get(contender.id),
                was_warm=contender.id in warm_ids if warm_ids else None,
                error=errors.get(contender.id, ""),
            )
        )
    return summaries


def run(
    pairs: list[tuple[LLMConfig, Contender]],
    config: RunConfig,
    *,
    finalize: Any = None,
    warm_ids: frozenset[str] = frozenset(),
) -> Iterator[Event]:
    """Run every task against every model, reporting each result as it lands.

    Yields newline-delimited-JSON-ready events. The generator owns the cancel flag for
    the run and clears it on the way out, however the run ends.

    ``finalize`` is an optional generator called with the assembled comparison once every
    task has answered and before anything is totalled. It may yield events of its own and
    may rewrite the outcomes -- which is how the judging pass attaches its ratings without
    this module needing to know that judging exists.
    """

    contenders = list(config.contenders)
    tasks = list(config.tasks)
    run_id = config.run_id or str(uuid.uuid4())
    stop = threading.Event()
    with _cancels_lock:
        _cancels[run_id] = stop

    comparison = Comparison(config=config)
    warmups: dict[str, int] = {}
    errors: dict[str, str] = {}
    total_cells = len(contenders) * len(tasks)
    finished = 0
    wall = time.perf_counter()
    # Threads push here and the generator drains it, which is what lets a result reach
    # the browser the moment it exists rather than when its model finishes every task.
    inbox: queue.Queue[Event | None] = queue.Queue()

    def work(llm: LLMConfig, contender: Contender) -> None:
        # Nothing may escape this function. The drain loop below ends when the watcher
        # queues its sentinel, and the watcher only reaches that after every worker has
        # returned -- so an exception thrown out of here would leave the stream waiting
        # on a queue that nobody will ever put anything on, which the browser sees as a
        # comparison that started and then never said another word.
        try:
            ready, warmup_ms, problem = warm(
                llm, contender, stop, allow_thinking=config.generation.allow_thinking
            )
            if not ready:
                errors[contender.id] = problem
                inbox.put(
                    {
                        "type": "contender_failed",
                        "contender_id": contender.id,
                        "message": problem,
                        # Named so the browser can settle every cell this model will
                        # never reach. A column stuck on "Queued" for ever is the worst
                        # thing the grid could show.
                        "tasks": [task.id for task in tasks],
                    }
                )
                return
            warmups[contender.id] = warmup_ms
            inbox.put({"type": "ready", "contender_id": contender.id, "warmup_ms": warmup_ms})
            for task in tasks:
                if stop.is_set():
                    return
                inbox.put(
                    {
                        "type": "cell_state",
                        "contender_id": contender.id,
                        "task_id": task.id,
                        "state": "generating",
                    }
                )
                outcome = run_task(
                    llm, contender, task, config.generation, deterministic=config.deterministic
                )
                comparison.outcomes.append(outcome)
                inbox.put({"type": "result", **outcome.as_dict()})
        except Exception as exc:
            _LOG.exception("Comparison worker for %s stopped", contender.id)
            errors.setdefault(contender.id, "This model stopped part way through.")
            answered = {
                item.task_id for item in comparison.outcomes if item.contender_id == contender.id
            }
            inbox.put(
                {
                    "type": "contender_failed",
                    "contender_id": contender.id,
                    "message": f"This model stopped part way through ({exc.__class__.__name__}).",
                    "tasks": [task.id for task in tasks if task.id not in answered],
                }
            )

    pool = ThreadPoolExecutor(max_workers=workers(contenders), thread_name_prefix="compare")
    try:
        # The work is started before the first event is yielded, for two reasons. It
        # means warm-up begins while the browser is still reading the opening event
        # rather than after it. And it means the watcher thread -- which owns teardown --
        # exists from the outset, so a reader that takes the first event and then vanishes
        # still leaves a run that cleans up after itself.
        futures = [pool.submit(work, llm, contender) for llm, contender in pairs]

        def watch() -> None:
            # The sentinel goes in a `finally` for the same reason: it is the only thing
            # that ends the drain loop, so it has to be queued however the wait ends.
            #
            # This thread also owns the teardown, because it is the only part of a run
            # that is guaranteed to finish. When a browser goes away mid-run the ASGI
            # server stops advancing the response generator, which leaves it suspended at
            # a yield nobody will return to -- so its own `finally` never runs, and a
            # teardown that lived only there would leak this run's registry entry and its
            # thread pool for the life of the process. Measured: still registered twenty
            # seconds after the socket was closed.
            try:
                for future in futures:
                    future.result()
            finally:
                inbox.put(None)
                with _cancels_lock:
                    _cancels.pop(run_id, None)
                pool.shutdown(wait=False)

        threading.Thread(target=watch, name="compare-watch", daemon=True).start()

        yield {
            "type": "started",
            "run_id": run_id,
            "config": config.as_dict(),
            "total_cells": total_cells,
        }

        while True:
            event = inbox.get()
            if event is None:
                break
            yield event
            if event["type"] == "result":
                finished += 1
                yield {"type": "progress", "completed": finished, "total": total_cells}

        # Everything below stays inside the `try` so that the cancel flag is still the
        # user's -- the `finally` sets it for its own reasons, and reading it after that
        # would report every run that merely had a model fall over as a cancelled one.
        # By this point `watch` has waited on every future, so no worker is still running.
        if finalize is not None and not stop.is_set():
            # Reaching here means somebody is still reading, so the run goes back on the
            # register for the length of the judging pass -- `watch` took it off when the
            # workers finished, and without this a Stop pressed while the judge is working
            # would find nothing to stop.
            with _cancels_lock:
                _cancels[run_id] = stop
            yield from finalize(comparison, stop)

        comparison.cancelled = stop.is_set()
        comparison.summaries = summarise(
            contenders, comparison.outcomes, warmups, errors, warm_ids
        )
        comparison.completed_at = now()
        comparison.total_duration_ms = int((time.perf_counter() - wall) * 1000)
        # Exactly one of these is ever emitted, and it is always the last event on the
        # stream: the browser treats it as the end of the run.
        yield {"type": "done", "comparison": comparison.as_dict()}
    finally:
        # Threads already inside a request finish that request; the flag stops them
        # starting another. Not waiting on them keeps a cancelled run -- or a browser
        # that navigated away -- from holding on for however long the current task needs.
        stop.set()
        pool.shutdown(wait=False)
        with _cancels_lock:
            _cancels.pop(run_id, None)

"""HTTP surface for comparing models against each other.

Thin by design: parse, validate, hand off. Every decision lives in the service.

Nothing here does authentication. ProfileSessionMiddleware in app/main.py gates every
/api path centrally, which is what stops a new router shipping unprotected by omission
-- and it matters here, because a comparison sends the user's own prompts to every model
they picked and the results describe what is set up on their machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.model_compare import runner
from app.services.model_compare.service import (
    DEFAULT_DEPTH,
    MAX_CONTENDERS,
    MAX_OUTPUT_TOKENS,
    MAX_RUBRIC_LENGTH,
    MAX_TEMPERATURE,
    MIN_CONTENDERS,
    MIN_OUTPUT_TOKENS,
    ModelCompareService,
)
from app.services.model_compare.tasks import MAX_CUSTOM_PROMPTS

#: How often to ask whether anyone is still reading. A second is far below the length
#: of any task, so nothing generates for long after the reader has gone, and the poll
#: itself costs nothing.
_DISCONNECT_POLL_SECONDS = 1.0

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/model-compare", tags=["model-compare"])


def _service() -> ModelCompareService:
    return ModelCompareService()


class ComparisonRequest(BaseModel):
    """One comparison, as the browser describes it.

    Every field that changes what is measured is here rather than in a server-side
    default, so that the run config written into the result is the whole truth about what
    produced it.
    """

    model_ids: list[str] = Field(min_length=MIN_CONTENDERS, max_length=MAX_CONTENDERS)
    use_case: str = "coding"
    depth: int = Field(default=DEFAULT_DEPTH, ge=1, le=12)
    #: Only read when use_case is "custom". One task per question, in order.
    prompts: list[str] = Field(default_factory=list, max_length=MAX_CUSTOM_PROMPTS)
    #: The model asked to rate the answers, or nothing for rule-based grading only.
    judge_id: str | None = None
    #: What the judge should weigh. Empty falls back to the published default rubric.
    judge_rubric: str = Field(default="", max_length=MAX_RUBRIC_LENGTH)
    #: Rule-based grading, where a task has a rule. Off, only the judge scores anything.
    deterministic: bool = True
    temperature: float = Field(default=0.0, ge=0, le=MAX_TEMPERATURE)
    #: A run-wide ceiling. Unset, each task keeps the cap it was written with.
    max_output_tokens: int | None = Field(
        default=None, ge=MIN_OUTPUT_TOKENS, le=MAX_OUTPUT_TOKENS
    )
    #: Let models reason out loud before answering. Off by default: under a per-task
    #: token cap a reasoning model spends the budget thinking and answers with nothing.
    allow_thinking: bool = False
    #: Supplied by the caller so it can cancel a run it has not had a reply from yet.
    run_id: str | None = Field(default=None, max_length=64)

    def as_kwargs(self) -> dict:
        return {
            "model_ids": self.model_ids,
            "use_case": self.use_case,
            "depth": self.depth,
            "prompts": self.prompts,
            "judge_id": self.judge_id,
            "judge_rubric": self.judge_rubric,
            "deterministic": self.deterministic,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "allow_thinking": self.allow_thinking,
        }


class PlanRequest(ComparisonRequest):
    """Same shape, but one model is enough to price a configuration."""

    model_ids: list[str] = Field(min_length=1, max_length=MAX_CONTENDERS)


class CancelRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)


class AddModelRequest(BaseModel):
    """One model the machine already has, to be set up so it can be compared."""

    model: str = Field(min_length=1, max_length=240)
    #: Which host serves it. Defaults to the Ollama address Neo is configured with.
    base_url: str = Field(default="", max_length=500)


def _handled(exc: Exception) -> HTTPException:
    """A refusal the user can act on, with the status code that fits its cause."""

    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/use-cases")
def use_cases() -> dict:
    """What models can be compared on, in the wording the screen offers it."""

    return _service().use_cases()


@router.get("/candidates")
def candidates() -> dict:
    """Every model Neo could put in a comparison, and whether it looks reachable."""

    return _service().candidates()


@router.post("/models")
def add_model(request: AddModelRequest) -> dict:
    """Set up a model the machine already has, and return the refreshed picker.

    Here rather than only in Settings because this is where its absence is felt: a
    comparison needs two models, and sending someone to another screen to add the second
    one is the whole feature failing at the last step.
    """

    try:
        return _service().add_model(request.model, request.base_url)
    except (LookupError, ValueError) as exc:
        raise _handled(exc) from exc


@router.post("/plan")
def plan(request: PlanRequest) -> dict:
    """What a run would cost, so the wait can be shown before it is committed to."""

    try:
        return _service().plan(**request.as_kwargs())
    except (LookupError, ValueError) as exc:
        raise _handled(exc) from exc


@router.post("/run")
async def run(http: Request, payload: ComparisonRequest) -> StreamingResponse:
    """Run the comparison, reporting each answer as it lands.

    Newline-delimited JSON rather than one response at the end. A comparison is tens of
    seconds of generation, and the point of streaming is not politeness: the grid fills
    in cell by cell, so the user can watch the evidence arrive and stop the run early
    rather than waiting for a result they had already seen enough of.

    Asynchronous only so that the browser going away can be *noticed*. The comparison
    itself is ordinary blocking work and runs in a worker thread; what the event loop
    does between events is ask whether anyone is still listening. Without that check a
    closed tab leaves every model generating to the end of the run, because a write to a
    socket nobody is reading does not fail promptly enough to stop anything.
    """

    service = _service()
    run_id = payload.run_id or str(uuid.uuid4())
    kwargs = {**payload.as_kwargs(), "run_id": run_id}
    # Validated before the stream opens. Inside a StreamingResponse an exception is a
    # half-written body with a 200 already on it, which the browser cannot tell from a
    # run that simply stopped.
    try:
        service.build(**kwargs)
    except (LookupError, ValueError) as exc:
        raise _handled(exc) from exc

    async def watch_for_disconnect() -> None:
        """Stop the run when the browser goes away.

        A task of its own rather than a check inside the loop below, because on a
        disconnect the ASGI server stops advancing the response generator entirely --
        so anything written between two yields is never reached. This keeps polling on
        the event loop regardless, which is what lets a closed tab actually stop the
        models instead of leaving them generating to the end of the run.
        """

        while run_id in runner.active_runs():
            if await http.is_disconnected():
                _LOG.debug("Comparison %s abandoned by its client", run_id)
                runner.cancel(run_id)
                return
            await asyncio.sleep(_DISCONNECT_POLL_SECONDS)

    async def events() -> AsyncIterator[bytes]:
        stream = service.stream(**kwargs)
        loop = asyncio.get_running_loop()
        sentry = asyncio.create_task(watch_for_disconnect())

        def pull():
            try:
                return next(stream)
            except StopIteration:
                return None

        try:
            while True:
                # The comparison itself is ordinary blocking work, so it runs in a worker
                # thread and the event loop stays free to notice a disconnect.
                event = await loop.run_in_executor(None, pull)
                if event is None:
                    return
                yield (json.dumps(event) + "\n").encode()
        finally:
            sentry.cancel()
            # Belt and braces. The runner tears itself down from its own watcher thread,
            # which is the path that survives a disconnect; this covers the ordinary case
            # where the response was read to the end or abandoned mid-iteration.
            stream.close()

    return StreamingResponse(events(), media_type="application/x-ndjson")


@router.post("/cancel")
def cancel(request: CancelRequest) -> dict:
    """Stop a run in progress.

    Aborting the browser's fetch alone does not stop it -- the models generate in
    server-side threads that have nothing to do with the connection to the browser, so
    they need their own signal to stop starting the next task.
    """

    return _service().cancel(request.run_id)

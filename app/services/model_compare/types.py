"""Value types for comparing models against each other.

Dataclasses rather than loose dicts, for the same reason the local-models feature uses
them: a ``TaskOutcome`` can be built directly in a test, which is what lets the grading
and the reporting be exercised without a model server anywhere near the suite.

Two things about the shape of this module are deliberate.

**There is no winner.** Nothing here names a best model, and there is no tie threshold,
no ranking and no place for one. A comparison reports what each model scored and how long
it took; which of those matters is a question about the reader's work, not about the
models, and the product is not in a position to answer it. Presentation may say "A scored
higher, B was faster" -- that is arithmetic on the numbers below, not a verdict stored
alongside them.

**A run records what produced it.** ``RunConfig`` carries the models and their versions,
the exact prompts, the generation settings, the grader version and the judge, so a result
can be read months later or set against another run. Nothing persists yet, but the
objects are shaped so that persistence is a store module and not a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

#: Bumped when the shape of a stored comparison changes. Written into every run so a
#: future history feature can read old records rather than discarding them.
SCHEMA_VERSION = 1

# What the user is comparing models *for*. The first four deliberately match the goal
# ids the local-models wizard already uses, so someone who picked "coding" there sees
# the same word here and it means the same thing.
UseCase = Literal["chat", "writing", "coding", "reasoning", "structured", "custom"]
USE_CASES: tuple[str, ...] = ("chat", "writing", "coding", "reasoning", "structured", "custom")

# How a single check came out. "skipped" is a real answer: a check that could not be
# applied (no code block to parse, so nothing to parse *for*) must not be scored as a
# pass, and must not be scored as a failure either.
CheckStatus = Literal["passed", "failed", "skipped"]

# Why a task run ended. Only "ok" carries a usable answer; the rest are shown in the
# grid in place of one, because a blank cell reads as "still running" forever.
OutcomeStatus = Literal["ok", "empty", "timed_out", "failed", "cancelled"]

# What a cell in the grid is doing right now. Reported as its own axis rather than
# inferred from whether an outcome exists, so "waiting its turn" and "generating" are
# distinguishable -- with several models and several tasks they are most of the run.
CellState = Literal["queued", "generating", "evaluating", "complete", "error", "cancelled"]

#: How an outcome was scored, or why it was not. Kept explicit so the interface can say
#: "no deterministic evaluation available" rather than showing a manufactured zero.
Evaluation = Literal["deterministic", "none"]


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class GenerationSettings:
    """The knobs that materially change what a comparison measures.

    Recorded on the run because they are part of the result: the same models over the
    same tasks at a different temperature, or with thinking switched on, are a different
    measurement and a result that does not say which it was cannot be trusted later.
    """

    #: Zero by default. A comparison wants the model's considered answer, and sampling
    #: noise between two runs of the same model would swamp the difference between two
    #: different ones.
    temperature: float = 0.0
    #: Overrides each task's own cap when set. Left unset, every task keeps the ceiling
    #: it was written with -- a one-word answer gets a small one, a function gets room.
    max_output_tokens: int | None = None
    #: Off by default. Under a per-task cap a reasoning model will spend the whole budget
    #: thinking and return nothing, which measures the cap rather than the model.
    allow_thinking: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "allow_thinking": self.allow_thinking,
        }


@dataclass(frozen=True)
class JudgeSettings:
    """The optional second opinion, and the instructions it was given."""

    model_id: str = ""
    display_name: str = ""
    model_version: str = ""
    #: What the judge was told to weigh. The default is in ``judge.py``; a user rubric
    #: replaces it wholesale, because a rubric appended to another rubric is neither.
    rubric: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.model_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "model_version": self.model_version,
            "rubric": self.rubric,
        }


@dataclass(frozen=True)
class CheckResult:
    """One graded property of one answer."""

    #: What was being checked, in words the user reads off the grid.
    label: str
    status: CheckStatus
    #: Shown only when it failed, and only when the reason is not obvious from the label.
    detail: str = ""
    #: Checks are not equally important. "Did the code parse" outweighs "was it short".
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class Task:
    """One question put to every contender.

    ``max_tokens`` is per task rather than global because the budget is the feature: a
    task whose right answer is one word gets a cap that makes a rambling answer *cost*
    something, and a task that asks for a function gets room to write one.
    """

    id: str
    use_case: str
    #: Shown as the row heading in the grid. A short noun phrase, not the prompt.
    label: str
    prompt: str
    system: str = ""
    max_tokens: int = 200
    #: Ordering within a pack, lowest first. The default depth takes the first few, so
    #: the most representative tasks carry the lowest ranks.
    rank: int = 0
    #: Applied to the answer, in order. Named rather than inline so the same check can
    #: be reused across tasks and so a failure names itself on screen.
    checks: tuple[Any, ...] = ()
    #: What a correct answer looks like, in a sentence. Shown when the user opens a cell,
    #: so the score can be argued with rather than taken on faith.
    rubric: str = ""
    #: An answer that must score full marks. Never sent anywhere and never serialised --
    #: it exists so the suite can assert, for every question in every pack, that a
    #: correct answer actually passes. With hundreds of generated questions that is the
    #: only way to know a grader has not quietly become impossible to satisfy.
    canonical: str = ""

    @property
    def gradeable(self) -> bool:
        """Whether anything here can be checked without asking another model."""

        return bool(self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "use_case": self.use_case,
            "label": self.label,
            "prompt": self.prompt,
            "system": self.system,
            "max_tokens": self.max_tokens,
            "gradeable": self.gradeable,
            "rubric": self.rubric,
            # The checks themselves are deliberately absent: the prompt is shown on
            # screen and the expected answers must not ride along with it.
            "check_count": len(self.checks),
        }


@dataclass(frozen=True)
class Contender:
    """One model in the comparison, as the registry knows it."""

    #: The LLM registry configuration id. This is what the run is keyed on.
    id: str
    #: What the user reads: the model tag, which is what actually distinguishes two
    #: registry entries that share a name.
    display_name: str
    provider: str
    model: str
    base_url: str
    #: True when this model runs on the user's own machine, which is what decides how
    #: many of them may be loaded at once.
    local: bool = True
    #: The exact build behind the tag -- digest, size, compression where the provider
    #: reports them. A tag like "latest" moves, so without this a result cannot say what
    #: it actually measured.
    version: str = ""
    #: The name of the registry entry, kept for when two entries share a model.
    config_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "model": self.model,
            "local": self.local,
            "version": self.version,
            "config_name": self.config_name,
        }


@dataclass(frozen=True)
class TaskOutcome:
    """What one model did with one task."""

    contender_id: str
    task_id: str
    status: OutcomeStatus
    state: CellState = "complete"
    #: The answer as the user sees it, thinking already stripped by the client.
    answer: str = ""
    #: What the model reasoned before answering, when it was allowed to and did.
    thinking: str = ""
    checks: tuple[CheckResult, ...] = ()
    #: 0..1, the weighted share of applicable checks that passed. None when there was no
    #: rule that could be applied -- which is the honest answer for a custom prompt, and
    #: is shown as such rather than as a zero.
    score: float | None = None
    #: How the score above was arrived at, or that it was not.
    evaluation: Evaluation = "none"
    #: Wall-clock from asking to the answer being complete.
    duration_ms: int = 0
    #: How long until the model produced anything at all. Separate from the total because
    #: they answer different questions: this one is how responsive it feels, the total is
    #: how long the work took.
    time_to_first_token_ms: int | None = None
    completion_tokens: int | None = None
    #: Generated before the answer, when the model reasons out loud. Counted separately
    #: because it is most of the wait on a thinking model and none of the answer.
    thinking_tokens: int | None = None
    tokens_per_second: float | None = None
    #: 0..1 from the optional judging pass. Kept apart from ``score`` rather than blended
    #: into it: one is a rule that can be read and argued with, the other is a model's
    #: opinion, and averaging them would hide which of the two is talking.
    judge_score: float | None = None
    judge_note: str = ""
    #: Set when status is not "ok", in plain language.
    message: str = ""
    finish_reason: str = ""
    started_at: str = ""

    @property
    def graded(self) -> bool:
        return self.score is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "contender_id": self.contender_id,
            "task_id": self.task_id,
            "status": self.status,
            "state": self.state,
            "answer": self.answer,
            "thinking": self.thinking,
            "checks": [check.as_dict() for check in self.checks],
            "score": round(self.score, 3) if self.score is not None else None,
            "evaluation": self.evaluation,
            "duration_ms": self.duration_ms,
            "time_to_first_token_ms": self.time_to_first_token_ms,
            "completion_tokens": self.completion_tokens,
            "thinking_tokens": self.thinking_tokens,
            "tokens_per_second": (
                round(self.tokens_per_second, 1) if self.tokens_per_second else None
            ),
            "judge_score": round(self.judge_score, 3) if self.judge_score is not None else None,
            "judge_note": self.judge_note,
            "message": self.message,
            "finish_reason": self.finish_reason,
            "started_at": self.started_at,
        }


@dataclass(frozen=True)
class ContenderSummary:
    """How one model did over the whole comparison.

    Every field is a measurement. There is deliberately no overall rank, no "best" flag
    and no combined figure of merit: the two numbers that matter pull in different
    directions, and collapsing them into one would be the product deciding for the reader
    which of the two their work cares about.
    """

    contender: Contender
    #: 0..1 over every deterministically graded task, or None when no rule applied.
    score: float | None
    #: Mean of the judging pass, when one was asked for. Never blended with ``score``.
    judge_score: float | None = None
    tasks_answered: int = 0
    tasks_attempted: int = 0
    tasks_graded: int = 0
    checks_passed: int = 0
    checks_applied: int = 0
    #: The middle task time rather than the mean: one cold task should not decide how
    #: fast a model looks.
    median_duration_ms: int = 0
    median_time_to_first_token_ms: int | None = None
    median_tokens_per_second: float | None = None
    total_duration_ms: int = 0
    total_completion_tokens: int = 0
    #: How long the model took to become ready, before any task was timed.
    warmup_ms: int | None = None
    #: Whether the model was already resident when the run started, where the provider
    #: says. A cold model's warm-up is excluded from every task time, but it is still
    #: part of what the user waited for.
    was_warm: bool | None = None
    #: Set when the model could not be reached or fell over part way through.
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "contender": self.contender.as_dict(),
            "score": round(self.score, 3) if self.score is not None else None,
            "judge_score": round(self.judge_score, 3) if self.judge_score is not None else None,
            "tasks_answered": self.tasks_answered,
            "tasks_attempted": self.tasks_attempted,
            "tasks_graded": self.tasks_graded,
            "checks_passed": self.checks_passed,
            "checks_applied": self.checks_applied,
            "median_duration_ms": self.median_duration_ms,
            "median_time_to_first_token_ms": self.median_time_to_first_token_ms,
            "median_tokens_per_second": (
                round(self.median_tokens_per_second, 1) if self.median_tokens_per_second else None
            ),
            "total_duration_ms": self.total_duration_ms,
            "total_completion_tokens": self.total_completion_tokens,
            "warmup_ms": self.warmup_ms,
            "was_warm": self.was_warm,
            "error": self.error,
        }


@dataclass(frozen=True)
class RunConfig:
    """Everything that produced a result.

    Written once when a run starts and never changed. This is the record a future history
    feature would store: with it, a result read back later says exactly which build of
    which model answered which prompt under which settings, and two runs can be set
    against each other honestly.
    """

    run_id: str
    use_case: str
    contenders: tuple[Contender, ...] = ()
    tasks: tuple[Task, ...] = ()
    generation: GenerationSettings = field(default_factory=GenerationSettings)
    judge: JudgeSettings = field(default_factory=JudgeSettings)
    #: Whether rule-based grading was asked for. Off, every task reports "not evaluated"
    #: rather than a score, even where a rule existed.
    deterministic: bool = True
    #: Which build of the rule-based grader ran. The rules are the score, so a result
    #: from a later version is not directly comparable with one from an earlier.
    grader_version: str = ""
    parallel: int = 1
    estimate_seconds: int = 0
    #: Which draw from the question pool this run got. Recorded because the questions are
    #: sampled at random: without it a result could not say what it actually asked, and
    #: two runs could never be set against each other on equal terms. Passing it back
    #: reproduces the exact set.
    seed: int | None = None
    started_at: str = field(default_factory=now)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "use_case": self.use_case,
            "contenders": [item.as_dict() for item in self.contenders],
            "tasks": [item.as_dict() for item in self.tasks],
            "generation": self.generation.as_dict(),
            "judge": self.judge.as_dict(),
            "deterministic": self.deterministic,
            "grader_version": self.grader_version,
            "parallel": self.parallel,
            "estimate_seconds": self.estimate_seconds,
            "seed": self.seed,
            "started_at": self.started_at,
            "schema_version": self.schema_version,
        }


@dataclass
class Comparison:
    """A finished run, assembled as the outcomes arrive.

    Note what is not here: no verdict, no winner, no ranking. The summaries carry the
    measurements and the reader draws the conclusion.
    """

    config: RunConfig
    outcomes: list[TaskOutcome] = field(default_factory=list)
    summaries: list[ContenderSummary] = field(default_factory=list)
    cancelled: bool = False
    #: Populated when a judging pass ran and produced ratings, so a rating on screen is
    #: always attributable. An unattributed number reads as Neo's own verdict.
    judged_by: str = ""
    completed_at: str = ""
    total_duration_ms: int = 0
    #: Anything that went wrong at the level of the run rather than one cell.
    errors: list[str] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.config.run_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.as_dict(),
            "outcomes": [item.as_dict() for item in self.outcomes],
            "summaries": [item.as_dict() for item in self.summaries],
            "cancelled": self.cancelled,
            "judged_by": self.judged_by,
            "completed_at": self.completed_at,
            "total_duration_ms": self.total_duration_ms,
            "errors": list(self.errors),
            # Convenience mirrors so a reader does not have to reach into the config.
            "run_id": self.config.run_id,
            "use_case": self.config.use_case,
            "schema_version": self.config.schema_version,
        }

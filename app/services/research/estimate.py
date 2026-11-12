"""How long a research run should take on this machine, refined while it runs.

A run's time is mostly model calls, and the number of calls follows from the mode and the
plan: planning, a few calls per research step, and the finishing calls every run pays for.
Each call takes what this model takes on this hardware. So the estimate is

    calls still to come x seconds per call  +  research steps still to come x search/read time

Before a run, seconds per call come from this machine's history with the model and the step
count from a typical plan for the mode. During a run they come from the run itself -- its own
calls, its actual plan, its own searching and reading -- so the estimate converges on what the
run is actually doing. It never exceeds the run's time limit, because the run stops there.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.services.provider_runtime.pace import ModelPace
from app.services.research.policy import ResearchPolicy
from app.services.research.types import Tier

#: A typical research call on this route: a page batch or plan in, a structured answer out.
TYPICAL_PROMPT_TOKENS = 1300
TYPICAL_OUTPUT_TOKENS = 500
#: Searching and reading for one step, before the run has measured its own.
STEP_SECONDS = 20.0
#: Calls a step makes beyond judging: choosing what to search and recording what it found.
STEP_OVERHEAD_CALLS = 2
#: This run's own calls replace the machine's history once there are this many.
RUN_SAMPLE_CALLS = 3


@dataclass(frozen=True)
class RunShape:
    """The model calls a run in one mode makes, whatever the machine."""

    planning_calls: int
    calls_per_step: int
    finishing_calls: int
    steps: int
    call_limit: int

    def total_calls(self) -> int:
        work = self.planning_calls + self.steps * self.calls_per_step + self.finishing_calls
        return min(self.call_limit, work)


def pursued_targets(policy: ResearchPolicy) -> int:
    """How many questions a typical plan in this mode pursues.

    Plans use fewer than the caps allow -- a simple question needs one -- so a typical plan is
    taken as two thirds of the pursued tiers' caps.
    """
    caps = policy.planning
    by_tier = {Tier.CORE: caps.core, Tier.SUPPORTING: caps.supporting, Tier.CONTEXT: caps.context}
    pursued = sum(n for tier, n in by_tier.items() if tier in policy.pursued_tiers)
    return max(1, round(pursued * 2 / 3))


def expected_steps(policy: ResearchPolicy, targets: int) -> int:
    """Research steps for ``targets`` questions: one pass over them, a follow-up for about
    half, and the plan widenings the mode allows without needing a cause."""
    per_step = max(1, policy.ceilings.targets_per_step)
    first = math.ceil(targets / per_step)
    follow_up = math.ceil(targets / per_step / 2)
    widening = 0 if policy.expansion_requires_cause else policy.expansion_calls
    return max(1, min(policy.ceilings.steps, first + follow_up + widening))


def shape(
    policy: ResearchPolicy,
    *,
    planning_calls: int,
    judge_calls: int,
    finishing_calls: int,
    call_limit: int,
    targets: int | None = None,
) -> RunShape:
    return RunShape(
        planning_calls=planning_calls,
        calls_per_step=STEP_OVERHEAD_CALLS + judge_calls,
        finishing_calls=finishing_calls,
        steps=expected_steps(policy, targets if targets is not None else pursued_targets(policy)),
        call_limit=call_limit,
    )


def seconds_per_call(pace: ModelPace | None) -> float | None:
    """A typical research call's duration on this machine, from its history with the model."""
    if pace is None:
        return None
    if pace.route_samples >= 3:
        # The mean, not the median: a run's time is its calls added up, and research mixes
        # short calls with long ones.
        return pace.budget_seconds()
    return pace.expected_seconds(TYPICAL_PROMPT_TOKENS, TYPICAL_OUTPUT_TOKENS)


def before_run(run: RunShape, pace: ModelPace | None, ceiling_seconds: float) -> int | None:
    """Expected seconds for a whole run, or None when this model has never been measured."""
    per_call = seconds_per_call(pace)
    if per_call is None:
        return None
    total = run.total_calls() * per_call + run.steps * STEP_SECONDS
    return int(min(ceiling_seconds, total))


def remaining(
    run: RunShape,
    *,
    calls_used: int,
    steps_done: int,
    planned: bool,
    finishing_calls_used: int | None,
    run_seconds_per_call: float,
    run_step_seconds: float | None,
    prior_seconds_per_call: float | None,
    seconds_left: float,
) -> int | None:
    """Expected seconds until a running job finishes, or None when there is nothing to go on.

    ``finishing_calls_used`` is None until the run starts finishing. ``seconds_left`` is the
    time before the run's deadline, which the estimate never exceeds.
    """
    per_call = (
        run_seconds_per_call
        if calls_used >= RUN_SAMPLE_CALLS or prior_seconds_per_call is None
        else prior_seconds_per_call
    )
    if not per_call:
        return None
    if finishing_calls_used is not None:
        calls, steps = max(0, run.finishing_calls - finishing_calls_used), 0
    else:
        planning = 0 if planned else max(0, run.planning_calls - calls_used)
        steps = max(0, run.steps - steps_done)
        calls = planning + steps * run.calls_per_step + run.finishing_calls
    calls = min(calls, max(0, run.call_limit - calls_used))
    step_seconds = run_step_seconds if run_step_seconds is not None else STEP_SECONDS
    return int(max(0.0, min(seconds_left, calls * per_call + steps * step_seconds)))

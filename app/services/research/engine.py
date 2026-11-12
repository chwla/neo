"""The research engine: the one pipeline behind every Research Mode entry point.

plan → [act on evidence needs: search → triage → read → judge → grade → evidence map]* →
claims → verify → confidence → report. The mode supplies a policy; it never selects a different
path. Each stage is its own module; this file orders them, admits each step only when the run
can afford it, and records the trace. Dependencies are injected so the whole pipeline runs
against fakes in tests.
"""

from __future__ import annotations

import copy
import logging
import math
import re
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from pydantic import BaseModel

from app.core.config import get_settings
from app.services.disclosure import Destination, Label, Labeled, join, label_of, require, to_json
from app.services.provider_runtime import pace as model_pace
from app.services.research import estimate as run_estimate
from app.services.research import stopping, strategy
from app.services.research.authority import assess_sources, claim_fit
from app.services.research.capability import ModelCapability, resolve_capability, work_scale
from app.services.research.claims import build_claims, build_comparison, extractive_claims
from app.services.research.confidence import assess as assess_confidence
from app.services.research.evidence import (
    JUDGE_OUTPUT_TOKENS_PER_CHUNK,
    EvidencePool,
    detect_contradictions,
    judge_candidates,
    resolve_restatements,
    select_candidates,
)
from app.services.research.evidence_map import EvidenceMap
from app.services.research.extraction import (
    extract_chunks,
    near_duplicate_pairs,
    repeated_block_hashes,
)
from app.services.research.llm_json import (
    STAGE_FAILURES,
    LLMBudget,
    LLMFactory,
    ResearchCancelled,
    ResearchModelUnavailable,
    StageProfile,
    call_json,
)
from app.services.research.planner import (
    PlanningFailed,
    expand,
    may_expand,
    plan_research,
    planning_call_budget,
)
from app.services.research.policy import POLICIES
from app.services.research.report import render, render_failure
from app.services.research.report_depth import REPORT_DEPTH
from app.services.research.retrieval import (
    SHARED_PAGE_CACHE,
    FetchedSource,
    PageCache,
    PageFetcher,
    Retriever,
    SearchProvider,
)
from app.services.research.textutil import canonicalize_url, host_group, host_of
from app.services.research.types import (
    Action,
    Chunk,
    CompletionStatus,
    ContradictionGroup,
    ContradictionStatus,
    DepthMode,
    EvidenceItem,
    EvidenceTarget,
    Intent,
    LeadStatus,
    OriginLead,
    Provenance,
    RejectionReason,
    RejectionRecord,
    ResearchMetrics,
    ResearchOutcome,
    ResearchPlan,
    ResearchSource,
    ResearchTrace,
    StageTiming,
    StopReason,
    Sufficiency,
    SupportStatus,
    Tier,
)
from app.services.research.verify import audit, label_strengths, merge_verified

logger = logging.getLogger(__name__)

_MAX_EMBEDDINGS_PER_RUN = 240
_JUDGE_BATCH = 10
#: Finishing calls a run with findings is always given time for: claim synthesis and the summary.
_GUARANTEED_FINISHING_CALLS = 2
#: Chunks judged for a target that is still being established, and for one being improved.
_CHUNKS_OPEN, _CHUNKS_IMPROVING = 6, 4
_PROVENANCE_ORDER = {Provenance.A: 0, Provenance.B: 1, Provenance.C: 2, Provenance.D: 3, None: 4}
_STOP_OF_COMPLETION = {
    CompletionStatus.COMPLETED: StopReason.COVERAGE_SUFFICIENT,
    CompletionStatus.COMPLETED_WITH_GAPS: StopReason.INSUFFICIENT_EVIDENCE,
    CompletionStatus.CEILING_REACHED: StopReason.BUDGET_EXHAUSTED,
    CompletionStatus.BLOCKED: StopReason.SEARCH_UNAVAILABLE,
    CompletionStatus.CANCELLED: StopReason.USER_CANCELLED,
    CompletionStatus.FAILED: StopReason.FAILED,
}


#: Providers that run on this machine, where a first call may first have to load the model.
_LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp", "llama_cpp", "lmstudio", "local"})
#: Time a run keeps past its research limit to finish: claims, the audit and the summary.
#: At least this many seconds, whatever the measured pace says.
_MIN_FINISHING_SECONDS = 120.0
#: Calls the finishing allowance is sized for.
_FINISHING_CALLS = 3
#: What each engine stage is called when a run says where its time limit was reached.
_STAGE_NAMES = {
    "planning": "planning",
    "searching": "searching",
    "fetching": "searching",
    "judging": "judging",
    "coverage": "judging",
    "synthesizing": "claims",
    "verifying": "verification",
    "reporting": "report",
}


class _Ping(BaseModel):
    ok: bool = True


def machine_hint(capability: ModelCapability) -> model_pace.ModelPace | None:
    """An estimated pace for a local model this machine has never run, or None.

    Before a model has answered here there is no measurement, and the mode's limits were
    written for a GPU. On a machine without one, the model's size and the memory bandwidth
    give a rough speed -- enough to size the first run's time limit so it is not cut short.
    """
    if capability.provider.lower() not in _LOCAL_PROVIDERS:
        return None
    try:
        from app.services.local_models import probe, sizing

        machine = probe.detect()
        if machine.has_gpu and machine.accelerator != "cpu":
            return None
        match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", capability.model.lower())
        parameters = float(match.group(1)) if match else 7.0
        bytes_read = max(0.3, parameters) * 0.6e9  # a 4-bit quantised model
        decode = sizing.bandwidth_gbps(machine) * sizing.DECODE_EFFICIENCY * 1e9 / bytes_read
        decode = max(1.0, min(decode, 40.0))
        prefill = decode * 6
        typical = 10.0 + 1300 / prefill + 500 / decode
        return model_pace.ModelPace(
            model=capability.model,
            samples=0,
            decode_tps=decode,
            prefill_tps=prefill,
            load_seconds=10.0,
            call_seconds=typical,
            route_samples=0,
            mean_call_seconds=typical,
        )
    except Exception:
        logger.debug("Research could not estimate this machine's speed", exc_info=True)
        return None


@dataclass
class ProgressUpdate:
    stage: str
    percent: int
    step: str
    message: str
    metrics: ResearchMetrics
    plan: ResearchPlan | None = None
    sources: list[ResearchSource] | None = None
    evidence: list[EvidenceItem] | None = None
    queries: list[str] | None = None
    #: What the user sees the run doing, independent of internal stage names.
    phase: str = ""
    #: Core questions established so far, of how many, and the run's clock against its cap.
    core_established: int = 0
    core_targets: int = 0
    elapsed_seconds: int = 0
    ceiling_seconds: int = 0
    #: Expected seconds until the run finishes, at this machine's pace; None when unknown.
    remaining_seconds: int | None = None


ProgressCallback = Callable[[ProgressUpdate], None]


@dataclass
class EngineDependencies:
    llm_factory: LLMFactory
    providers: list[SearchProvider]
    fetcher: PageFetcher
    #: What the routed model can do. Resolved from Neo's configured route by default.
    capability: Callable[[], ModelCapability] = resolve_capability
    cache: PageCache | None = SHARED_PAGE_CACHE
    clock: Callable[[], float] = time.monotonic
    today: Callable[[], date] = field(default_factory=lambda: lambda: datetime.now(UTC).date())
    embed: Callable[[str], list[float]] | None = None
    max_fetch_workers: int = 4
    fetch_retries: int = 1


def default_dependencies(route_name: str = "research") -> EngineDependencies:
    from app.services.llm import get_llm_client
    from app.services.search.content import WebPageFetcher
    from app.services.search.providers import ProviderRegistry

    settings = get_settings()

    def llm_factory(num_predict: int):
        return get_llm_client(num_predict=num_predict, route_name=route_name, disable_thinking=True)

    return EngineDependencies(
        llm_factory=llm_factory,
        capability=lambda: resolve_capability(route_name),
        providers=[p for p in ProviderRegistry().chain() if getattr(p, "name", "") != "disabled"],
        fetcher=WebPageFetcher(
            timeout_seconds=settings.research_fetch_timeout_seconds,
            user_agent=settings.research_fetch_user_agent,
            read_documents=True,
            truncate_oversized=True,
        ),
        embed=_embedding_function(),
        max_fetch_workers=settings.research_max_fetch_workers,
        fetch_retries=settings.research_fetch_retries,
    )


def finishing_calls(depth: DepthMode) -> int:
    """Model calls a run reserves for finishing, before the capability work scale."""
    policy = POLICIES[depth]
    # Claims, the entailment audit, a possible rewrite of what it weakens, and the summary
    # are always paid for; stricter verification reserves more.
    reserved = 4 + (policy.entailment != "core_decisive") + policy.union_check
    # Richer presentation needs generation and audit calls after the ordinary finish.
    # Reserve them before research, without raising any ceiling or withholding calls
    # from mandatory verification. If verification needs them, the report falls back.
    return reserved + REPORT_DEPTH[depth].writing_reserve


def time_estimates(route_name: str = "research") -> dict:
    """How long a run in each mode should take on this machine, before it starts.

    From the routed model's measured pace here and a typical plan for each mode. A mode's
    ``seconds`` is None when the model has no history on this machine yet: there is nothing
    honest to estimate from until it has answered once.
    """
    try:
        capability = resolve_capability(route_name)
    except Exception:
        capability = ModelCapability()
    measured = model_pace.measured(capability.model, route_name)
    # A model never run here is estimated from the hardware, as a run's own limit is.
    estimated = measured is None and (hint := machine_hint(capability)) is not None
    if estimated:
        measured = hint
    scale = work_scale(
        capability, reference_items=_JUDGE_BATCH, output_per_item=JUDGE_OUTPUT_TOKENS_PER_CHUNK
    )
    modes = {}
    for depth in DepthMode:
        policy = POLICIES[depth]
        planning = planning_call_budget(depth, capability)
        judge = policy.ceilings.judge_calls_per_step * scale
        reserved = finishing_calls(depth) * scale
        run = run_estimate.shape(
            policy,
            planning_calls=planning,
            judge_calls=judge,
            finishing_calls=reserved,
            call_limit=policy.ceilings.model_calls * scale,
        )
        # Sized exactly as a run sizes its own limit, so the estimate and the run agree.
        ceiling = model_pace.scaled_ceiling(
            policy.ceilings.seconds, measured, calls=run.total_calls(),
            extra_seconds=run.steps * run_estimate.STEP_SECONDS,
        )
        modes[depth.value] = {
            "seconds": run_estimate.before_run(run, measured, ceiling),
            "ceiling_seconds": int(ceiling),
            # How many questions of each priority the mode researches, so an edited plan can
            # say what it will drop before the run drops it.
            "caps": {
                "core": policy.planning.core,
                "supporting": policy.planning.supporting,
                "context": policy.planning.context,
            },
        }
    return {
        "model": capability.model,
        "measured": measured is not None and not estimated,
        "estimated": estimated,
        "modes": modes,
    }


def capability_of(deps: EngineDependencies) -> ModelCapability:
    """What the routed model can do, or conservative limits when the registry cannot say."""
    try:
        return deps.capability()
    except Exception:  # a profile without a registry must still be able to research
        logger.warning("Research could not read the configured model route", exc_info=True)
        return ModelCapability()


def _embedding_function() -> Callable[[str], list[float]] | None:
    """Neo's configured embedding route, when it has one; semantic ranking is optional."""
    try:
        from app.services.embeddings import configured_embedder

        provider = configured_embedder()
        return provider.embed if provider else None
    except Exception:
        return None


class _Semantic:
    """Optional embedding similarity, used only as one candidate-ranking signal."""

    def __init__(self, embed: Callable[[str], list[float]] | None) -> None:
        self.embed = embed
        self.vectors: dict[str, list[float]] = {}
        self.enabled = embed is not None

    def scores(self, target: EvidenceTarget, chunks: list[Chunk]) -> dict[str, float] | None:
        if not self.enabled or self.embed is None:
            return None
        try:
            query_vector = self._vector(f"target:{target.id}", target.text)
            scores: dict[str, float] = {}
            for chunk in chunks:
                if chunk.id not in self.vectors and len(self.vectors) >= _MAX_EMBEDDINGS_PER_RUN:
                    continue
                vector = self._vector(chunk.id, chunk.text[:2000])
                scores[chunk.id] = max(0.0, _cosine(query_vector, vector))
            return scores
        except Exception:
            self.enabled = False
            return None

    def _vector(self, key: str, text: str) -> list[float]:
        if key not in self.vectors:
            self.vectors[key] = self.embed(text)  # type: ignore[misc]
        return self.vectors[key]


def plan_preview(
    query: str,
    depth: DepthMode,
    *,
    deps: EngineDependencies | None = None,
    context: str = "",
    cancelled: Callable[[], bool] | None = None,
) -> ResearchPlan:
    """The plan a run would start from, made without searching or reading anything.

    Raises ``ResearchModelUnavailable`` exactly as a run's planning would; a planner that
    answers unusably yields the plan built from the query itself, as a run would use.
    """
    deps = deps or default_dependencies()
    capability = capability_of(deps)
    budget = LLMBudget(
        planning_call_budget(depth, capability), 0, capability=capability, cancelled=cancelled
    )
    return plan_research(query, depth, llm_factory=deps.llm_factory, budget=budget, context=context)


def run_research(
    query: str | Labeled,
    depth: DepthMode,
    *,
    deps: EngineDependencies | None = None,
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
    context: str = "",
    run_id: str | None = None,
    plan: ResearchPlan | None = None,
) -> ResearchOutcome:
    """Research ``query``. ``plan``, when given, is a checked plan to research as it stands --
    one the person edited in the preview -- so the run does not plan its own."""
    source = label_of(query)
    query = query.value if isinstance(query, Labeled) else query
    if context:
        source = join(source, Label.unknown("untracked_research_context"))
    # Planning itself is a worker boundary, not a declassification step. Reject
    # unknown/private inputs before constructing models or retrieving evidence.
    require("research", Labeled(query, source), Destination("research"))
    deps = deps or default_dependencies()
    run = _Run(
        query,
        depth,
        deps,
        progress,
        cancelled or (lambda: False),
        context,
        run_id or uuid.uuid4().hex[:12],
        source_label=source,
        plan=plan,
    )
    outcome = run.execute()
    outcome.source_label_json = to_json(source)
    return outcome


class _Run:
    def __init__(
        self,
        query,
        depth,
        deps,
        progress,
        cancelled,
        context,
        run_id,
        *,
        source_label: Label | None = None,
        plan: ResearchPlan | None = None,
    ) -> None:
        #: A plan given in advance, which replaces planning; None plans as usual.
        self.given_plan = plan
        self.query = query
        self.source_label = label_of(source_label)
        self.depth = depth
        self.deps = deps
        self.progress = progress
        self.cancelled = cancelled
        self.context = context
        self.policy = POLICIES[depth]
        self.capability = capability_of(deps)
        self.profile = StageProfile(deps.clock)
        # A model that fits less work into one answer needs more answers for the same
        # research. Scaling the call allowance keeps the evidence, at the cost of time.
        scale = work_scale(
            self.capability,
            reference_items=_JUDGE_BATCH,
            output_per_item=JUDGE_OUTPUT_TOKENS_PER_CHUNK,
        )
        self.scale = scale
        #: Judge calls a step may make; a model answering less per call needs more calls.
        self.judge_calls = self.policy.ceilings.judge_calls_per_step * scale
        reserved = finishing_calls(depth)
        self.started = deps.clock()
        # What the run is expected to spend: planning, its research steps, and finishing. The
        # step count is a typical plan's (or the given plan's) until the real plan exists.
        self.shape = run_estimate.shape(
            self.policy,
            planning_calls=self._planning_calls(depth),
            judge_calls=self.judge_calls,
            finishing_calls=reserved * scale,
            call_limit=self.policy.ceilings.model_calls * scale,
            targets=self._given_targets(),
        )
        # The mode's time limit covers the whole run, planning and the final report included.
        # It was set for a model answering in seconds. Where this machine has been measured
        # answering slower -- a CPU-only machine, a large model on a small GPU -- the limit
        # grows to fit the mode's whole work at the measured pace: the mode decides how much
        # research is done, the machine only how long it takes. Measured fast, or not measured
        # yet, it is the mode's own limit.
        self.ceiling_seconds = self._ceiling_seconds()
        self.deadline = self.started + self.ceiling_seconds
        #: Past this nothing more is asked of the model; the run finishes deterministically.
        self.hard_limit_seconds = self._hard_limit_seconds()
        #: The stage running when the time limit was reached, once it has been.
        self.deadline_stage: str | None = None
        self.stage = "planning"
        self.fallbacks: list[dict[str, str]] = []
        self._prior_seconds_per_call = run_estimate.seconds_per_call(self._pace)
        #: Model calls already spent when finishing began; None while still researching.
        self._finishing_from: int | None = None
        self.llm_budget = LLMBudget(
            self.policy.ceilings.model_calls * scale,
            reserved * scale,
            capability=self.capability,
            profile=self.profile,
            clock=deps.clock,
            deadline=self.deadline,
            cancelled=cancelled,
            source_label=self.source_label,
        )
        self.llm_budget.hard_deadline = self.started + self.hard_limit_seconds
        self.trace = ResearchTrace(run_id=run_id, query=query, depth=depth)
        self.trace.model_route = {
            "route_name": self.capability.route_name,
            "provider": self.capability.provider,
            "model": self.capability.model,
            "context_window": self.capability.context_window,
            "max_output_tokens": self.capability.max_output_tokens,
            "capability_known": self.capability.known,
            "call_allowance_scale": scale,
        }
        if scale > 1:
            self.trace.errors.append(
                f"the configured research model returns at most "
                f"{self.capability.max_output_tokens} tokens per answer, so this run used "
                f"{scale}x more, smaller model calls to cover the same evidence"
            )
        if self.ceiling_seconds > self.policy.ceilings.seconds:
            self.trace.errors.append(
                f"{self.capability.model} takes about {round(self._pace.budget_seconds())} s "
                f"per answer on this machine, so this run's time limit was raised from "
                f"{self.policy.ceilings.seconds // 60} to {round(self.ceiling_seconds / 60)} "
                f"minutes to fit this mode's research"
            )
        self.plan: ResearchPlan | None = None
        self.emap: EvidenceMap | None = None
        self.retriever: Retriever | None = None
        self.sources: list[ResearchSource] = []
        self.evidence: list[EvidenceItem] = []
        self.groups: list[ContradictionGroup] = []
        self.leads: list[OriginLead] = []
        self.pool = EvidencePool()
        self.fingerprints: dict[str, set[int]] = {}
        self.semantic = _Semantic(deps.embed)
        self.roles: dict = {}
        self.pages_by_site: dict[str, list] = defaultdict(list)
        self.loop_end = ""
        self.stop_detail = ""
        self.steps = 0
        self.expansions = 0
        #: Why a run that ran out of pages could not read on, once it has.
        self._read_limit_detail = "page-read limit reached"

    def _ceiling_seconds(self) -> float:
        """The run's time limit: the mode's own, or what this mode's work takes on this machine.

        The work is the run's expected shape -- planning, its research steps, finishing --
        counted at this machine's measured time per call for the routed model, plus searching
        and reading. ``model_pace`` keeps the result at or above the mode's limit and below its
        safety cap.
        """
        self._pace = model_pace.measured(
            self.capability.model, self.capability.route_name or "research"
        )
        self._pace_source = "measured" if self._pace is not None else "none"
        if self._pace is None:
            # Never run here: estimate from the hardware rather than assume a GPU.
            self._pace = machine_hint(self.capability)
            if self._pace is not None:
                self._pace_source = "estimated"
        return model_pace.scaled_ceiling(
            self.policy.ceilings.seconds,
            self._pace,
            calls=self.shape.total_calls(),
            extra_seconds=self.shape.steps * run_estimate.STEP_SECONDS,
        )

    def _hard_limit_seconds(self) -> float:
        """The research limit plus time to finish, never past the run safety cap.

        Fixed when the run starts. Finishing is sized for long answers (writing claims is
        one) at this machine's pace, so the write-up that the research limit leaves room for
        always fits inside it.
        """
        if self._pace is not None:
            long_call = self._pace.expected_seconds(3000, 2000) or 0.0
            per_call = max(self._pace.budget_seconds(), long_call)
        else:
            per_call = 120.0
        finishing = max(_MIN_FINISHING_SECONDS, _FINISHING_CALLS * per_call * 1.5)
        return min(float(model_pace.MAX_RUN_SECONDS), self.ceiling_seconds + finishing)

    def _planning_calls(self, depth: DepthMode) -> int:
        return 0 if self.given_plan is not None else planning_call_budget(depth, self.capability)

    def _given_targets(self) -> int | None:
        """How many questions a given plan pursues; None until a plan exists."""
        if self.given_plan is None:
            return None
        return sum(1 for t in self.given_plan.top_targets() if t.tier in self.policy.pursued_tiers)

    # --- orchestration -----------------------------------------------------------------

    def execute(self) -> ResearchOutcome:
        try:
            if self.given_plan is not None:
                self.plan = self.given_plan
            if not self.deps.providers:
                # Nothing to search with: say so now, before any model time is spent.
                self.loop_end, self.stop_detail = "blocked", "no web search provider is enabled"
                return self._blocked()
            failure = self._preflight_model()
            if failure:
                return self._failure(failure)
            unavailable = self._probe_search()
            if unavailable:
                self.loop_end = "blocked"
                self.stop_detail = f"web search is unavailable ({unavailable})"
                return self._blocked()
            if self.given_plan is not None:
                self._emit("planning", 4, "Using your plan", "Researching the plan you reviewed")
                self.plan = self.given_plan
            else:
                self._emit("planning", 4, "Planning", "Working out what needs to be established")
                self.plan = plan_research(
                    self.query,
                    self.depth,
                    llm_factory=self.deps.llm_factory,
                    budget=self.llm_budget,
                    context=self.context,
                )
            for issue in self.plan.issues:
                if issue.code == "fallback_plan":
                    self.fallbacks.append({"stage": "planning", "reason": issue.detail})
            if self.llm_budget.model_down:
                return self._failure(
                    f"The research model is unavailable ({self.llm_budget.down_reason})."
                )
            self.trace.plan_issues = self.plan.issues
            self.emap = EvidenceMap(self.plan, self.policy)
            top = self.plan.top_targets()
            # The real plan replaces the typical one in the time estimate.
            self.shape = run_estimate.shape(
                self.policy,
                planning_calls=self.shape.planning_calls,
                judge_calls=self.judge_calls,
                finishing_calls=self.shape.finishing_calls,
                call_limit=self.shape.call_limit,
                targets=sum(1 for t in top if t.tier in self.policy.pursued_tiers),
            )
            self._emit(
                "planning",
                10,
                "Plan ready",
                f"{sum(t.tier == Tier.CORE for t in top)} core questions to establish",
                plan=self.plan,
            )
            self._check_deadline()
            if self.deadline_stage is None:
                self._research_loop()
            else:
                self.loop_end = "ceiling"
                self.stop_detail = f"time limit reached during {self.deadline_stage}"
                self.emap.update(self.evidence, self.sources, self.groups, self.leads)
            return self._synthesize()
        except ResearchCancelled:
            return self._outcome(CompletionStatus.CANCELLED, "cancelled by user", report_text="")
        except ResearchModelUnavailable as exc:
            return self._failure(self._unavailable_message(exc))
        except PlanningFailed as exc:
            self.trace.plan_issues = exc.issues
            return self._failure(str(exc))
        except Exception as exc:  # the trace keeps the class name, never a stack or secrets
            logger.exception("Research run %s failed", self.trace.run_id)
            return self._failure(f"Research failed unexpectedly ({exc.__class__.__name__}).")

    # --- preflight ---------------------------------------------------------------------

    def _unavailable_message(self, exc: Exception) -> str:
        provider = self.capability.provider or "the model provider"
        hint = (
            f" Check that {provider} is running and that {self.capability.model or 'the model'}"
            " is installed, then try again."
            if getattr(exc, "unreachable", False)
            else ""
        )
        return f"The research model is unavailable ({exc}).{hint}"

    def _preflight_model(self) -> str:
        """Wake a local model before planning; report an unreachable one now, not in an hour.

        A local model's first call may load it into memory, which on a slow disk takes minutes.
        One tiny call does that while nothing else waits on it, and a server that is not
        running at all is found before any searching is done. A slow answer is not a failure.
        """
        if self.capability.provider.lower() not in _LOCAL_PROVIDERS:
            return ""
        self._emit("planning", 2, "Loading model", "Waking the research model")
        ping = LLMBudget(
            1, 0, capability=self.capability, profile=self.profile, clock=self.deps.clock,
            cancelled=self.cancelled, source_label=self.source_label,
        )
        try:
            call_json(
                self.deps.llm_factory,
                stage="warmup",
                instructions='Reply with {"ok": true}.',
                payload={"ping": True},
                schema=_Ping,
                budget=ping,
                num_predict=8,
            )
        except ResearchModelUnavailable as exc:
            if exc.unreachable:
                return self._unavailable_message(exc)
            self.trace.errors.append(f"the model was slow to answer its first call ({exc})")
        except STAGE_FAILURES:
            pass
        return ""

    def _probe_search(self) -> str:
        """One search before any model time is spent; the reason it failed, or ""."""
        from app.services.search.providers import WebSearchProvider

        problems: list[str] = []
        for provider in self.deps.providers:
            try:
                if isinstance(provider, WebSearchProvider):
                    provider = copy.copy(provider)
                    provider.source_label = self.source_label
                response = provider.search(self.query, 3)
            except Exception as exc:  # each provider may fail on its own
                problems.append(f"{getattr(provider, 'name', 'search')}: {exc.__class__.__name__}")
                continue
            error = (response.error or "").lower()
            if response.results or not error or "no results" in error:
                return ""
            problems.append(f"{getattr(provider, 'name', 'search')}: {response.error[:80]}")
        return "; ".join(problems) or "no search provider answered"

    def _check_cancelled(self) -> None:
        if self.cancelled():
            raise ResearchCancelled("research cancelled")

    def _check_deadline(self) -> None:
        """Record the stage that was running when the time limit passed, the first time."""
        if self.deadline_stage is None and self.deps.clock() >= self.llm_budget.deadline:
            self.deadline_stage = _STAGE_NAMES.get(self.stage, self.stage)

    def _research_loop(self) -> None:
        assert self.plan is not None and self.emap is not None
        ceilings = self.policy.ceilings
        self.retriever = Retriever(
            plan=self.plan,
            max_searches=ceilings.searches,
            max_reads=ceilings.reads,
            providers=self.deps.providers,
            fetcher=self.deps.fetcher,
            trace=self.trace,
            cache=self.deps.cache,
            clock=self.deps.clock,
            # On the first step, searching and reading are paid for like the first model
            # calls are: a run that looked at nothing has nothing to report.
            has_time=lambda: (
                not self._cancelled_now() and self.llm_budget.has_time(reserved=self.steps <= 1)
            ),
            max_workers=self.deps.max_fetch_workers,
            fetch_retries=self.deps.fetch_retries,
            source_label=self.source_label,
        )
        while True:
            step = self.steps + 1
            self._check_cancelled()
            self._check_deadline()
            if self.deadline_stage is not None:
                self.loop_end = "ceiling"
                self.stop_detail = f"time limit reached during {self.deadline_stage}"
                break
            if self.llm_budget.model_down:
                self.loop_end = "ceiling"
                self.stop_detail = (
                    f"stopped early: the research model stopped responding "
                    f"({self.llm_budget.down_reason})"
                )
                break
            if step == 1:
                actions = strategy.first_actions(self.emap, self.policy)
            else:
                affordable, limit = self._can_afford_step(step)
                decision = stopping.decide(
                    self.emap,
                    self.policy,
                    self.leads,
                    self.groups,
                    can_afford_step=affordable,
                    limiting_ceiling=limit,
                    search_unavailable=self.retriever.search_unavailable,
                    any_evidence=any(item.status == "accepted" for item in self.evidence),
                )
                if decision.stop:
                    if decision.loop_end == "saturated" and self._expand():
                        continue
                    self.loop_end, self.stop_detail = decision.loop_end, decision.detail
                    break
                actions = strategy.next_actions(
                    self.emap,
                    self.policy,
                    step,
                    self.leads,
                    self.groups,
                    ceilings.targets_per_step,
                )
                if not actions:
                    # Nothing worth doing is the same answer however it was reached, so this
                    # path ends the loop exactly as a saturated decision does.
                    if self._expand():
                        continue
                    self.loop_end = "saturated"
                    self.stop_detail = "no further search this depth allows could add to the answer"
                    break
            self.steps = step
            self._run_step(step, actions)
            self._check_deadline()
            if (
                step == 1
                and self.retriever.search_unavailable
                and not any(s.reviewed for s in self.sources)
            ):
                self.loop_end, self.stop_detail = "blocked", "every search provider call failed"
                break
        stopping.settle_targets(self.emap, self.policy, self.leads, self.groups)

    def _expand(self) -> bool:
        """Ask for the targets the evidence itself showed were missing, once a plan is answered.

        Only a mode that allows it, only while the run can afford it, and only when what was
        read raises something: the answer is otherwise finished.
        """
        assert self.plan is not None and self.emap is not None
        if not may_expand(self.plan, self.policy, self.expansions, self.groups):
            return False
        if not self.llm_budget.can_afford(2 + self.judge_calls):
            return False
        self.expansions += 1
        self._emit("planning", 70, "Widening the plan", "Following up what the evidence raised")
        added = expand(
            self.plan,
            self.emap.summary(),
            self.depth,
            llm_factory=self.deps.llm_factory,
            budget=self.llm_budget,
            trace=self.trace,
        )
        if not added:
            return False
        self.emap.add_targets(added)
        self.emap.update(self.evidence, self.sources, self.groups, self.leads)
        return True

    def _run_step(self, step: int, actions: list[Action]) -> None:
        assert self.plan is not None and self.emap is not None and self.retriever is not None
        leads_by_id = {lead.id: lead for lead in self.leads}
        base = 10 + int(60 * min(1.0, (step - 1) / max(1, self.policy.ceilings.steps)))
        phase = _phase_of(actions)
        self._emit(
            "searching",
            base,
            _PHASE_LABEL[phase],
            _step_message(phase, actions),
            phase=phase,
        )
        tried: dict[str, list[str]] = defaultdict(list)
        for done in self.trace.actions:
            if done.query:
                tried[done.target_id].append(done.query)
        # A run that plans but never looks is worth nothing, so the first step is paid for out
        # of the same reserve the report is: after that, the reserve protects the finish.
        first = step == 1
        strategy.write_queries(
            actions,
            self.plan,
            self.emap,
            self.leads,
            tried=tried,
            used_hosts=sorted({s.host for s in self.sources if s.reviewed}),
            llm_factory=self.deps.llm_factory,
            budget=self.llm_budget,
            trace=self.trace,
            reserved=first,
        )
        before = {a.target_id: list(self.emap.state(a.target_id).fingerprint) for a in actions}
        self._check_cancelled()
        attempts_before = len(self.trace.search_attempts)
        searched = self.retriever.run_actions(actions, leads_by_id)
        self._check_cancelled()
        # An attempt counts once it searched (or followed a cited record). One that could not
        # search at all -- no time, every provider failing -- leaves the allowance unspent.
        ran = {
            attempt.task_id
            for attempt in self.trace.search_attempts[attempts_before:]
            if attempt.status in ("useful", "weak", "empty", "skipped_duplicate")
        }
        for action in actions:
            followed = action.intent == Intent.FOLLOW_LEAD and bool(
                (lead := leads_by_id.get(action.lead_id or "")) and lead.href
            )
            if action.id in ran or followed:
                self.emap.record_attempt(action.target_id, action.intent)
            self.trace.actions.append(action)
        self._add_leads(searched.leads)
        leads_by_id = {lead.id: lead for lead in self.leads}
        targets = list(dict.fromkeys(action.target_id for action in actions))
        reads = min(self.retriever.fetches_remaining, max(3, 2 * len(targets)))
        chosen = self.retriever.triage(
            searched.candidates, actions, leads_by_id, self.sources, reads
        )
        self._emit("fetching", base + 5, "Reading", f"Reading up to {reads} pages", phase=phase)
        fetched = self.retriever.fetch(chosen, reads)
        self._check_cancelled()
        fetched_for: dict[str, set[str]] = defaultdict(set)
        for item in fetched:
            self.sources.append(item.source)
            self._settle_lead(item, leads_by_id)
            if item.page is not None and item.source.rejection_reason is None:
                self.pages_by_site[item.source.host_group].append(item.page)
                for target_id in item.candidate.target_ids if item.candidate else []:
                    fetched_for[target_id].add(item.source.id)
        self._settle_unreachable_leads()
        for item in fetched:
            if item.page is not None and item.source.rejection_reason is None:
                self._extract(item)
                # What a page cites is a route to the record behind it, and is followed before
                # the same question is asked of search again.
                self._add_leads(self.retriever.reference_leads(item))
        # An original recognised only now has not been judged yet, for any target.
        restored = self._mark_duplicates()
        self._check_cancelled()

        self._emit(
            "judging",
            base + 10,
            "Weighing evidence",
            "Checking passages against the questions",
            phase=phase,
        )
        selections = {}
        for target_id in targets:
            target = self.plan.target(target_id)
            if target is None:
                continue
            state = self.emap.state(target_id)
            improving = state.sufficiency != Sufficiency.INSUFFICIENT
            chunks = select_candidates(
                self.plan,
                target,
                self.pool,
                _CHUNKS_IMPROVING if improving else _CHUNKS_OPEN,
                seed=self.trace.run_id,
                semantic_scores=self.semantic.scores(target, self.pool.eligible()),
                only_sources=(fetched_for.get(target_id, set()) | restored) if improving else None,
            )
            if chunks:
                selections[target_id] = chunks
        judged = judge_candidates(
            self.plan,
            selections,
            self.pool,
            self.evidence,
            llm_factory=self.deps.llm_factory,
            budget=self.llm_budget,
            trace=self.trace,
            round_no=step,
            today=self.deps.today(),
            out_of_time=lambda reserved_now: not self.llm_budget.has_time(reserved=reserved_now),
            reserved=first,
            known_leads=self.leads,
            batch_size=_JUDGE_BATCH,
            max_calls=self.judge_calls,
        )
        self.evidence.extend(judged.items)
        self.roles.update(judged.roles)
        self._add_leads(judged.leads)
        self._grade_evidence()
        self.groups = detect_contradictions(self.evidence, self.groups, judged.conflict_links)
        for action in actions:
            if action.intent == Intent.RESOLVE_CONFLICT:
                target = self.plan.target(action.target_id)
                family = self.emap.family(target) if target else {action.target_id}
                for group in self.groups:
                    if group.question_id in family and group.status == ContradictionStatus.OPEN:
                        group.intents_spent += 1
        self.emap.update(self.evidence, self.sources, self.groups, self.leads)
        for action in actions:
            after = self.emap.state(action.target_id).fingerprint
            self.emap.record_gain(action.target_id, action.intent, after > before[action.target_id])
        self.trace.target_states = [s.model_copy() for s in self.emap.states.values()]
        established = sum(
            1
            for t in self.plan.top_targets()
            if t.tier == Tier.CORE and self.emap.state(t.id).sufficiency != Sufficiency.INSUFFICIENT
        )
        self._emit(
            "coverage",
            base + 15,
            "Evidence updated",
            f"{established} of {sum(t.tier == Tier.CORE for t in self.plan.top_targets())} "
            "core questions established",
            sources=self.sources,
            evidence=self.evidence,
            phase=phase,
        )

    def _secure_finishing(self) -> None:
        """Evidence the run accepted is never thrown away for want of time to write it up.

        Research may run up to the deadline, and on a slow machine one call can take minutes.
        A run that reached the deadline with findings in hand used to skip claim synthesis and
        report nothing established. Writing up is the point of the run, so when there is
        anything to write up, the deadline moves out by what claim synthesis and the summary
        take at this run's own pace. What the audit cannot then check is stated as partial.
        """
        accepted = sum(1 for item in self.evidence if item.status == "accepted")
        if not accepted or self.llm_budget.deadline is None:
            return
        # Claim synthesis writes a long answer, so it is charged at the run's slowest pace.
        # Never past the hard limit, which was sized for this when the run started.
        per_call = self.llm_budget.slowest_seconds_per_call()
        needed = _GUARANTEED_FINISHING_CALLS * per_call * self.llm_budget.RESERVE_MARGIN
        added = self.llm_budget.extend_deadline(self.deps.clock() + needed)
        if added <= 0:
            return
        self.deadline = self.llm_budget.deadline
        self.ceiling_seconds = self.deadline - self.started
        self.trace.errors.append(
            f"time limit extended by up to {max(1, round(added / 60))} min so the {accepted} "
            "findings already accepted could be written up"
        )

    def _synthesize(self) -> ResearchOutcome:
        assert self.plan is not None and self.emap is not None
        if self.llm_budget.model_down and not any(
            item.status == "accepted" for item in self.evidence
        ):
            return self._failure(
                f"The research model is unavailable ({self.llm_budget.down_reason})."
            )
        self._secure_finishing()
        self._finishing_from = self.llm_budget.used
        self._emit("synthesizing", 76, "Building claims", "Turning evidence into claims")
        claims, links = build_claims(
            self.plan,
            self.evidence,
            self.emap.states,
            llm_factory=self.deps.llm_factory,
            llm_budget=self.llm_budget,
            trace=self.trace,
        )
        if links:
            self.groups = detect_contradictions(self.evidence, self.groups, links)
            self.emap.update(self.evidence, self.sources, self.groups, self.leads)
        self._emit(
            "verifying", 84, "Checking citations", "Checking every claim against its evidence"
        )
        verified = audit(
            self.plan,
            claims,
            self.evidence,
            policy=self.policy,
            llm_factory=self.deps.llm_factory,
            llm_budget=self.llm_budget,
            trace=self.trace,
        )
        verified_targets = self._verified_targets(verified.claims)
        refused = False
        unanswered = self._unanswered_core(verified_targets)
        self._check_deadline()
        if unanswered and self.deadline_stage is None:
            # Verification removed the only answer to a core question. One more step is worth
            # more than reporting nothing about it, when the run can still afford one.
            step, claims, verified, verified_targets, refused = self._re_research(
                unanswered, claims, verified
            )
        if refused and self.loop_end != "ceiling":
            self.stop_detail = (
                "the step, call or time limit left no room to look again for questions "
                "verification left unanswered"
            )
        self._check_deadline()
        if self.deadline_stage is not None and self.loop_end != "ceiling":
            self.loop_end = "ceiling"
            self.stop_detail = f"time limit reached during {self.deadline_stage}"
        verified = self._recover_withheld(verified)
        verified_targets = self._verified_targets(verified.claims)
        label_strengths(self.plan, verified.claims, self.evidence, self.sources)
        completion = stopping.completion_status(
            self.emap,
            self.policy,
            self.groups,
            loop_end=self.loop_end,
            verified_targets=verified_targets,
            re_research_refused=refused,
        )
        comparison = build_comparison(self.plan, list(verified.claims), self.emap.states)
        confidence, reasons = assess_confidence(self.emap, verified.claims)
        metrics = self._metrics()
        self._emit("reporting", 92, "Writing report", "Rendering verified findings")
        self._check_deadline()
        rendered = render(
            query=self.query,
            depth=self.depth,
            plan=self.plan,
            verified=verified,
            evidence=self.evidence,
            sources=self.sources,
            emap=self.emap,
            groups=self.groups,
            comparison=comparison,
            confidence=confidence,
            confidence_reasons=reasons,
            completion=completion,
            stop_detail=self.stop_detail,
            metrics=metrics,
            llm_factory=self.deps.llm_factory,
            llm_budget=self.llm_budget,
        )
        self.trace.confidence_reasons = reasons
        self.trace.report_sentences = rendered.sentences
        outcome = self._outcome(completion, self.stop_detail, report_text=rendered.text)
        outcome.claims = list(verified.claims)
        outcome.unverified = list(verified.unverified)
        outcome.comparison = comparison
        outcome.confidence = confidence
        outcome.confidence_reasons = reasons
        outcome.report_sections = rendered.sections
        outcome.report_type = rendered.report_type  # type: ignore[assignment]
        outcome.metrics = metrics
        return outcome

    def _recover_withheld(self, verified):
        """Give evidence behind a withheld model claim a second chance, in its own words.

        A small model often paraphrases an excerpt into something the excerpt does not quite
        say; verification rightly withholds that claim, and the fact it came from was lost with
        it. The excerpt's own words are then offered as a finding and verified like any other
        (they are entailed by construction, and must still speak to their question).
        """
        assert self.plan is not None
        by_id = {item.id: item for item in self.evidence}
        withheld = {
            evidence_id
            for claim in verified.removed
            if claim.origin == "model"
            for evidence_id in claim.evidence_ids
        }
        items = [
            by_id[u.evidence_id]
            for u in verified.unverified
            if u.evidence_id in withheld and u.evidence_id in by_id
        ]
        if not items:
            return verified
        # Only surviving findings count as already said; the withheld paraphrase does not.
        retry = extractive_claims(self.plan, items, list(verified.claims))
        if not retry:
            return verified
        for claim in retry:
            claim.id = f"r{claim.id}"  # never collide with a withheld claim's id
        rechecked = audit(
            self.plan,
            retry,
            self.evidence,
            policy=self.policy,
            llm_factory=self.deps.llm_factory,
            llm_budget=self.llm_budget,
            trace=self.trace,
        )
        if rechecked.claims:
            self.trace.errors.append(
                f"{len(rechecked.claims)} withheld finding(s) were restated in their excerpts' "
                "own words and verified"
            )
        return merge_verified(verified, rechecked)

    def _unanswered_core(self, verified_targets: set[str]) -> list[str]:
        """Core targets the evidence establishes but no verified claim states."""
        assert self.plan is not None and self.emap is not None
        return [
            target.id
            for target in self.plan.targets
            if target.tier == Tier.CORE
            and target.id not in verified_targets
            and not (target.parent_id is None and self.emap.family(target) & verified_targets)
            and self.emap.state(target.id).sufficiency != Sufficiency.INSUFFICIENT
        ]

    def _re_research(self, unanswered: list[str], claims, verified):
        """One more step for the targets left without an answer, then rebuild and re-check them."""
        assert self.plan is not None and self.emap is not None and self.retriever is not None
        # The step, then rebuilding and re-checking the claims it produces.
        affordable = self.steps < self.policy.ceilings.steps and self.llm_budget.can_afford(
            3 + self.judge_calls, reserved=True
        )
        actions = []
        if affordable:
            for index, target_id in enumerate(unanswered[: self.policy.ceilings.targets_per_step]):
                target = self.plan.target(target_id)
                action = target and strategy.recovery_action(
                    target, self.emap, self.policy, self.leads, self.groups, self.steps + 1, index
                )
                if action is not None:
                    actions.append(action)
        if not actions:
            return self.steps, claims, verified, self._verified_targets(verified.claims), True
        self.steps += 1
        self._emit(
            "searching",
            88,
            "Looking again",
            f"Looking again at {len(unanswered)} question(s) verification could not confirm",
        )
        self._run_step(self.steps, actions)
        stopping.settle_targets(self.emap, self.policy, self.leads, self.groups)
        rebuilt, links = build_claims(
            self.plan,
            self.evidence,
            self.emap.states,
            llm_factory=self.deps.llm_factory,
            llm_budget=self.llm_budget,
            trace=self.trace,
        )
        if links:
            self.groups = detect_contradictions(self.evidence, self.groups, links)
            self.emap.update(self.evidence, self.sources, self.groups, self.leads)
        family = {
            member
            for target_id in unanswered
            for member in self.emap.family(self.plan.target(target_id))
        }
        fresh = [claim for claim in rebuilt if claim.question_id in family]
        if not fresh:
            return self.steps, claims, verified, self._verified_targets(verified.claims), False
        # Claims are numbered from one each time they are written, so the second set is given
        # its own ids: a citation must never resolve to a claim from the other pass.
        taken = {claim.id for claim in claims}
        renamed = {claim.id: f"{claim.id}b" for claim in fresh if claim.id in taken}
        for claim in fresh:
            claim.id = renamed.get(claim.id, claim.id)
            claim.supporting_claim_ids = [
                renamed.get(reason, reason) for reason in claim.supporting_claim_ids
            ]
        rechecked = audit(
            self.plan,
            fresh,
            self.evidence,
            policy=self.policy,
            llm_factory=self.deps.llm_factory,
            llm_budget=self.llm_budget,
            trace=self.trace,
        )
        merged = merge_verified(verified, rechecked)
        # A draft the first audit removed and the second confirmed was not withheld after all.
        confirmed = {claim.text for claim in merged.claims}
        self.trace.removed_claims = [
            claim for claim in self.trace.removed_claims if claim.text not in confirmed
        ]
        return self.steps, [*claims, *fresh], merged, self._verified_targets(merged.claims), False

    def _verified_targets(self, claims) -> set[str]:
        assert self.plan is not None
        verified: set[str] = set()
        for claim in claims:
            if claim.question_id is None or claim.support not in (
                SupportStatus.SUPPORTED,
                SupportStatus.PARTIALLY_SUPPORTED,
            ):
                continue
            verified.add(claim.question_id)
            target = self.plan.target(claim.question_id)
            if target is not None and target.parent_id:
                verified.add(target.parent_id)
        return verified

    # --- stages ------------------------------------------------------------------------

    def _can_afford_step(self, step: int) -> tuple[bool, str]:
        assert self.retriever is not None
        ceilings = self.policy.ceilings
        if step > ceilings.steps:
            return False, "step limit reached"
        if not self.llm_budget.has_time():
            return False, "time limit reached"
        if not self.llm_budget.can_afford(1 + self.judge_calls):
            return False, (
                "time limit reached"
                if self.llm_budget.used < self.llm_budget.limit - self.llm_budget.reserved
                else "model-call limit reached"
            )
        open_lead = any(lead.status == LeadStatus.OPEN and lead.href for lead in self.leads)
        if self.retriever.searches_remaining <= 0 and not open_lead:
            return False, "search limit reached"
        if self.retriever.fetches_remaining <= 0 and not self._extend_reads():
            return False, self._read_limit_detail
        return True, ""

    def _extend_reads(self) -> bool:
        """Read on past the mode's usual page allowance, a few pages at a time.

        The allowance is where a run normally settles, not a guillotine. An answer that is
        still coming together deserves the pages it takes, so research continues while a core
        question is unanswered, the last step improved something, some action would still help
        it, and every absolute limit allows it. It stops at the hard ceiling, and the ordinary
        reasons to stop -- sufficient coverage, a stall, nothing worth doing -- are decided
        before this and are unaffected by it.
        """
        assert self.plan is not None and self.emap is not None and self.retriever is not None
        ceilings = self.policy.ceilings
        self._read_limit_detail = "page-read limit reached"
        if self.retriever.max_reads >= ceilings.reads_hard:
            return False
        if self.emap.steps_without_change:
            self._read_limit_detail = "page-read limit reached, and the last step added nothing"
            return False
        unresolved = [
            target
            for target in self.plan.top_targets()
            if target.tier == Tier.CORE
            and self.emap.state(target.id).sufficiency == Sufficiency.INSUFFICIENT
        ]
        if not any(
            strategy.has_material_action(target, self.emap, self.policy, self.leads, self.groups)
            for target in unresolved
        ):
            return False
        batch = min(
            max(2, ceilings.targets_per_step), ceilings.reads_hard - self.retriever.max_reads
        )
        self.retriever.max_reads += batch
        self.trace.budget.read_extensions += 1
        return True

    def _add_leads(self, leads: list[OriginLead]) -> None:
        known = {(lead.target_id, lead.href or lead.title) for lead in self.leads}
        for lead in leads:
            key = (lead.target_id, lead.href or lead.title)
            if key in known:
                continue
            known.add(key)
            self.leads.append(lead)
        self.trace.leads = self.leads

    def _settle_unreachable_leads(self) -> None:
        """A record on a site that refused us is located, and unreadable.

        Knowing that is what sends the run looking for a copy of it, instead of asking the
        same site again or treating the record as though it did not exist.
        """
        assert self.retriever is not None
        for lead in self.leads:
            if lead.status != LeadStatus.OPEN or not lead.href:
                continue
            if host_group(host_of(lead.href)) in self.retriever.blocked_groups:
                lead.status = LeadStatus.UNREADABLE

    def _settle_lead(self, item: FetchedSource, leads_by_id: dict[str, OriginLead]) -> None:
        """A lead is resolved once its page has been read, or found unreadable."""
        urls = {item.source.canonical_url, canonicalize_url(item.source.url)}
        candidate = item.candidate
        for lead in self.leads:
            matched = (candidate is not None and candidate.lead_id == lead.id) or (
                lead.href and canonicalize_url(lead.href) in urls
            )
            if not matched or lead.status != LeadStatus.OPEN:
                continue
            lead.status = (
                LeadStatus.FETCHED
                if item.page is not None and item.source.rejection_reason is None
                else LeadStatus.UNREADABLE
            )

    def _extract(self, item) -> None:
        source = item.source
        try:
            extraction = extract_chunks(
                source.id, item.page, repeated_block_hashes(self.pages_by_site[source.host_group])
            )
        except Exception as exc:  # one unparseable page is that page's problem only
            source.rejection_reason = RejectionReason.POOR_EXTRACTION
            self.trace.rejections.append(
                RejectionRecord(
                    stage="extraction",
                    ref=source.url,
                    reason=RejectionReason.POOR_EXTRACTION,
                    detail=exc.__class__.__name__,
                )
            )
            return
        source.extraction_quality = extraction.quality
        source.content_words = extraction.content_words
        if not source.snippet and extraction.chunks:
            source.snippet = extraction.chunks[0].text[:500]
        if extraction.rejection:
            source.rejection_reason = extraction.rejection
            self.trace.rejections.append(
                RejectionRecord(
                    stage="extraction",
                    ref=source.url,
                    reason=extraction.rejection,
                    detail=(
                        f"quality {extraction.quality}, {extraction.content_words} content words"
                    ),
                )
            )
            return
        self.pool.add(source, extraction.chunks)
        self.fingerprints[source.id] = extraction.fingerprint

    def _grade_evidence(self) -> None:
        """Reassess every source's provenance and what each accepted item can do for its target.

        Provenance can rise after a source is first read, when a later page cites or links it,
        so grading runs over everything read so far rather than only the newest sources.
        """
        assert self.plan is not None
        assess_sources(
            self.sources,
            self.roles,
            self.leads,
            sources_citing_leads={item.source_id for item in self.evidence if item.lead_ids},
            plan=self.plan,
        )
        by_id = {source.id: source for source in self.sources}
        for item in self.evidence:
            if item.status != "accepted":
                continue
            item.claim_fit, item.interested_party = claim_fit(
                item, self.plan, by_id.get(item.source_id)
            )
        resolve_restatements(self.evidence, self.sources)

    def _mark_duplicates(self) -> set[str]:
        """Group copies of one page and keep the page that is most evidently the original.

        Which copy is the original is re-decided every step, from what the run has read so far,
        so the order pages happened to be read in never decides it. Returns the sources that
        were copies before and are now the kept original.
        """
        by_id = {source.id: source for source in self.sources}
        readable = {
            source_id: fingerprint
            for source_id, fingerprint in self.fingerprints.items()
            if by_id[source_id].rejection_reason is None
        }
        parent = {source_id: source_id for source_id in readable}

        def root(source_id: str) -> str:
            while parent[source_id] != source_id:
                parent[source_id] = parent[parent[source_id]]
                source_id = parent[source_id]
            return source_id

        containment_of: dict[frozenset[str], float] = {}
        for left_id, right_id, containment in near_duplicate_pairs(readable):
            parent[root(left_id)] = root(right_id)
            containment_of[frozenset((left_id, right_id))] = containment
        clusters: dict[str, list[ResearchSource]] = defaultdict(list)
        for source_id in readable:
            clusters[root(source_id)].append(by_id[source_id])

        restored: set[str] = set()
        for members in clusters.values():
            if len(members) < 2:
                continue
            groups = {member.host_group for member in members}
            keep = min(members, key=lambda s: self._original_rank(s, groups))
            if keep.duplicate_of is not None:
                restored.add(keep.id)
                keep.duplicate_of = None
            for drop in members:
                if drop is keep or drop.duplicate_of == keep.id:
                    continue
                drop.duplicate_of = keep.id
                containment = containment_of.get(frozenset((drop.id, keep.id)))
                self.trace.rejections.append(
                    RejectionRecord(
                        stage="dedup",
                        ref=drop.url,
                        reason=RejectionReason.NEAR_DUPLICATE,
                        detail=f"copy of {keep.url}"
                        + (f" (containment {containment})" if containment else ""),
                    )
                )
        return restored

    def _original_rank(self, source: ResearchSource, cluster_groups: set[str]) -> tuple:
        """Sort key: the copy that declares no other canonical, that independent pages link to,
        and that is best graded so far comes first. Read order breaks only exact ties."""
        declares_elsewhere = bool(
            source.declared_canonical and source.declared_canonical != source.canonical_url
        )
        linked_exactly: set[str] = set()
        linked_host: set[str] = set()
        for other in self.sources:
            if other.host_group in cluster_groups or not other.reviewed:
                continue
            if other.rejection_reason is not None or other.duplicate_of is not None:
                continue
            for link in other.links:
                href = canonicalize_url(link.href)
                if href == source.canonical_url:
                    linked_exactly.add(other.independence_key)
                elif host_group(host_of(href)) == source.host_group:
                    linked_host.add(other.independence_key)
        grade = source.assessment.provenance if source.assessment else None
        return (
            declares_elsewhere,
            -len(linked_exactly),
            -len(linked_host),
            _PROVENANCE_ORDER[grade],
            -source.extraction_quality,
            int(source.id[1:]) if source.id[1:].isdigit() else 0,
        )

    # --- metrics and outcome -------------------------------------------------------------

    def _metrics(self) -> ResearchMetrics:
        open_questions = 0
        if self.plan is not None and self.emap is not None:
            open_questions = sum(
                1
                for target in self.plan.top_targets()
                if target.tier in self.policy.pursued_tiers
                and self.emap.state(target.id).sufficiency != Sufficiency.ESTABLISHED
            )
        return ResearchMetrics(
            searches_run=self.retriever.searches_used if self.retriever else 0,
            sources_reviewed=sum(1 for source in self.sources if source.reviewed),
            evidence_accepted=sum(1 for item in self.evidence if item.status == "accepted"),
            evidence_rejected=sum(1 for item in self.evidence if item.status == "rejected"),
            open_gaps=open_questions,
        )

    def _cancelled_now(self) -> bool:
        try:
            return bool(self.cancelled())
        except Exception:
            return False

    def _emit(self, stage: str, percent: int, step: str, message: str, **payload) -> None:
        # The deadline is checked at every stage boundary, so the stage that was running when
        # it passed is the one recorded.
        if hasattr(self, "llm_budget"):
            self._check_deadline()
        self.stage = stage
        if self.progress is None:
            return
        core = [t for t in self.plan.top_targets() if t.tier == Tier.CORE] if self.plan else []
        established = sum(
            1
            for target in core
            if self.emap is not None
            and self.emap.state(target.id).sufficiency != Sufficiency.INSUFFICIENT
        )
        try:
            self.progress(
                ProgressUpdate(
                    stage=stage,
                    percent=min(99, percent),
                    step=step,
                    message=message,
                    metrics=self._metrics(),
                    queries=[action.query for action in self.trace.actions if action.query],
                    core_established=established,
                    core_targets=len(core),
                    elapsed_seconds=int(self.deps.clock() - self.started),
                    ceiling_seconds=int(self.ceiling_seconds),
                    remaining_seconds=self._remaining_seconds(),
                    **payload,
                )
            )
        except Exception:
            logger.exception("Research progress callback failed")

    def _remaining_seconds(self) -> int | None:
        """How long the run should still take, from what it has spent and has left to do."""
        now = self.deps.clock()
        model_seconds = sum(usage.seconds for usage in self.profile.stages.values())
        # Searching and reading per step, measured as the run's time outside model calls.
        step_seconds = (
            max(5.0, (now - self.started - model_seconds) / self.steps) if self.steps else None
        )
        used = self.llm_budget.used
        finishing = None if self._finishing_from is None else used - self._finishing_from
        return run_estimate.remaining(
            self.shape,
            calls_used=used,
            steps_done=self.steps,
            planned=self.plan is not None,
            finishing_calls_used=finishing,
            run_seconds_per_call=self.llm_budget.seconds_per_call(),
            run_step_seconds=step_seconds,
            prior_seconds_per_call=self._prior_seconds_per_call,
            seconds_left=max(0.0, self.deadline - now),
        )

    def _finalize_trace(self, completion: CompletionStatus, detail: str) -> None:
        self.trace.completion_status = completion
        self.trace.stop_reason = _STOP_OF_COMPLETION[completion]
        self.trace.stop_detail = detail
        self.trace.budget.searches = self.retriever.searches_used if self.retriever else 0
        self.trace.budget.fetches = self.retriever.fetches_used if self.retriever else 0
        self.trace.budget.llm_calls = self.llm_budget.used
        self.trace.budget.seconds = round(self.deps.clock() - self.started, 2)
        self.trace.stages = [StageTiming(**usage) for usage in self.profile.as_list()]
        self.trace.contradiction_groups = self.groups
        self.trace.leads = self.leads
        if self.emap is not None:
            self.trace.target_states = [s.model_copy() for s in self.emap.states.values()]
        selected = {selection.chunk_id for selection in self.trace.chunk_selections}
        grounding = {item.chunk_id for item in self.evidence if item.status == "accepted"}
        self.trace.chunks = [
            chunk if chunk.id in grounding else chunk.model_copy(update={"text": ""})
            for chunk_id, chunk in self.pool.chunks.items()
            if chunk_id in selected or chunk_id in grounding
        ]
        self.trace.diagnostics = self._diagnostics(completion, detail)

    def _diagnostics(self, completion: CompletionStatus, detail: str) -> dict:
        """What it takes to diagnose this run without repeating it. No credentials, no prompts."""
        rejected = Counter(
            (item.rejection_reason.value if item.rejection_reason else "unknown")
            for item in self.evidence
            if item.status == "rejected"
        )
        fallbacks = list(self.fallbacks)
        for note in self.trace.errors:
            lowered = note.lower()
            if any(word in lowered for word in ("fell back", "skipped", "own words", "slow to")):
                stage = next(
                    (
                        stage_name
                        for phrase, stage_name in (
                            ("query", "queries"),
                            ("judge", "judging"),
                            ("finding(s) were written", "claims"),
                            ("claim", "claims"),
                            ("audit", "verification"),
                            ("summary", "report"),
                            ("first call", "model"),
                        )
                        if phrase in lowered
                    ),
                    "run",
                )
                fallbacks.append({"stage": stage, "reason": note[:300]})
        claims = self.trace.verification
        return {
            "inference": {
                "provider": self.capability.provider,
                "model": self.capability.model,
                "context_window": self.capability.context_window,
                "max_output_tokens": self.capability.max_output_tokens,
                "capability_known": self.capability.known,
                "call_allowance_scale": self.scale,
                "structured_output": "json_schema",
                "thinking": "disabled",
                "temperature": 0.1,
            },
            "search": {
                "providers": [getattr(p, "name", "") for p in self.deps.providers],
                "used": sorted({a.provider for a in self.trace.search_attempts if a.provider}),
                "searches": self.retriever.searches_used if self.retriever else 0,
                "fetches": self.retriever.fetches_used if self.retriever else 0,
            },
            "depth": self.depth.value,
            "timing": {
                "ceiling_seconds": round(self.ceiling_seconds, 1),
                "hard_limit_seconds": round(self.hard_limit_seconds, 1),
                "deadline_hit": self.deadline_stage,
                "elapsed_seconds": round(self.deps.clock() - self.started, 1),
                "pace": self._pace_source,
                "seconds_per_call": round(self.llm_budget.seconds_per_call(), 1),
            },
            "stages": {
                usage.stage: {
                    "calls": usage.calls,
                    "failures": usage.failures,
                    "retries": usage.retries,
                    "truncations": usage.truncations,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "seconds": usage.seconds,
                }
                for usage in self.profile.stages.values()
            },
            "fallbacks": fallbacks,
            "evidence": {
                "sources_read": sum(1 for s in self.sources if s.reviewed),
                "sources_refused": sum(
                    1 for s in self.sources if s.http_status in (401, 403, 429, 451)
                ),
                "accepted": sum(1 for item in self.evidence if item.status == "accepted"),
                "rejected": dict(rejected),
            },
            "claims": {
                "kept": sum(1 for d in claims if d.outcome != "removed"),
                "removed": sum(1 for d in claims if d.outcome == "removed"),
            },
            "end": {
                "status": completion.value,
                "reason": detail,
                "model_down": self.llm_budget.down_reason,
            },
        }

    def _outcome(
        self, completion: CompletionStatus, detail: str, *, report_text: str
    ) -> ResearchOutcome:
        self._finalize_trace(completion, detail)
        return ResearchOutcome(
            query=self.query,
            depth=self.depth,
            plan=self.plan,
            sources=self.sources,
            evidence=self.evidence,
            report=report_text,
            stop_reason=_STOP_OF_COMPLETION[completion],
            completion_status=completion,
            stop_detail=detail,
            target_states=list(self.trace.target_states),
            metrics=self._metrics(),
            trace=self.trace,
            report_type="failed" if completion == CompletionStatus.FAILED else "partial",
        )

    def _blocked(self) -> ResearchOutcome:
        metrics = self._metrics()
        rendered = render_failure(self.query, self.depth, self.stop_detail, metrics)
        outcome = self._outcome(
            CompletionStatus.BLOCKED, self.stop_detail, report_text=rendered.text
        )
        outcome.report_sections = rendered.sections
        return outcome

    def _failure(self, detail: str) -> ResearchOutcome:
        self.trace.errors.append(detail)
        metrics = self._metrics()
        rendered = render_failure(self.query, self.depth, detail, metrics)
        outcome = self._outcome(CompletionStatus.FAILED, detail, report_text=rendered.text)
        outcome.report_sections = rendered.sections
        return outcome


_PHASE_LABEL = {
    "finding_primary_evidence": "Finding primary evidence",
    "investigating": "Investigating",
    "checking_conflicts": "Checking conflicting evidence",
}


def _phase_of(actions: list[Action]) -> str:
    intents = {action.intent for action in actions}
    if intents & {Intent.LOCATE_ORIGIN, Intent.FOLLOW_LEAD, Intent.ACCESSIBLE_COPY}:
        return "finding_primary_evidence"
    if Intent.RESOLVE_CONFLICT in intents:
        return "checking_conflicts"
    return "investigating"


def _step_message(phase: str, actions: list[Action]) -> str:
    count = len({action.target_id for action in actions})
    noun = "question" if count == 1 else "questions"
    if phase == "finding_primary_evidence":
        return f"Looking for original sources for {count} {noun}"
    if phase == "checking_conflicts":
        return f"Checking disagreements on {count} {noun}"
    return f"Investigating {count} {noun}"


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0

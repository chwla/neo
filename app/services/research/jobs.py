"""Research jobs: run the shared research engine in a background thread and persist it."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import build_engine
from app.repositories.memory import MemoryRepository
from app.services.disclosure import Labeled, label_of
from app.services.memory.prompt import (
    RecallPromptOrchestrator,
    repository_usage_recorder,
)
from app.services.memory.queries import MemoryQueryContext, RecallMode
from app.services.memory.recall import CanonicalRecallService
from app.services.memory.runtime import build_memory_recall_dependencies
from app.services.memory.settings import MemorySettings
from app.services.profile_accounts import database_url_for, profile_database
from app.services.research.engine import ProgressUpdate, default_dependencies, run_research
from app.services.research.memory_scope import (
    ResearchMemoryRecallResult,
    retrieve_scoped_memory_result,
)
from app.services.research.policy import POLICIES
from app.services.research.report import lead_answer
from app.services.research.store import (
    load_job,
    mark_interrupted_jobs,
    query_label_for,
    save_job,
    update_job_status,
)
from app.services.research.types import (
    CompletionStatus,
    DepthMode,
    JobStatus,
    ProgressEvent,
    ResearchJob,
    ResearchOutcome,
    StopReason,
)
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleResolveRequest

logger = logging.getLogger(__name__)

_active_jobs: dict[str, threading.Event] = {}
_lock = threading.Lock()

ResearchMemoryProvider = Callable[
    [dict[str, Any]], tuple[RecallPromptOrchestrator, MemoryQueryContext]
]
_memory_research_provider: ResearchMemoryProvider | None = None

_STAGE_STATUS = {
    "planning": JobStatus.PLANNING,
    "searching": JobStatus.SEARCHING,
    "fetching": JobStatus.FETCHING,
    "judging": JobStatus.EXTRACTING,
    "coverage": JobStatus.EXTRACTING,
    "synthesizing": JobStatus.SYNTHESIZING,
    "verifying": JobStatus.SYNTHESIZING,
    "reporting": JobStatus.SYNTHESIZING,
}


@dataclass
class _OwnedResearchMemoryRuntime:
    orchestrator: RecallPromptOrchestrator
    query_context: MemoryQueryContext
    session: Session
    engine: Any

    def finish(self, *, persist_usage: bool) -> None:
        try:
            if persist_usage:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
            self.engine.dispose()


def configure_memory_research_provider(
    provider: ResearchMemoryProvider | None,
) -> None:
    """Install trusted request-bound wiring; never a process-default DB session."""
    global _memory_research_provider
    _memory_research_provider = provider


def _job_memory_binding(job_data: dict[str, Any]) -> tuple[str, str, str, bool] | None:
    metadata = job_data.get("metadata") or {}
    owner_id = job_data.get("owner_id") or metadata.get("memory_owner_id")
    database_identity = job_data.get("database_identity") or metadata.get(
        "memory_database_identity"
    )
    profile_id = job_data.get("profile_id") or metadata.get("memory_profile_id")
    is_guest = bool(job_data.get("is_guest") or metadata.get("memory_is_guest"))
    if not owner_id or not database_identity or not profile_id:
        return None
    return str(owner_id), str(database_identity), str(profile_id), is_guest


def _default_research_memory_runtime(
    job_data: dict[str, Any],
) -> _OwnedResearchMemoryRuntime | None:
    binding = _job_memory_binding(job_data)
    if binding is None:
        return None
    owner_id, database_identity, profile_id, is_guest = binding
    settings = get_settings()
    flags = MemorySettings.from_settings(settings)
    if not flags.research_recall_enabled or not flags.owner_is_enabled(owner_id):
        return None
    engine = build_engine(database_url_for(profile_id, guest=is_guest))
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        repository = MemoryRepository(
            session,
            owner_id=owner_id,
            database_identity=database_identity,
        )
        recall_dependencies = build_memory_recall_dependencies(
            engine,
            owner_id=owner_id,
            database_identity=database_identity,
            flags=flags,
            settings=settings,
        )
        recall = CanonicalRecallService(
            repository,
            flags=flags,
            fts_index=recall_dependencies.fts_index,
            semantic_provider=recall_dependencies.semantic_provider,
            vector_index=recall_dependencies.vector_index,
            repair_scheduler=recall_dependencies.repair_scheduler,
            metric_recorder=recall_dependencies.metric_recorder,
            semantic_weight=settings.memory_semantic_weight,
            semantic_cap=settings.memory_semantic_cap,
            semantic_threshold=settings.semantic_similarity_threshold,
            vector_candidate_limit=settings.memory_vector_candidate_limit,
            fts_candidate_limit=settings.memory_fts_candidate_limit,
        )
        orchestrator = RecallPromptOrchestrator(
            recall,
            usage_recorder=repository_usage_recorder(repository),
        )
        metadata = job_data.get("metadata") or {}
        query_context = MemoryQueryContext(
            owner_id=owner_id,
            database_identity=database_identity,
            profile_id=profile_id,
            memory_enabled=bool(metadata.get("memory_enabled", True)),
            incognito=bool(metadata.get("incognito", False)),
            request_id=f"research:{job_data['id']}",
            session_id=f"research-job:{job_data['id']}",
            current_time=datetime.now(UTC),
            maximum_records=flags.recall_max_records,
            maximum_characters=flags.recall_max_chars,
            mode=RecallMode.SCOPED_LEXICAL,
            lexical_available=flags.lexical_recall_enabled,
        )
        return _OwnedResearchMemoryRuntime(orchestrator, query_context, session, engine)
    except Exception:
        session.close()
        engine.dispose()
        raise


def _canonical_research_memory(
    job_data: dict[str, Any],
    user_query: str,
) -> ResearchMemoryRecallResult:
    runtime: _OwnedResearchMemoryRuntime | None = None
    try:
        if _memory_research_provider is not None:
            orchestrator, query_context = _memory_research_provider(job_data)
        else:
            runtime = _default_research_memory_runtime(job_data)
            if runtime is None:
                return retrieve_scoped_memory_result(user_query, enabled=True)
            orchestrator, query_context = runtime.orchestrator, runtime.query_context
        result = retrieve_scoped_memory_result(
            user_query,
            enabled=True,
            orchestrator=orchestrator,
            query_context=query_context,
            usage_purpose=f"research_plan:{job_data['id']}",
        )
        if runtime is not None:
            runtime.finish(persist_usage=bool(result.diagnostic.get("usage_recorded")))
            runtime = None
        return result
    except Exception:
        logger.exception("Canonical research memory failed closed")
        return ResearchMemoryRecallResult(
            diagnostic={
                "mode": "canonical",
                "reason_codes": ["canonical_research_unavailable"],
                "final_injected_ids": [],
                "usage_event_ids": [],
            }
        )
    finally:
        if runtime is not None:
            runtime.finish(persist_usage=False)


def create_job(
    user_query: str | Labeled,
    depth: DepthMode = DepthMode.STANDARD,
    project_id: str | None = None,
    task_id: str | None = None,
    repo_id: str | None = None,
    owner_id: str | None = None,
    database_identity: str | None = None,
    profile_id: str | None = None,
    is_guest: bool = False,
    memory_enabled: bool = True,
    incognito: bool = False,
) -> ResearchJob:
    query_label = label_of(user_query)
    user_query = user_query.value if isinstance(user_query, Labeled) else user_query
    ceilings = POLICIES[depth].ceilings
    now = datetime.now(UTC).isoformat()
    rule_result = RuleResolver().resolve(
        RuleResolveRequest(
            context_type="research",
            project_id=project_id,
            task_id=task_id,
            repo_id=repo_id,
        )
    )
    job = ResearchJob(
        id=uuid.uuid4().hex[:12],
        user_query=user_query,
        depth=depth,
        max_sources=ceilings.reads,
        max_rounds=ceilings.steps,
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        current_step="Queued",
        owner_id=owner_id,
        database_identity=database_identity,
        profile_id=profile_id,
        is_guest=is_guest,
        metadata={
            "project_id": project_id,
            "task_id": task_id,
            "repo_id": repo_id,
            "memory_owner_id": owner_id,
            "memory_database_identity": database_identity,
            "memory_profile_id": profile_id,
            "memory_is_guest": is_guest,
            "memory_enabled": memory_enabled,
            "incognito": incognito,
            "resolved_rules": rule_result["resolved_rules"],
            "applied_profiles": rule_result["applied_profiles"],
            "rule_warnings": rule_result["warnings"],
        },
    )
    save_job(job.model_dump(mode="json"), query_label=query_label)
    return job


def start_job(job_id: str) -> bool:
    job_data = load_job(job_id)
    if not job_data:
        return False
    if job_data["status"] not in (JobStatus.QUEUED.value, "queued"):
        return False

    cancel_event = threading.Event()
    with _lock:
        _active_jobs[job_id] = cancel_event

    metadata = job_data.get("metadata") or {}
    thread = threading.Thread(
        target=_run_research_pipeline_bound,
        args=(
            job_id,
            cancel_event,
            metadata.get("memory_profile_id"),
            bool(metadata.get("memory_is_guest")),
        ),
        daemon=True,
        name=f"research-{job_id}",
    )
    thread.start()
    return True


def _run_research_pipeline_bound(
    job_id: str,
    cancel: threading.Event,
    profile_id: str | None,
    is_guest: bool,
) -> None:
    if profile_id:
        with profile_database(profile_id, guest=is_guest):
            _run_research_pipeline(job_id, cancel)
        return
    _run_research_pipeline(job_id, cancel)


def cancel_job(job_id: str) -> bool:
    with _lock:
        # Setting the flag and writing the status together means a run finishing at the same
        # moment either sees the flag or is overwritten by this write, never the reverse.
        cancel_event = _active_jobs.get(job_id)
        if cancel_event:
            cancel_event.set()
            update_job_status(job_id, JobStatus.CANCELLED.value, current_step="Cancelled by user")
            return True
    job_data = load_job(job_id)
    if job_data and job_data["status"] in ("queued",):
        update_job_status(job_id, JobStatus.CANCELLED.value, current_step="Cancelled by user")
        return True
    return False


def get_job(job_id: str) -> ResearchJob | None:
    data = load_job(job_id)
    if not data:
        return None
    return _dict_to_job(data)


def _dict_to_job(data: dict) -> ResearchJob:
    metadata = data.get("metadata") or {}
    data.setdefault("owner_id", metadata.get("memory_owner_id"))
    data.setdefault("database_identity", metadata.get("memory_database_identity"))
    data.setdefault("profile_id", metadata.get("memory_profile_id"))
    data.setdefault("is_guest", bool(metadata.get("memory_is_guest")))
    return ResearchJob(**data)


def _research_context(job_data: dict[str, Any], user_query: str) -> tuple[str, list[str]]:
    """Rules and scoped memory as untrusted planning context, plus the memory ids used."""
    settings = get_settings()
    metadata = dict(job_data.get("metadata") or {})
    memory_allowed = bool(
        settings.memory_enabled
        and not settings.memory_incognito
        and metadata.get("memory_enabled", True)
        and not metadata.get("incognito", False)
    )
    memory_result = (
        _canonical_research_memory(job_data, user_query)
        if memory_allowed
        else retrieve_scoped_memory_result(user_query)
    )
    metadata["memory_recall_diagnostic"] = memory_result.diagnostic
    job_data["metadata"] = metadata
    save_job(job_data)

    rule_result = {
        "resolved_rules": metadata.get("resolved_rules", {}),
        "applied_profiles": metadata.get("applied_profiles", []),
        "warnings": metadata.get("rule_warnings", []),
    }
    parts: list[str] = []
    rule_context = RuleResolver.research_context(rule_result)
    if rule_context:
        parts.append(f"Research rules (guidance only):\n{rule_context}")
    if memory_allowed and memory_result.context_text:
        parts.append(f"Remembered context about the user:\n{memory_result.context_text}")
    return "\n\n".join(parts), list(memory_result.canonical_ids)


def _run_research_pipeline(job_id: str, cancel: threading.Event) -> None:
    try:
        _update(job_id, JobStatus.PLANNING, 2, "Starting", "Preparing research")
        job_data = load_job(job_id)
        if not job_data:
            return
        user_query = job_data["user_query"]
        depth = DepthMode(job_data["depth"])
        metadata = job_data.get("metadata") or {}
        # Search queries and page choices are outbound channels. A model that
        # has seen remembered context or user-written rules can encode it in
        # either one, even when the literal secret is absent from the query.
        # Until research has a destination-bound approval flow, its worker sees
        # only the query the user submitted for this run.
        context, memory_keys = "", []
        metadata["memory_recall_diagnostic"] = {
            "mode": "withheld",
            "reason_codes": ["external_search_requires_explicit_grant"],
        }
        job_data["metadata"] = metadata
        save_job(job_data)
        route_name = RuleResolver.route_name(
            {
                "resolved_rules": {},
                "applied_profiles": [],
                "warnings": [],
            },
            "research",
            "research",
        )

        def on_progress(update: ProgressUpdate) -> None:
            fields: dict[str, Any] = {}
            if update.plan is not None:
                fields["plan_json"] = update.plan.model_dump(mode="json")
            if update.sources is not None:
                fields["sources_json"] = [s.model_dump(mode="json") for s in update.sources]
            if update.evidence is not None:
                fields["evidence_json"] = [e.model_dump(mode="json") for e in update.evidence]
            if update.queries is not None:
                fields["generated_queries_json"] = update.queries
            _update(
                job_id,
                _STAGE_STATUS.get(update.stage, JobStatus.SEARCHING),
                update.percent,
                update.step,
                update.message,
                progress=update,
                **fields,
            )

        outcome = run_research(
            Labeled(user_query, query_label_for(job_data)),
            depth,
            deps=default_dependencies(route_name),
            progress=on_progress,
            cancelled=cancel.is_set,
            context=context,
            run_id=job_id,
        )
        if cancel.is_set() or outcome.stop_reason == StopReason.USER_CANCELLED:
            return _mark_cancelled(job_id)
        _save_final(job_id, outcome, memory_keys, cancel)
    except Exception as exc:
        logger.exception("Research pipeline failed for job %s", job_id)
        update_job_status(
            job_id,
            JobStatus.FAILED.value,
            error=str(exc)[:500],
            current_step="Pipeline failed",
            progress_percent=0,
        )
    finally:
        with _lock:
            _active_jobs.pop(job_id, None)


def _update(
    job_id: str,
    status: JobStatus,
    pct: int,
    step: str,
    message: str,
    progress: ProgressUpdate | None = None,
    **fields: Any,
) -> None:
    job_data = load_job(job_id)
    if not job_data or job_data.get("status") == JobStatus.CANCELLED.value:
        return
    log = [item for item in job_data.get("progress_log", []) if isinstance(item, dict)]
    metrics = progress.metrics if progress is not None else None
    log.append(
        ProgressEvent(
            status=status.value,
            progress_percent=pct,
            current_step=step,
            message=message,
            timestamp=datetime.now(UTC).isoformat(),
            phase=progress.phase if progress is not None else "",
            core_established=progress.core_established if progress is not None else 0,
            core_targets=progress.core_targets if progress is not None else 0,
            elapsed_seconds=progress.elapsed_seconds if progress is not None else 0,
            ceiling_seconds=progress.ceiling_seconds if progress is not None else 0,
            searches_run=metrics.searches_run if metrics else 0,
            sources_reviewed=metrics.sources_reviewed if metrics else 0,
            sources_used=metrics.sources_used if metrics else 0,
            evidence_accepted=metrics.evidence_accepted if metrics else 0,
            open_gaps=metrics.open_gaps if metrics else 0,
        ).model_dump()
    )
    update_job_status(
        job_id,
        status.value,
        progress_percent=pct,
        current_step=step,
        progress_log_json=log[-200:],
        **fields,
    )


def _save_final(
    job_id: str,
    outcome: ResearchOutcome,
    memory_keys: list[str],
    cancel: threading.Event | None = None,
) -> None:
    data = load_job(job_id)
    if not data:
        return
    failed = outcome.completion_status == CompletionStatus.FAILED
    data["report"] = outcome.report
    data["plan"] = outcome.plan.model_dump(mode="json") if outcome.plan else None
    data["sources"] = [source.model_dump(mode="json") for source in outcome.sources]
    data["evidence"] = [item.model_dump(mode="json") for item in outcome.evidence]
    data["claims"] = [claim.model_dump(mode="json") for claim in outcome.claims]
    data["trace"] = outcome.trace.model_dump(mode="json")
    data["generated_queries"] = [action.query for action in outcome.trace.actions if action.query]
    metadata = dict(data.get("metadata") or {})
    metadata.update(
        {
            "stop_reason": outcome.stop_reason.value,
            "completion_status": outcome.completion_status.value,
            "stop_detail": outcome.stop_detail,
            "confidence": outcome.confidence.value,
            "confidence_reasons": outcome.confidence_reasons,
            "answer": lead_answer(outcome.trace.report_sentences),
            "report_type": outcome.report_type,
            "report_sections": outcome.report_sections,
            "metrics": outcome.metrics.model_dump(),
            "memory_used": memory_keys,
        }
    )
    data["metadata"] = metadata
    data["status"] = JobStatus.FAILED.value if failed else JobStatus.COMPLETED.value
    data["error"] = outcome.stop_detail if failed else None
    data["progress_percent"] = 100
    data["current_step"] = "Research failed" if failed else "Research complete"
    with _lock:
        if cancel is not None and cancel.is_set():
            return
        save_job(data)


def recover_interrupted_jobs() -> int:
    """Fail jobs a previous process left running; jobs running in this process are kept."""
    with _lock:
        active = set(_active_jobs)
    return mark_interrupted_jobs(active)


def _mark_cancelled(job_id: str) -> None:
    update_job_status(
        job_id,
        JobStatus.CANCELLED.value,
        current_step="Cancelled by user",
    )
    with _lock:
        _active_jobs.pop(job_id, None)

"""Research job orchestrator: runs the full research pipeline in a background thread."""

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
from app.repositories.memory_v2 import MemoryV2Repository
from app.services.llm import get_llm_client
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.prompt import (
    RecallPromptOrchestrator,
    repository_usage_recorder,
)
from app.services.memory_v2.queries import MemoryQueryContext, RecallMode
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.runtime import build_phase6_recall_dependencies
from app.services.profile_accounts import database_url_for, profile_database
from app.services.research.evidence import (
    extract_entity_terms,
    extract_evidence,
    filter_irrelevant_sources,
    identify_gaps,
)
from app.services.research.memory_scope import (
    ResearchMemoryRecallResult,
    retrieve_scoped_memory_result,
)
from app.services.research.planner import generate_followup_queries, generate_plan
from app.services.research.product_intent import TOPIC_PRODUCT_COMPARISON, normalize_user_query
from app.services.research.searcher import ResearchSearcher
from app.services.research.store import load_job, save_job, update_job_status
from app.services.research.synthesizer import synthesize_report
from app.services.research.topic_intent import TOPIC_AI_CODING_TOOLS, classify_topic_intent
from app.services.research.types import (
    DEPTH_CONFIG,
    DepthMode,
    JobStatus,
    ProgressEvent,
    ResearchJob,
)
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleResolveRequest

logger = logging.getLogger(__name__)

_active_jobs: dict[str, threading.Event] = {}
_lock = threading.Lock()

ResearchMemoryProvider = Callable[
    [dict[str, Any]], tuple[RecallPromptOrchestrator, MemoryQueryContext]
]
_memory_v2_research_provider: ResearchMemoryProvider | None = None


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


def configure_memory_v2_research_provider(
    provider: ResearchMemoryProvider | None,
) -> None:
    """Install trusted request-bound wiring; never a process-default DB session."""
    global _memory_v2_research_provider
    _memory_v2_research_provider = provider


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
    flags = MemoryV2FeatureFlags.from_settings(settings)
    if not flags.research_recall_enabled or not flags.owner_is_enabled(owner_id):
        return None
    engine = build_engine(database_url_for(profile_id, guest=is_guest))
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        repository = MemoryV2Repository(
            session,
            owner_id=owner_id,
            database_identity=database_identity,
        )
        phase6 = build_phase6_recall_dependencies(
            engine,
            owner_id=owner_id,
            database_identity=database_identity,
            flags=flags,
            settings=settings,
        )
        recall = CanonicalRecallService(
            repository,
            flags=flags,
            fts_index=phase6.fts_index,
            semantic_provider=phase6.semantic_provider,
            vector_index=phase6.vector_index,
            repair_scheduler=phase6.repair_scheduler,
            metric_recorder=phase6.metric_recorder,
            semantic_weight=settings.memory_v2_semantic_weight,
            semantic_cap=settings.memory_v2_semantic_cap,
            semantic_threshold=settings.semantic_similarity_threshold,
            vector_candidate_limit=settings.memory_v2_vector_candidate_limit,
            fts_candidate_limit=settings.memory_v2_fts_candidate_limit,
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
        if _memory_v2_research_provider is not None:
            orchestrator, query_context = _memory_v2_research_provider(job_data)
        else:
            runtime = _default_research_memory_runtime(job_data)
            if runtime is None:
                return retrieve_scoped_memory_result(user_query, v2_enabled=True)
            orchestrator, query_context = runtime.orchestrator, runtime.query_context
        result = retrieve_scoped_memory_result(
            user_query,
            v2_enabled=True,
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
    user_query: str,
    depth: DepthMode = DepthMode.STANDARD,
    max_sources: int | None = None,
    max_rounds: int | None = None,
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
    config = DEPTH_CONFIG[depth]
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
        max_sources=max_sources or config["max_sources"],
        max_rounds=max_rounds or config["max_rounds"],
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
    save_job(job.model_dump())
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
    if isinstance(data.get("depth"), str):
        data["depth"] = DepthMode(data["depth"])
    if isinstance(data.get("status"), str):
        data["status"] = JobStatus(data["status"])
    metadata = data.get("metadata") or {}
    data.setdefault("owner_id", metadata.get("memory_owner_id"))
    data.setdefault("database_identity", metadata.get("memory_database_identity"))
    data.setdefault("profile_id", metadata.get("memory_profile_id"))
    data.setdefault("is_guest", bool(metadata.get("memory_is_guest")))
    return ResearchJob(**data)


def _run_research_pipeline(job_id: str, cancel: threading.Event) -> None:
    """Execute the full multi-step research pipeline."""
    try:
        _update(job_id, JobStatus.PLANNING, 5, "Planning research", "Generating research plan...")
        if cancel.is_set():
            return _mark_cancelled(job_id)

        job_data = load_job(job_id)
        if not job_data:
            return
        depth = DepthMode(job_data["depth"])
        user_query = job_data["user_query"]
        max_sources = job_data["max_sources"]
        max_rounds = job_data["max_rounds"]

        query_norm = normalize_user_query(user_query)
        effective_query = query_norm.effective_query

        # --- SCOPED MEMORY ---
        settings = get_settings()
        v2_research = settings.memory_v2_research_recall_enabled
        if v2_research:
            memory_result = _canonical_research_memory(job_data, user_query)
        else:
            memory_result = retrieve_scoped_memory_result(user_query)
        memory_context = memory_result.context_text
        memory_keys = list(memory_result.canonical_ids)
        metadata = dict(job_data.get("metadata") or {})
        metadata["memory_recall_diagnostic"] = memory_result.diagnostic
        job_data["metadata"] = metadata
        save_job(job_data)
        if memory_context:
            _update(
                job_id,
                JobStatus.PLANNING,
                7,
                "Memory loaded",
                f"Loaded memory context: {', '.join(memory_keys)}",
            )

        rule_result = {
            "resolved_rules": job_data.get("metadata", {}).get("resolved_rules", {}),
            "applied_profiles": job_data.get("metadata", {}).get("applied_profiles", []),
            "warnings": job_data.get("metadata", {}).get("rule_warnings", []),
        }
        route_name = RuleResolver.route_name(rule_result, "research", "research")
        rule_context = RuleResolver.research_context(rule_result)
        untrusted_memory_context = memory_context if v2_research else ""
        if rule_context and not v2_research:
            memory_context = (
                f"{memory_context}\n\nResearch rules (guidance only):\n{rule_context}"
            ).strip()
        elif v2_research:
            memory_context = rule_context
        ollama = get_llm_client(num_predict=512, route_name=route_name)

        intent = classify_topic_intent(effective_query, original_query=user_query)
        plan = generate_plan(
            effective_query,
            depth,
            memory_context=memory_context,
            ollama=ollama,
            topic_intent=intent,
            original_query=user_query,
            untrusted_memory_context=untrusted_memory_context,
        )
        _save_plan(job_id, plan)
        _update(
            job_id,
            JobStatus.PLANNING,
            10,
            "Plan ready",
            f"Generated {len(plan.queries)} search queries, {len(plan.subquestions)} sub-questions",
        )

        if cancel.is_set():
            return _mark_cancelled(job_id)

        # --- SEARCH ---
        searcher = ResearchSearcher(max_sources=max_sources)
        all_queries = list(plan.queries)
        current_round = 0

        def on_query_done(done: int, total: int, query: str) -> None:
            pct = 15 + int((done / total) * 25)
            _update(
                job_id, JobStatus.SEARCHING, pct, f"Searching {done}/{total}", f"Searched: {query}"
            )

        _update(
            job_id,
            JobStatus.SEARCHING,
            15,
            "Searching web",
            f"Running {len(plan.queries)} queries...",
        )
        search_results = searcher.search_multiple(
            plan.queries,
            on_query_done=on_query_done,
            cancelled=cancel.is_set,
        )
        if cancel.is_set():
            return _mark_cancelled(job_id)

        _update(
            job_id,
            JobStatus.FETCHING,
            42,
            "Fetching sources",
            f"Fetching top {min(max_sources, len(search_results))} sources...",
        )
        sources = searcher.fetch_sources(
            search_results,
            max_pages=max_sources,
            cancelled=cancel.is_set,
        )
        _save_sources(job_id, sources, all_queries)
        if cancel.is_set():
            return _mark_cancelled(job_id)

        entity_terms = extract_entity_terms(effective_query, plan)
        intent_filtered_topics = (TOPIC_AI_CODING_TOOLS, TOPIC_PRODUCT_COMPARISON)
        if entity_terms or plan.topic_intent in intent_filtered_topics:
            sources = filter_irrelevant_sources(
                sources,
                entity_terms,
                plan=plan,
                user_query=effective_query,
            )

        fetched_count = sum(1 for s in sources if s.fetched)
        rejected_count = sum(1 for s in sources if s.fetch_status == "rejected")
        failed_count = sum(1 for s in sources if s.fetch_status == "failed")
        _update(
            job_id,
            JobStatus.EXTRACTING,
            55,
            "Extracting evidence",
            (
                f"Fetched {fetched_count} pages ({failed_count} failed, "
                f"{rejected_count} rejected), extracting evidence..."
            ),
        )

        evidence = extract_evidence(
            sources, plan, entity_terms=entity_terms, user_query=effective_query
        )

        for src in sources:
            src.evidence_count = sum(1 for e in evidence if e.source_id == src.id)

        _save_evidence(job_id, evidence)
        _save_sources(job_id, sources, all_queries)

        _update(
            job_id,
            JobStatus.EXTRACTING,
            60,
            "Checking gaps",
            f"Extracted {len(evidence)} evidence chunks, checking for gaps...",
        )
        gaps = identify_gaps(plan, evidence, user_query=effective_query)

        # --- FOLLOW-UP ROUNDS ---
        while current_round < max_rounds - 1 and gaps and not cancel.is_set():
            current_round += 1
            followup_queries = generate_followup_queries(effective_query, plan, gaps, ollama=ollama)
            if not followup_queries:
                break

            all_queries.extend(followup_queries)
            _update(
                job_id,
                JobStatus.SEARCHING,
                62 + current_round * 5,
                f"Follow-up round {current_round}",
                f"Running {len(followup_queries)} follow-up queries...",
            )

            new_results = searcher.search_multiple(
                followup_queries,
                cancelled=cancel.is_set,
            )
            if cancel.is_set():
                return _mark_cancelled(job_id)

            if new_results:
                remaining_pages = max_sources - fetched_count
                if remaining_pages <= 0:
                    gaps = []
                    break
                new_sources = searcher.fetch_sources(
                    new_results,
                    max_pages=min(5, remaining_pages),
                    cancelled=cancel.is_set,
                )
                next_id = max((s.id for s in sources), default=0) + 1
                for s in new_sources:
                    s.id = next_id
                    next_id += 1
                sources.extend(new_sources)
                fetched_count = sum(1 for s in sources if s.fetched)

                if entity_terms or plan.topic_intent in intent_filtered_topics:
                    filter_irrelevant_sources(
                        new_sources,
                        entity_terms,
                        plan=plan,
                        user_query=effective_query,
                    )
                evidence = extract_evidence(
                    sources,
                    plan,
                    entity_terms=entity_terms,
                    user_query=effective_query,
                )
                for src in sources:
                    src.evidence_count = sum(1 for e in evidence if e.source_id == src.id)
                _save_sources(job_id, sources, all_queries)
                _save_evidence(job_id, evidence)
                gaps = identify_gaps(plan, evidence, user_query=effective_query)

        if cancel.is_set():
            return _mark_cancelled(job_id)

        # --- SYNTHESIS ---
        _update(
            job_id,
            JobStatus.SYNTHESIZING,
            75,
            "Synthesizing report",
            f"Writing report from {len(evidence)} evidence chunks, {fetched_count} sources...",
        )

        report = synthesize_report(
            user_query,
            plan,
            evidence,
            sources,
            gaps,
            ollama=get_llm_client(num_predict=800, timeout=300, route_name=route_name),
            depth=depth,
        )

        _save_final(job_id, report, sources, evidence, all_queries, plan, gaps, memory_keys)
        _update(
            job_id,
            JobStatus.COMPLETED,
            100,
            "Research complete",
            f"Report ready: {len(evidence)} evidence chunks from {fetched_count} sources",
        )

    except Exception as exc:
        logger.exception("Research pipeline failed for job %s", job_id)
        update_job_status(
            job_id,
            JobStatus.FAILED.value,
            error=str(exc),
            current_step="Pipeline failed",
            progress_percent=0,
        )
    finally:
        with _lock:
            _active_jobs.pop(job_id, None)


def _update(job_id: str, status: JobStatus, pct: int, step: str, message: str) -> None:
    now = datetime.now(UTC).isoformat()
    job_data = load_job(job_id)
    if not job_data:
        return
    if job_data.get("status") == JobStatus.CANCELLED.value:
        return
    log = job_data.get("progress_log", [])
    log.append(
        ProgressEvent(
            status=status.value,
            progress_percent=pct,
            current_step=step,
            message=message,
            timestamp=now,
        ).model_dump()
    )

    update_job_status(
        job_id,
        status.value,
        progress_percent=pct,
        current_step=step,
        progress_log_json=log,
    )


def _save_plan(job_id: str, plan) -> None:
    update_job_status(
        job_id,
        JobStatus.PLANNING.value,
        plan_json=plan.model_dump(),
        generated_queries_json=plan.queries,
    )


def _save_sources(job_id: str, sources, queries) -> None:
    update_job_status(
        job_id,
        JobStatus.FETCHING.value,
        sources_json=[s.model_dump() for s in sources],
        generated_queries_json=queries,
    )


def _save_evidence(job_id: str, evidence) -> None:
    update_job_status(
        job_id,
        JobStatus.EXTRACTING.value,
        evidence_json=[e.model_dump() for e in evidence],
    )


def _save_final(job_id, report, sources, evidence, queries, plan, gaps, memory_keys) -> None:
    data = load_job(job_id)
    if data:
        fetched = [s for s in sources if s.fetched]
        failed = [s for s in sources if s.fetch_status == "failed"]
        data["report"] = report
        data["sources"] = [s.model_dump() for s in sources]
        data["evidence_chunks"] = [e.model_dump() for e in evidence]
        data["generated_queries"] = queries
        data["plan"] = plan.model_dump()
        data["metadata"] = {
            "total_sources": len(sources),
            "fetched_sources": len(fetched),
            "failed_sources": len(failed),
            "evidence_chunks": len(evidence),
            "queries_run": len(queries),
            "gaps": gaps or [],
            "memory_used": memory_keys,
            "topic_intent": plan.topic_intent,
            "normalized_entities": plan.normalized_entities,
            "comparison_tools": plan.comparison_tools,
            "original_query": plan.original_query,
            "normalized_query": plan.normalized_query,
            "normalization_reason": plan.normalization_reason,
            "domain_hint": plan.domain_hint,
            "qualifiers": plan.qualifiers,
            "ai_workload_focus": plan.ai_workload_focus,
            "product_pair": plan.product_pair,
            "fetch_summary": {
                "success": len(fetched),
                "failed": len(failed),
                "skipped": len(sources) - len(fetched) - len(failed),
                "failure_reasons": list({s.fetch_error for s in failed if s.fetch_error})[:5],
            },
        }
        data["status"] = JobStatus.COMPLETED.value
        data["progress_percent"] = 100
        data["current_step"] = "Research complete"
        save_job(data)


def _mark_cancelled(job_id: str) -> None:
    update_job_status(
        job_id,
        JobStatus.CANCELLED.value,
        current_step="Cancelled by user",
    )
    with _lock:
        _active_jobs.pop(job_id, None)

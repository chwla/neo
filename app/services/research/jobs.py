"""Research job orchestrator: runs the full research pipeline in a background thread."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone

from app.services.ollama_client import OllamaClient
from app.services.research.evidence import (
    extract_entity_terms,
    extract_evidence,
    filter_irrelevant_sources,
    identify_gaps,
)
from app.services.research.topic_intent import TOPIC_AI_CODING_TOOLS, classify_topic_intent
from app.services.research.product_intent import TOPIC_PRODUCT_COMPARISON, normalize_user_query
from app.services.research.memory_scope import retrieve_scoped_memory
from app.services.research.planner import generate_followup_queries, generate_plan
from app.services.research.searcher import ResearchSearcher
from app.services.research.store import load_job, save_job, update_job_status
from app.services.research.synthesizer import synthesize_report
from app.services.research.types import (
    DEPTH_CONFIG,
    DepthMode,
    JobStatus,
    ProgressEvent,
    ResearchJob,
)

logger = logging.getLogger(__name__)

_active_jobs: dict[str, threading.Event] = {}
_lock = threading.Lock()


def create_job(
    user_query: str,
    depth: DepthMode = DepthMode.STANDARD,
    max_sources: int | None = None,
    max_rounds: int | None = None,
) -> ResearchJob:
    config = DEPTH_CONFIG[depth]
    now = datetime.now(timezone.utc).isoformat()
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

    thread = threading.Thread(
        target=_run_research_pipeline,
        args=(job_id, cancel_event),
        daemon=True,
        name=f"research-{job_id}",
    )
    thread.start()
    return True


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
        memory_context, memory_keys = retrieve_scoped_memory(user_query)
        if memory_context:
            _update(job_id, JobStatus.PLANNING, 7, "Memory loaded",
                    f"Loaded memory context: {', '.join(memory_keys)}")

        ollama = OllamaClient(num_predict=512)

        intent = classify_topic_intent(effective_query, original_query=user_query)
        plan = generate_plan(
            effective_query, depth,
            memory_context=memory_context,
            ollama=ollama,
            topic_intent=intent,
            original_query=user_query,
        )
        _save_plan(job_id, plan)
        _update(job_id, JobStatus.PLANNING, 10, "Plan ready",
                f"Generated {len(plan.queries)} search queries, {len(plan.subquestions)} sub-questions")

        if cancel.is_set():
            return _mark_cancelled(job_id)

        # --- SEARCH ---
        searcher = ResearchSearcher(max_sources=max_sources)
        all_queries = list(plan.queries)
        current_round = 0

        def on_query_done(done: int, total: int, query: str) -> None:
            pct = 15 + int((done / total) * 25)
            _update(job_id, JobStatus.SEARCHING, pct,
                    f"Searching {done}/{total}", f"Searched: {query}")

        _update(job_id, JobStatus.SEARCHING, 15, "Searching web", f"Running {len(plan.queries)} queries...")
        search_results = searcher.search_multiple(
            plan.queries,
            on_query_done=on_query_done,
            cancelled=cancel.is_set,
        )
        if cancel.is_set():
            return _mark_cancelled(job_id)

        _update(job_id, JobStatus.FETCHING, 42, "Fetching sources",
                f"Fetching top {min(max_sources, len(search_results))} sources...")
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
                sources, entity_terms, plan=plan, user_query=effective_query,
            )

        fetched_count = sum(1 for s in sources if s.fetched)
        rejected_count = sum(1 for s in sources if s.fetch_status == "rejected")
        failed_count = sum(1 for s in sources if s.fetch_status == "failed")
        _update(job_id, JobStatus.EXTRACTING, 55, "Extracting evidence",
                f"Fetched {fetched_count} pages ({failed_count} failed, {rejected_count} rejected), extracting evidence...")

        evidence = extract_evidence(sources, plan, entity_terms=entity_terms, user_query=effective_query)

        for src in sources:
            src.evidence_count = sum(1 for e in evidence if e.source_id == src.id)

        _save_evidence(job_id, evidence)
        _save_sources(job_id, sources, all_queries)

        _update(job_id, JobStatus.EXTRACTING, 60, "Checking gaps",
                f"Extracted {len(evidence)} evidence chunks, checking for gaps...")
        gaps = identify_gaps(plan, evidence, user_query=effective_query)

        # --- FOLLOW-UP ROUNDS ---
        while current_round < max_rounds - 1 and gaps and not cancel.is_set():
            current_round += 1
            followup_queries = generate_followup_queries(effective_query, plan, gaps, ollama=ollama)
            if not followup_queries:
                break

            all_queries.extend(followup_queries)
            _update(job_id, JobStatus.SEARCHING, 62 + current_round * 5,
                    f"Follow-up round {current_round}",
                    f"Running {len(followup_queries)} follow-up queries...")

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
                        new_sources, entity_terms, plan=plan, user_query=effective_query,
                    )
                evidence = extract_evidence(
                    sources, plan, entity_terms=entity_terms, user_query=effective_query,
                )
                for src in sources:
                    src.evidence_count = sum(1 for e in evidence if e.source_id == src.id)
                _save_sources(job_id, sources, all_queries)
                _save_evidence(job_id, evidence)
                gaps = identify_gaps(plan, evidence, user_query=effective_query)

        if cancel.is_set():
            return _mark_cancelled(job_id)

        # --- SYNTHESIS ---
        _update(job_id, JobStatus.SYNTHESIZING, 75, "Synthesizing report",
                f"Writing report from {len(evidence)} evidence chunks, {fetched_count} sources...")

        report = synthesize_report(
            user_query, plan, evidence, sources, gaps,
            ollama=OllamaClient(num_predict=800, timeout=300),
            depth=depth,
        )

        _save_final(job_id, report, sources, evidence, all_queries, plan, gaps, memory_keys)
        _update(job_id, JobStatus.COMPLETED, 100, "Research complete",
                f"Report ready: {len(evidence)} evidence chunks from {fetched_count} sources")

    except Exception as exc:
        logger.exception("Research pipeline failed for job %s", job_id)
        update_job_status(
            job_id, JobStatus.FAILED.value,
            error=str(exc),
            current_step="Pipeline failed",
            progress_percent=0,
        )
    finally:
        with _lock:
            _active_jobs.pop(job_id, None)


def _update(job_id: str, status: JobStatus, pct: int, step: str, message: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    job_data = load_job(job_id)
    if not job_data:
        return
    if job_data.get("status") == JobStatus.CANCELLED.value:
        return
    log = job_data.get("progress_log", [])
    log.append(ProgressEvent(
        status=status.value,
        progress_percent=pct,
        current_step=step,
        message=message,
        timestamp=now,
    ).model_dump())

    update_job_status(
        job_id, status.value,
        progress_percent=pct,
        current_step=step,
        progress_log_json=log,
    )


def _save_plan(job_id: str, plan) -> None:
    update_job_status(
        job_id, JobStatus.PLANNING.value,
        plan_json=plan.model_dump(),
        generated_queries_json=plan.queries,
    )


def _save_sources(job_id: str, sources, queries) -> None:
    update_job_status(
        job_id, JobStatus.FETCHING.value,
        sources_json=[s.model_dump() for s in sources],
        generated_queries_json=queries,
    )


def _save_evidence(job_id: str, evidence) -> None:
    update_job_status(
        job_id, JobStatus.EXTRACTING.value,
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
        job_id, JobStatus.CANCELLED.value,
        current_step="Cancelled by user",
    )
    with _lock:
        _active_jobs.pop(job_id, None)

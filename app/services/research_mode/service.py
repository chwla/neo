"""Orchestration for a bounded, auditable research workflow."""

from __future__ import annotations

from app.services.memory_retrieval import MemoryRetrievalService
from app.services.memory_retrieval.types import MemoryItemCreate
from app.services.research_mode import store
from app.services.research_mode.citations import validate
from app.services.research_mode.collector import collect_memory, collect_web
from app.services.research_mode.confidence import run_confidence
from app.services.research_mode.conflicts import detect
from app.services.research_mode.evidence import from_memory, from_web
from app.services.research_mode.planner import make_plan
from app.services.research_mode.redaction import safe_text
from app.services.research_mode.report import render
from app.services.research_mode.synthesizer import claims_from_evidence
from app.services.research_mode.types import ResearchPlanRequest, ResearchRunRequest


class ResearchModeService:
    def __init__(self) -> None:
        store.initialize_research_mode_tables()

    def plan(self, request: ResearchPlanRequest) -> dict:
        return make_plan(
            request.question, request.mode, request.freshness_required, request.depth
        ).model_dump()

    def run(self, request: ResearchRunRequest) -> dict:
        plan = self.plan(
            ResearchPlanRequest(
                question=request.question,
                mode=request.mode,
                freshness_required=request.freshness_required,
                depth=request.depth,
            )
        )
        run = store.create_run(request.model_dump(), plan)
        try:
            memory = (
                collect_memory(request.question)
                if request.include_memory
                else {"results": [], "retrieval": None}
            )
            memory_ids = [memory["retrieval"]["id"]] if memory.get("retrieval") else []
            store.update_run(run["id"], status="gathering", memory_retrieval_ids_json=memory_ids)
            planned_queries = list(plan.get("search_queries") or [request.question])[
                : request.max_search_runs
            ]
            source_budget = max(1, request.max_sources // max(1, len(planned_queries)))
            web_results = []
            for query in planned_queries:
                try:
                    web_results.append(
                        collect_web(
                            query,
                            request.mode,
                            request.freshness_required,
                            source_budget,
                            request.include_conflict_analysis,
                        )
                    )
                except (LookupError, RuntimeError, ValueError):
                    # A single planned query cannot expand scope or create a
                    # factual fallback when its bounded provider fails.
                    continue
            web_ids = list(dict.fromkeys(item["id"] for item in web_results if item.get("id")))
            raw_evidence = from_memory(memory)
            for web_result in web_results:
                raw_evidence.extend(from_web(web_result))
            deduplicated: dict[tuple[str, str | None, str], dict] = {}
            for item in raw_evidence:
                key = (
                    item["source_type"],
                    item.get("source_id"),
                    item["evidence_text"],
                )
                deduplicated.setdefault(key, item)
            raw_evidence = list(deduplicated.values())
            evidence = [store.add_evidence(run["id"], item) for item in raw_evidence]
            web_conflicts = [
                conflict for item in web_results for conflict in item.get("conflicts", [])
            ]
            conflicts = (
                [store.add_conflict(run["id"], item) for item in detect(web_conflicts, evidence)]
                if request.include_conflict_analysis
                else []
            )
            claims = [
                store.add_claim(run["id"], item)
                for item in claims_from_evidence(evidence, conflicts)
            ]
            validation = validate(claims, evidence)
            confidence = run_confidence(evidence, claims, conflicts, len(memory.get("results", [])))
            uncertainty = [item["claim"] for item in claims if item["status"] != "supported"]
            full_run = store.get_run(run["id"]) or run
            full_run["confidence"] = confidence
            text, sections = render(full_run, evidence, claims, conflicts, validation)
            store.add_report(
                run["id"],
                f"Research: {request.question[:100]}",
                text,
                sections,
                [label for claim in claims for label in claim.get("citation_ids", [])],
                confidence,
            )
            status = "completed" if validation["passed"] else "needs_review"
            stored = (
                store.update_run(
                    run["id"],
                    status=status,
                    web_search_run_ids_json=web_ids,
                    report_text=text,
                    executive_summary=safe_text(text.split("## Research Question")[0], 700),
                    confidence_json=confidence,
                    uncertainty_json=uncertainty,
                    completed_at=store.now(),
                )
                or run
            )
            self._write_memory(run["id"], claims, conflicts, confidence)
            return self.detail(stored["id"])
        except Exception as exc:
            store.update_run(
                run["id"], status="blocked", error=safe_text(exc, 500), completed_at=store.now()
            )
            return self.detail(run["id"])

    def _write_memory(
        self, run_id: str, claims: list[dict], conflicts: list[dict], confidence: dict
    ) -> list[dict]:
        service = MemoryRetrievalService()
        items = []
        for claim in claims:
            if claim.get("status") != "supported":
                continue
            items.append(
                service.create(
                    MemoryItemCreate(
                        scope_type="research_run",
                        scope_id=run_id,
                        source_type="research_mode",
                        source_id=claim["id"],
                        memory_type="research_finding",
                        title=f"Research finding: {claim['claim'][:120]}",
                        content_text=claim["claim"],
                        content_json={
                            "citation_ids": claim.get("citation_ids", []),
                            "confidence": claim.get("confidence"),
                        },
                        tags=["research", "finding"],
                        confidence=float(claim.get("confidence") or 0.0),
                    )
                )
            )
        for conflict in conflicts:
            items.append(
                service.create(
                    MemoryItemCreate(
                        scope_type="research_run",
                        scope_id=run_id,
                        source_type="research_mode",
                        source_id=conflict["id"],
                        memory_type="open_item",
                        title=f"Research conflict: {conflict['topic']}",
                        content_text=conflict["recommended_resolution"],
                        content_json=conflict,
                        tags=["research", "conflict"],
                        confidence=0.5,
                    )
                )
            )
        if not claims:
            items.append(
                service.create(
                    MemoryItemCreate(
                        scope_type="research_run",
                        scope_id=run_id,
                        source_type="research_mode",
                        source_id="summary",
                        memory_type="safety_note",
                        title="Research evidence gap",
                        content_text=(
                            "No supported research claims were produced; "
                            "do not infer factual conclusions."
                        ),
                        content_json={"confidence": confidence},
                        tags=["research", "uncertainty"],
                        confidence=0.2,
                    )
                )
            )
        return items

    def detail(self, run_id: str) -> dict:
        run = store.get_run(run_id)
        if not run:
            raise LookupError("Research run not found.")
        evidence = store.related("workspace_research_evidence", run_id)
        claims = store.related("workspace_research_claims", run_id)
        conflicts = store.related("workspace_research_conflicts", run_id)
        reports = store.related("workspace_research_reports", run_id)
        return {
            **run,
            "evidence": evidence,
            "claims": claims,
            "conflicts": conflicts,
            "report": reports[-1] if reports else None,
            "citation_validation": validate(claims, evidence),
            "memory_items": MemoryRetrievalService().list_items(
                scope_type="research_run", scope_id=run_id, limit=50
            ),
        }

    def continue_run(self, run_id: str) -> dict:
        current = store.get_run(run_id)
        if not current:
            raise LookupError("Research run not found.")
        return self.run(
            ResearchRunRequest(
                question=current["question"],
                mode=current["mode"],
                freshness_required=(current.get("plan") or {}).get("freshness_required", True),
                created_by=current.get("created_by") or "user",
            )
        )

    def refresh(self, run_id: str) -> dict:
        return self.continue_run(run_id)

    def validate_citations(self, run_id: str) -> dict:
        detail = self.detail(run_id)
        result = validate(detail["claims"], detail["evidence"])
        if not result["passed"]:
            store.update_run(run_id, status="needs_review", error="Citation validation failed.")
        return result

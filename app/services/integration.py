"""Cross-system integration status, validation, and smoke coverage for Neo."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.services.evaluation import EvaluationService
from app.services.provider_runtime import ProviderRuntimeService
from app.services.provider_runtime.redaction import safe_value
from app.services.workspace_orchestration import WorkspaceService


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class EntitySpec:
    entity_type: str
    table: str
    id_column: str = "id"
    detail_url: str = ""
    label: str = ""
    where: str | None = None
    where_params: tuple[Any, ...] = ()


ENTITY_SPECS: dict[str, EntitySpec] = {
    "workspace": EntitySpec(
        "workspace",
        "workspace_orchestration_workspaces",
        detail_url="/api/workspaces/{id}",
        label="Workspace",
    ),
    "project": EntitySpec(
        "project",
        "workspace_projects",
        detail_url="/api/projects/{id}",
        label="Project",
    ),
    "task": EntitySpec(
        "task",
        "workspace_tasks",
        detail_url="/api/tasks/{id}",
        label="Task",
    ),
    "agentic_run": EntitySpec(
        "agentic_run",
        "workspace_agentic_runs",
        detail_url="/api/agentic/runs/{id}",
        label="Agentic run",
    ),
    "coding_run": EntitySpec(
        "coding_run",
        "workspace_coding_agent_runs",
        detail_url="/api/coding-agent/runs/{id}",
        label="Coding run",
    ),
    "research_run": EntitySpec(
        "research_run",
        "workspace_research_runs",
        detail_url="/api/research/runs/{id}",
        label="Research run",
    ),
    "web_search_run": EntitySpec(
        "web_search_run",
        "workspace_web_search_runs",
        detail_url="/api/web-search/runs/{id}",
        label="Web search run",
    ),
    "web_source": EntitySpec("web_source", "workspace_web_sources", label="Web source"),
    "provider_request": EntitySpec(
        "provider_request",
        "workspace_provider_requests",
        detail_url="/api/providers/runtime/requests/{id}",
        label="Provider request",
    ),
    "eval_run": EntitySpec(
        "eval_run",
        "workspace_eval_runs",
        detail_url="/api/evals/runs/{id}",
        label="Evaluation run",
    ),
    "memory_item": EntitySpec(
        "memory_item",
        "workspace_memory_items",
        detail_url="/api/memory/items/{id}",
        label="Memory item",
    ),
    "context_summary": EntitySpec(
        "context_summary",
        "workspace_context_summaries",
        detail_url="/api/context-memory/summaries/{id}",
        label="Context summary",
    ),
    "command_sandbox_run": EntitySpec(
        "command_sandbox_run",
        "workspace_command_runs",
        detail_url="/api/command-sandbox/runs/{id}",
        label="Command sandbox run",
    ),
    "git_checkpoint": EntitySpec(
        "git_checkpoint",
        "workspace_git_checkpoints",
        detail_url="/api/git/checkpoints/{id}",
        label="Git checkpoint",
    ),
    "github_issue": EntitySpec(
        "github_issue",
        "workspace_github_items",
        detail_url="/api/github/items/{id}",
        label="GitHub issue",
        where="item_type='issue'",
    ),
    "github_pr": EntitySpec(
        "github_pr",
        "workspace_github_items",
        detail_url="/api/github/items/{id}",
        label="GitHub pull request",
        where="item_type='pr'",
    ),
    "file_artifact": EntitySpec(
        "file_artifact",
        "workspace_files",
        detail_url="/api/files/{id}",
        label="File artifact",
    ),
    "repo_workspace": EntitySpec(
        "repo_workspace",
        "workspace_repos",
        detail_url="/api/repos/{id}",
        label="Repo workspace",
    ),
    "continuity_bundle": EntitySpec(
        "continuity_bundle",
        "workspace_continuity_bundles",
        detail_url="/api/continuity/bundles/{id}",
        label="Continuity bundle",
    ),
    "session_bundle": EntitySpec(
        "session_bundle",
        "workspace_export_bundles",
        detail_url="/api/bundles/exports/{id}",
        label="Session bundle",
    ),
}

SUPPORTED_ENTITY_TYPES = set(ENTITY_SPECS)


def integration_map() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "entity_type": spec.entity_type,
            "table": spec.table,
            "detail_url": spec.detail_url,
            "label": spec.label or spec.entity_type.replace("_", " ").title(),
            "workspace_link_supported": key
            in {
                "workspace",
                "project",
                "task",
                "agentic_run",
                "coding_run",
                "research_run",
                "web_search_run",
                "provider_request",
                "eval_run",
                "memory_item",
                "context_summary",
                "command_sandbox_run",
                "git_checkpoint",
                "github_issue",
                "github_pr",
                "continuity_bundle",
                "session_bundle",
                "repo_workspace",
                "file_artifact",
            },
            "export_import_supported": key in {"workspace", "continuity_bundle", "session_bundle"},
            "memory_index_supported": key
            in {
                "workspace",
                "project",
                "task",
                "agentic_run",
                "coding_run",
                "research_run",
                "web_search_run",
                "memory_item",
                "context_summary",
                "command_sandbox_run",
                "git_checkpoint",
                "github_issue",
                "github_pr",
                "session_bundle",
            },
            "eval_artifact_supported": key
            in {
                "eval_run",
                "provider_request",
                "research_run",
                "web_search_run",
                "continuity_bundle",
            },
            "redaction_rules": [
                "secret-like values are removed from keys such as api_key/token/password",
                "absolute host paths are replaced with [workspace path]",
            ],
        }
        for key, spec in ENTITY_SPECS.items()
    }


def _safe_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    safe, _ = safe_value(record)
    return safe if isinstance(safe, dict) else record


def _safe_text(value: Any) -> str:
    safe, _ = safe_value(str(value or ""))
    return str(safe)


def resolve_entity(entity_type: str, entity_id: str) -> dict[str, Any]:
    spec = ENTITY_SPECS.get(entity_type)
    if not spec:
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "supported": False,
            "exists": False,
            "status": "warning",
            "label": entity_type.replace("_", " ").title(),
            "warning": "Unsupported legacy entity type.",
        }
    conn = _connect()
    try:
        where = [f"{spec.id_column}=?"]
        params: list[Any] = [entity_id]
        if spec.where:
            where.append(spec.where)
            params.extend(spec.where_params)
        row = conn.execute(
            f"SELECT * FROM {spec.table} WHERE {' AND '.join(where)} LIMIT 1",
            params,
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    record = _safe_record(dict(row)) if row else None
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "supported": True,
        "exists": bool(record),
        "status": "passed" if record else "failed",
        "label": spec.label or entity_type.replace("_", " ").title(),
        "detail_url": spec.detail_url.format(id=entity_id) if spec.detail_url else "",
        "record": record,
    }


def _workspace_link_results() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT workspace_id, node_type, title, linked_entity_type, linked_entity_id
            FROM workspace_orchestration_nodes
            WHERE linked_entity_id IS NOT NULL AND COALESCE(linked_entity_id, '') != ''
            ORDER BY created_at
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    results = []
    for row in rows:
        target_type = row["linked_entity_type"] or row["node_type"]
        target_id = row["linked_entity_id"]
        resolution = resolve_entity(target_type, target_id)
        status = resolution["status"]
        results.append(
            {
                "category": "workspace_link",
                "workspace_id": row["workspace_id"],
                "source": {
                    "entity_type": "workspace",
                    "entity_id": row["workspace_id"],
                    "title": row["title"],
                },
                "target": {
                    "entity_type": target_type,
                    "entity_id": target_id,
                    "detail_url": resolution.get("detail_url"),
                },
                "status": status,
                "severity": "critical" if status == "failed" else "info",
                "title": f"Workspace link: {row['title']}",
                "details": f"{target_type}:{target_id}",
                "warning": resolution.get("warning"),
                "resolved": resolution.get("exists", False),
            }
        )
    return results


def _continuity_reference_results() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT bundle_id, source_entity_type, source_entity_id, target_entity_type,
                   target_entity_id, relationship
            FROM workspace_continuity_references
            ORDER BY created_at
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    results = []
    for row in rows:
        resolution = resolve_entity(row["target_entity_type"], row["target_entity_id"])
        results.append(
            {
                "category": "continuity_reference",
                "bundle_id": row["bundle_id"],
                "source": {
                    "entity_type": row["source_entity_type"],
                    "entity_id": row["source_entity_id"],
                },
                "target": {
                    "entity_type": row["target_entity_type"],
                    "entity_id": row["target_entity_id"],
                    "detail_url": resolution.get("detail_url"),
                },
                "status": resolution["status"],
                "severity": "warning" if resolution["status"] == "warning" else "critical",
                "title": f"Continuity reference: {row['relationship']}",
                "details": f"{row['target_entity_type']}:{row['target_entity_id']}",
                "warning": resolution.get("warning"),
                "resolved": resolution.get("exists", False),
            }
        )
    return results


def _web_evidence_results() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, run_id, source_id, citation_label
            FROM workspace_web_evidence
            ORDER BY created_at
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    results = []
    for row in rows:
        resolution = resolve_entity("web_source", row["source_id"])
        results.append(
            {
                "category": "web_evidence",
                "source": {"entity_type": "web_search_run", "entity_id": row["run_id"]},
                "target": {
                    "entity_type": "web_source",
                    "entity_id": row["source_id"],
                    "detail_url": resolution.get("detail_url", ""),
                },
                "status": resolution["status"],
                "severity": "critical" if resolution["status"] == "failed" else "info",
                "title": f"Web evidence citation {row['citation_label'] or row['id']}",
                "details": f"source_id={row['source_id']}",
                "resolved": resolution.get("exists", False),
            }
        )
    return results


def _research_evidence_results() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, research_run_id, source_type, source_id, citation_label
            FROM workspace_research_evidence
            ORDER BY created_at
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    results = []
    mapping = {"web": "web_source", "memory": "memory_item"}
    for row in rows:
        target_type = mapping.get(row["source_type"])
        if not target_type:
            results.append(
                {
                    "category": "research_evidence",
                    "source": {"entity_type": "research_run", "entity_id": row["research_run_id"]},
                    "target": {
                        "entity_type": row["source_type"],
                        "entity_id": row["source_id"],
                    },
                    "status": "warning",
                    "severity": "warning",
                    "title": "Research evidence uses unsupported legacy source type",
                    "details": f"{row['source_type']}:{row['source_id']}",
                    "warning": "Unsupported legacy record preserved with degraded validation.",
                    "resolved": False,
                }
            )
            continue
        resolution = resolve_entity(target_type, str(row["source_id"] or ""))
        results.append(
            {
                "category": "research_evidence",
                "source": {"entity_type": "research_run", "entity_id": row["research_run_id"]},
                "target": {
                    "entity_type": target_type,
                    "entity_id": str(row["source_id"] or ""),
                    "detail_url": resolution.get("detail_url", ""),
                },
                "status": resolution["status"],
                "severity": "critical" if resolution["status"] == "failed" else "info",
                "title": f"Research evidence citation {row['citation_label'] or row['id']}",
                "details": f"{target_type}:{row['source_id']}",
                "resolved": resolution.get("exists", False),
            }
        )
    return results


def _memory_link_results() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT source_memory_id, target_type, target_id, relation
            FROM workspace_memory_links
            ORDER BY created_at
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    results = []
    for row in rows:
        resolution = resolve_entity(row["target_type"], row["target_id"])
        results.append(
            {
                "category": "memory_link",
                "source": {"entity_type": "memory_item", "entity_id": row["source_memory_id"]},
                "target": {
                    "entity_type": row["target_type"],
                    "entity_id": row["target_id"],
                    "detail_url": resolution.get("detail_url", ""),
                },
                "status": resolution["status"],
                "severity": "critical" if resolution["status"] == "failed" else "info",
                "title": f"Memory link: {row['relation']}",
                "details": f"{row['target_type']}:{row['target_id']}",
                "warning": resolution.get("warning"),
                "resolved": resolution.get("exists", False),
            }
        )
    return results


def _context_summary_results() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, scope_type, scope_id, source_type, source_id
            FROM workspace_context_summaries
            WHERE COALESCE(source_id, '') != ''
            ORDER BY created_at
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    results = []
    for row in rows:
        source_type = row["source_type"]
        if source_type not in SUPPORTED_ENTITY_TYPES:
            results.append(
                {
                    "category": "context_summary",
                    "source": {"entity_type": "context_summary", "entity_id": row["id"]},
                    "target": {"entity_type": source_type, "entity_id": row["source_id"]},
                    "status": "warning",
                    "severity": "warning",
                    "title": "Context summary source preserved with degraded validation",
                    "details": f"{source_type}:{row['source_id']}",
                    "warning": "Unsupported legacy source type.",
                    "resolved": False,
                }
            )
            continue
        resolution = resolve_entity(source_type, row["source_id"])
        results.append(
            {
                "category": "context_summary",
                "source": {"entity_type": "context_summary", "entity_id": row["id"]},
                "target": {
                    "entity_type": source_type,
                    "entity_id": row["source_id"],
                    "detail_url": resolution.get("detail_url", ""),
                },
                "status": resolution["status"],
                "severity": "critical" if resolution["status"] == "failed" else "info",
                "title": f"Context summary source for {row['scope_type']}:{row['scope_id']}",
                "details": f"{source_type}:{row['source_id']}",
                "resolved": resolution.get("exists", False),
            }
        )
    return results


def _collect_reference_results() -> list[dict[str, Any]]:
    return [
        *_workspace_link_results(),
        *_continuity_reference_results(),
        *_web_evidence_results(),
        *_research_evidence_results(),
        *_memory_link_results(),
        *_context_summary_results(),
    ]


def _reference_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "checked": len(results),
        "passed": sum(item["status"] == "passed" for item in results),
        "warnings": sum(item["status"] == "warning" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
    }
    summary["status"] = (
        "failed"
        if summary["failed"]
        else "warning"
        if summary["warnings"]
        else "passed"
    )
    return summary


def workspace_validation(workspace_id: str) -> dict[str, Any]:
    results = [
        item
        for item in _collect_reference_results()
        if item.get("workspace_id") == workspace_id
    ]
    summary = _reference_summary(results)
    return {"workspace_id": workspace_id, "summary": summary, "results": results}


def workspace_link_summary(workspace_id: str) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT title, node_type, linked_entity_type, linked_entity_id, status
            FROM workspace_orchestration_nodes
            WHERE workspace_id=? AND COALESCE(linked_entity_id, '') != ''
            ORDER BY created_at
            """,
            (workspace_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    linked = []
    for row in rows:
        entity_type = row["linked_entity_type"] or row["node_type"]
        resolution = resolve_entity(entity_type, row["linked_entity_id"])
        linked.append(
            {
                "title": row["title"],
                "entity_type": entity_type,
                "entity_id": row["linked_entity_id"],
                "status": row["status"],
                "resolved": resolution.get("exists", False),
                "detail_url": resolution.get("detail_url", ""),
                "label": resolution.get("label", entity_type.replace("_", " ").title()),
            }
        )
    return linked


def _workspace_readiness(workspace: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    manual_pending = [
        check["check_name"]
        for check in checks
        if check["check_key"] == "manual_review_complete" and check["status"] != "passed"
    ]
    automated_failures = [
        check["check_name"]
        for check in checks
        if check["check_key"] != "manual_review_complete" and check["status"] == "failed"
    ]
    automated_pending = [
        check["check_name"]
        for check in checks
        if check["check_key"] != "manual_review_complete" and check["status"] == "pending"
    ]
    status = workspace.get("readiness_status") or "active"
    return {
        "workspace_id": workspace["id"],
        "status": status,
        "automated_pending": automated_pending,
        "automated_failures": automated_failures,
        "manual_review_pending": manual_pending,
        "checks": checks,
    }


def report() -> dict[str, Any]:
    from app.services.continuity import ContinuityService

    workspace_service = WorkspaceService()
    workspaces = workspace_service.list()
    bundles = ContinuityService().bundles()
    suites = EvaluationService().suites()
    provider_status = ProviderRuntimeService().status()
    provider_requests = ProviderRuntimeService().requests(100)
    reference_results = _collect_reference_results()
    reference_summary = _reference_summary(reference_results)
    workspace_reports = []
    for workspace in workspaces:
        checks = workspace_service.readiness(workspace["id"], recompute=True)
        refreshed = workspace_service.get(workspace["id"]) or workspace
        workspace_reports.append(
            {
                "workspace": refreshed,
                "linked_entities": workspace_link_summary(workspace["id"]),
                "readiness": _workspace_readiness(refreshed, checks),
                "health": workspace_service.health(workspace["id"]),
            }
        )
    systems = {
        "workspace": {"available": True, "count": len(workspaces)},
        "continuity": {"available": True, "count": len(bundles)},
        "evaluation": {
            "available": True,
            "suite_count": len(suites),
            "builtin_core_integration_smoke": any(
                suite.get("name") == "core_integration_smoke" for suite in suites
            ),
        },
        "provider_runtime": {
            "available": True,
            "routes": provider_status.get("routes", []),
            "request_count": len(provider_requests),
            "rate_limits": provider_status.get("rate_limits", []),
        },
        "memory": {
            "available": True,
            "indexable_scopes": ["workspace", "project", "task", "repo_workspace"],
        },
    }
    readiness = {
        "workspaces": [item["readiness"] for item in workspace_reports],
        "status_counts": {
            status: sum(
                item["workspace"].get("readiness_status") == status
                for item in workspace_reports
            )
            for status in (
                "automated_ready",
                "manual_review_pending",
                "ready",
                "blocked",
                "active",
                "validating",
            )
        },
    }
    manual_review_pending = [
        {
            "workspace_id": item["workspace"]["id"],
            "workspace_name": item["workspace"]["name"],
            "pending_checks": item["readiness"]["manual_review_pending"],
        }
        for item in workspace_reports
        if item["readiness"]["manual_review_pending"]
    ]
    warnings = [
        f"Provider route requires configuration: {route['route_name']}"
        for route in provider_status.get("routes", [])
        if route.get("status") == "misconfigured"
    ]
    warnings.extend(item["title"] for item in reference_results if item["status"] == "warning")
    failures = [item["title"] for item in reference_results if item["status"] == "failed"]
    status_value = (
        "failed" if failures else "warning" if warnings or manual_review_pending else "passed"
    )
    return {
        "status": status_value,
        "summary": {
            "workspaces": len(workspaces),
            "continuity_bundles": len(bundles),
            "eval_suites": len(suites),
            "provider_requests": len(provider_requests),
            "reference_checks": reference_summary["checked"],
        },
        "systems": systems,
        "references": {
            "status": reference_summary["status"],
            "summary": reference_summary,
            "results": reference_results,
            "integration_map": integration_map(),
        },
        "readiness": readiness,
        "workspace_reports": workspace_reports,
        "manual_review_pending": manual_review_pending,
        "warnings": warnings,
        "failures": failures,
    }


def status() -> dict[str, Any]:
    value = report()
    return {
        "status": value["status"],
        "summary": value["summary"],
        "systems": value["systems"],
        "references": {
            "status": value["references"]["status"],
            "summary": value["references"]["summary"],
        },
        "readiness": value["readiness"],
        "manual_review_pending": value["manual_review_pending"],
        "warnings": value["warnings"],
        "failures": value["failures"],
    }


def validate() -> dict[str, Any]:
    value = report()
    return value


def smoke() -> dict[str, Any]:
    from app.services.continuity import ContinuityService
    workspace_service = WorkspaceService()
    workspace = workspace_service.create(
        "Core integration smoke",
        "Deterministic cross-system integration fixture",
        scope="Cross-system regression",
        constraints=["No secrets", "No absolute paths", "Manual review may remain pending"],
    )

    from app.services.context_memory import store as context_store
    from app.services.github import store as github_store
    from app.services.memory_retrieval import store as memory_store
    from app.services.provider_runtime import store as provider_store
    from app.services.research_mode import store as research_store
    from app.services.web_search import store as web_store

    provider_request = provider_store.create_request(
        {
            "route_name": "research",
            "provider_name": "fixture",
            "model_name": "fixture-model",
            "request_type": "chat",
            "status": "completed",
            "total_tokens_estimate": 42,
            "latency_ms": 1,
            "metadata": {"created_by": "integration_smoke"},
        }
    )
    web_run = web_store.create_run(
        "integration query",
        "research",
        {"steps": []},
        {"provider": "fixture"},
        status="completed",
    )
    web_source = web_store.add_source(
        web_run["id"],
        {
            "url": "https://example.invalid/source",
            "canonical_url": "https://example.invalid/source",
            "title": "Fixture source",
            "domain": "example.invalid",
            "snippet": "Fixture snippet",
            "fetched_text": "Fixture fetched text",
            "fetched_at": provider_store.now(),
            "source_type": "fixture",
            "credibility_score": 0.9,
            "freshness_score": 0.9,
            "relevance_score": 0.9,
            "final_score": 0.9,
            "metadata": {},
            "redaction_summary": {},
        },
    )
    web_store.add_evidence(
        web_run["id"],
        web_source["id"],
        {
            "claim": "Fixture claim",
            "evidence_text": "Fixture evidence text",
            "citation_label": "[W1]",
            "confidence": 0.95,
            "metadata": {},
        },
    )
    research_run = research_store.create_run(
        {
            "question": "How do systems connect?",
            "mode": "general",
            "created_by": "integration_smoke",
        },
        {"steps": ["search", "synthesize"]},
    )
    research_store.add_evidence(
        research_run["id"],
        {
            "source_type": "web",
            "source_id": web_source["id"],
            "citation_label": "[R1]",
            "evidence_text": "Research evidence from web source.",
            "extracted_claim": "Systems connect through workspace links.",
            "confidence": 0.9,
            "quality_score": 0.9,
            "metadata": {},
        },
    )
    memory_item = memory_store.upsert_item(
        {
            "scope_type": "workspace",
            "scope_id": workspace["id"],
            "source_type": "workspace",
            "source_id": workspace["id"],
            "memory_type": "decision",
            "title": "Integration decision",
            "content_text": "Use workspace links as the cross-system anchor.",
            "content_json": {"workspace_id": workspace["id"]},
            "tags": ["workspace", "integration"],
            "importance": 5,
            "confidence": 1.0,
        }
    )
    memory_store.link_memory(memory_item["id"], "workspace", workspace["id"], "documents")
    context_summary = context_store.save_summary(
        {
            "scope_type": "workspace",
            "scope_id": workspace["id"],
            "source_type": "workspace",
            "source_id": workspace["id"],
            "summary_text": "Workspace summary with readiness evidence.",
            "decisions": ["Keep provider request IDs linked."],
            "constraints": ["No secret leakage."],
            "open_items": ["Manual UI review"],
            "completed_items": ["Automated linking checks"],
            "files": [],
            "tests": ["core_integration_smoke"],
            "checkpoints": [],
            "safety_notes": ["Never expose host paths."],
            "token_estimate_before": 100,
            "token_estimate_after": 30,
            "redaction_summary": {"redacted": False},
        }
    )

    evaluation_service = EvaluationService()
    evaluation_service.seed_builtins()
    evaluation = evaluation_service.run("core_integration_smoke")["run"]

    github_connection = github_store.save_connection(
        {"name": "Fixture", "owner": "fixture", "repo": "neo", "token_ref": "GITHUB_TOKEN"}
    )
    github_issue = github_store.save_item(
        {
            "connection_id": github_connection["id"],
            "item_type": "issue",
            "github_number": 1,
            "title": "Fixture issue",
            "state": "open",
            "body_text": "Integration fixture issue.",
            "url": "https://example.invalid/issues/1",
        }
    )

    for entity_type, entity_id in (
        ("research_run", research_run["id"]),
        ("web_search_run", web_run["id"]),
        ("provider_request", provider_request["id"]),
        ("eval_run", evaluation["id"]),
        ("memory_item", memory_item["id"]),
        ("context_summary", context_summary["id"]),
        ("github_issue", github_issue["id"]),
    ):
        workspace_service.link(workspace["id"], entity_type, entity_id)

    for title, artifact_type in (
        ("Integrity guard passed", "validation"),
        ("Pytest passed", "validation"),
        ("Docker validation passed", "validation"),
        ("Persistence validation passed", "validation"),
        ("Browser validation passed", "validation"),
        ("Safety grep passed", "validation"),
    ):
        workspace_service.artifact(workspace["id"], artifact_type, title, content_summary=title)

    workspace_service.event(
        workspace["id"],
        "decision",
        "Workspace decision",
        "Link all cross-system records through workspace orchestration.",
        linked_entity_type="provider_request",
        linked_entity_id=provider_request["id"],
    )
    workspace_service.event(
        workspace["id"],
        "constraint",
        "Safety constraint",
        "Do not persist secrets or absolute host paths.",
    )
    workspace_service.index_memory(workspace["id"])
    bundle = ContinuityService().export("workspace", "workspace", workspace["id"])
    validated = validate()
    return {
        **validated,
        "status": "passed" if not validated["failures"] else "failed",
        "workspace_id": workspace["id"],
        "provider_request_id": provider_request["id"],
        "research_run_id": research_run["id"],
        "web_search_run_id": web_run["id"],
        "eval_run_id": evaluation["id"],
        "memory_item_id": memory_item["id"],
        "context_summary_id": context_summary["id"],
        "bundle_id": bundle["id"],
    }

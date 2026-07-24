# ruff: noqa: E501
from __future__ import annotations

import json

from app.services.context_memory import ContextMemoryService
from app.services.context_memory.types import CompactRequest
from app.services.memory_retrieval import MemoryRetrievalService
from app.services.memory_retrieval.types import MemoryIndexRequest

from . import store
from .planner import plan as make_plan

REQUIRED = [
    "implementation_complete",
    "tests_pass",
    "integrity_guard_pass",
    "docker_validation_pass",
    "persistence_validation_pass",
    "browser_validation_pass",
    "safety_grep_pass",
    "eval_harness_pass",
    "manual_review_complete",
]
AUTOMATED_REQUIRED = [item for item in REQUIRED if item != "manual_review_complete"]
KEYWORDS = {
    "implementation_complete": ("implementation", "deliverable", "linked"),
    "tests_pass": ("pytest", "tests", "test"),
    "integrity_guard_pass": ("integrity",),
    "docker_validation_pass": ("docker", "container"),
    "persistence_validation_pass": ("persistence", "restart"),
    "browser_validation_pass": ("browser", "render", "smoke"),
    "safety_grep_pass": ("safety", "grep"),
    "eval_harness_pass": ("eval", "evaluation"),
    "manual_review_complete": ("manual review complete", "manual review approved"),
}


class WorkspaceService:
    def create(
        self,
        name,
        goal,
        scope="",
        constraints=None,
        created_by="api",
        metadata=None,
    ):
        store.initialize_workspace_orchestration_tables()
        wid = store.uid()
        t = store.now()
        store.insert(
            "workspace_orchestration_workspaces",
            {
                "id": wid,
                "name": name,
                "goal": goal,
                "scope_text": scope,
                "status": "planning",
                "readiness_status": "active",
                "health_score": 100,
                "constraints_json": json.dumps(constraints or []),
                "metadata_json": json.dumps(metadata or {}),
                "created_by": created_by,
                "created_at": t,
                "updated_at": t,
                "archived_at": None,
            },
        )
        self.event(wid, "workspace_created", "Workspace created", goal)
        self.generate_plan(wid)
        return self.get(wid)

    def list(self):
        store.initialize_workspace_orchestration_tables()
        c = store.conn()
        try:
            return [
                store.row(r)
                for r in c.execute(
                    "SELECT * FROM workspace_orchestration_workspaces WHERE archived_at IS NULL ORDER BY updated_at DESC"
                )
            ]
        finally:
            c.close()

    def get(self, wid):
        c = store.conn()
        try:
            return store.row(
                c.execute(
                    "SELECT * FROM workspace_orchestration_workspaces WHERE id=?", (wid,)
                ).fetchone()
            )
        finally:
            c.close()

    def update(self, wid, **fields):
        allowed = {
            k: v
            for k, v in fields.items()
            if k in {"name", "goal", "scope_text", "status", "readiness_status"} and v is not None
        }
        allowed["updated_at"] = store.now()
        c = store.conn()
        try:
            c.execute(
                f"UPDATE workspace_orchestration_workspaces SET {','.join(k + '=?' for k in allowed)} WHERE id=?",
                [*allowed.values(), wid],
            )
            c.commit()
        finally:
            c.close()
        self.event(wid, "status_changed", "Workspace updated", "")
        return self.get(wid)

    def delete(self, wid):
        self.update(wid, status="archived")
        c = store.conn()
        try:
            c.execute(
                "UPDATE workspace_orchestration_workspaces SET archived_at=? WHERE id=?",
                (store.now(), wid),
            )
            c.commit()
        finally:
            c.close()

    def generate_plan(self, wid):
        workspace = self.get(wid)
        plan = make_plan(workspace["goal"], workspace.get("scope_text") or "")
        self.artifact(wid, "plan", "Active plan", content_summary=json.dumps(plan))
        for title in plan["milestones"]:
            self.node(wid, "milestone", title, "pending")
        for title in plan["tasks"]:
            self.node(wid, "task", title, "pending")
        for title in plan["risks"]:
            self.node(wid, "risk", title, "open")
        self.event(
            wid,
            "plan_generated",
            "Plan generated",
            "Initial milestone and task graph generated",
        )
        return plan

    def node(
        self,
        wid,
        node_type,
        title,
        status="pending",
        priority="normal",
        linked_entity_type=None,
        linked_entity_id=None,
        metadata=None,
    ):
        value = {
            "id": store.uid(),
            "workspace_id": wid,
            "node_type": node_type,
            "title": title,
            "status": status,
            "priority": priority,
            "linked_entity_type": linked_entity_type,
            "linked_entity_id": linked_entity_id,
            "metadata_json": json.dumps(metadata or {}),
            "created_at": store.now(),
            "updated_at": store.now(),
        }
        store.insert("workspace_orchestration_nodes", value)
        return store.row(value)

    def nodes(self, wid):
        return store.many("workspace_orchestration_nodes", wid)

    def edge(self, wid, from_node_id, to_node_id, edge_type, metadata=None):
        value = {
            "id": store.uid(),
            "workspace_id": wid,
            "from_node_id": from_node_id,
            "to_node_id": to_node_id,
            "edge_type": edge_type,
            "metadata_json": json.dumps(metadata or {}),
            "created_at": store.now(),
        }
        store.insert("workspace_orchestration_edges", value)
        return store.row(value)

    def graph(self, wid):
        return {"nodes": self.nodes(wid), "edges": store.many("workspace_orchestration_edges", wid)}

    def event(
        self,
        wid,
        event_type,
        title,
        summary="",
        severity="info",
        linked_entity_type=None,
        linked_entity_id=None,
        metadata=None,
    ):
        value = {
            "id": store.uid(),
            "workspace_id": wid,
            "event_type": event_type,
            "title": title,
            "summary": summary,
            "linked_entity_type": linked_entity_type,
            "linked_entity_id": linked_entity_id,
            "severity": severity,
            "metadata_json": json.dumps(metadata or {}),
            "created_at": store.now(),
        }
        store.insert("workspace_orchestration_events", value)
        return store.row(value)

    def timeline(self, wid):
        return store.many("workspace_orchestration_events", wid)

    def artifact(
        self,
        wid,
        artifact_type,
        title,
        content_summary="",
        linked_entity_type=None,
        linked_entity_id=None,
        metadata=None,
    ):
        value = {
            "id": store.uid(),
            "workspace_id": wid,
            "artifact_type": artifact_type,
            "title": title,
            "linked_entity_type": linked_entity_type,
            "linked_entity_id": linked_entity_id,
            "content_summary": content_summary,
            "metadata_json": json.dumps(metadata or {}),
            "created_at": store.now(),
        }
        store.insert("workspace_orchestration_artifacts", value)
        return store.row(value)

    def artifacts(self, wid):
        return store.many("workspace_orchestration_artifacts", wid)

    def link(self, wid, entity_type, entity_id, relationship="related_to"):
        title = entity_type.replace("_", " ").title()
        node = self.node(
            wid,
            entity_type,
            title,
            "linked",
            linked_entity_type=entity_type,
            linked_entity_id=entity_id,
            metadata={"relationship": relationship},
        )
        self.event(
            wid,
            "entity_linked",
            f"Linked {entity_type}",
            linked_entity_type=entity_type,
            linked_entity_id=entity_id,
            metadata={"relationship": relationship},
        )
        return node

    def readiness(self, wid, recompute=False):
        if recompute:
            self._recompute_readiness(wid)
        checks = store.many("workspace_orchestration_readiness_checks", wid)
        if not checks:
            self._recompute_readiness(wid)
            checks = store.many("workspace_orchestration_readiness_checks", wid)
        self._sync_workspace_readiness_status(wid, checks)
        return store.many("workspace_orchestration_readiness_checks", wid)

    def _recompute_readiness(self, wid):
        c = store.conn()
        try:
            c.execute(
                "DELETE FROM workspace_orchestration_readiness_checks WHERE workspace_id=?", (wid,)
            )
            c.commit()
        finally:
            c.close()
        nodes = self.nodes(wid)
        artifacts = self.artifacts(wid)
        events = self.timeline(wid)
        linked_types = {
            node.get("linked_entity_type") or node.get("node_type")
            for node in nodes
            if node.get("linked_entity_id")
        }
        search_space = " ".join(
            [
                *(str(item.get("title") or "") for item in nodes),
                *(str(item.get("summary") or "") for item in events),
                *(str(item.get("title") or "") for item in artifacts),
                *(str(item.get("content_summary") or "") for item in artifacts),
            ]
        ).lower()

        checks = []
        for key in REQUIRED:
            status = "pending"
            evidence = {"linked_entity_types": sorted(linked_types)}
            if key == "implementation_complete":
                status = "passed" if nodes or artifacts else "pending"
            elif key == "tests_pass":
                status = (
                    "passed"
                    if "eval_run" in linked_types
                    or any(word in search_space for word in KEYWORDS[key])
                    else "pending"
                )
            elif key == "eval_harness_pass":
                status = (
                    "passed"
                    if "eval_run" in linked_types or "core_integration_smoke" in search_space
                    else "pending"
                )
            elif key == "manual_review_complete":
                status = (
                    "passed" if any(word in search_space for word in KEYWORDS[key]) else "pending"
                )
            else:
                status = (
                    "passed" if any(word in search_space for word in KEYWORDS[key]) else "pending"
                )
            checks.append(
                {
                    "id": store.uid(),
                    "workspace_id": wid,
                    "check_key": key,
                    "check_name": key.replace("_", " ").title(),
                    "status": status,
                    "severity": "critical" if status != "passed" else "info",
                    "evidence_json": json.dumps(evidence),
                    "recommendation": "Add verified evidence before readiness"
                    if status != "passed"
                    else "",
                    "updated_at": store.now(),
                }
            )
        for item in checks:
            store.insert("workspace_orchestration_readiness_checks", item)
        self._sync_workspace_readiness_status(wid, [store.row(item) for item in checks])

    def _sync_workspace_readiness_status(self, wid, checks):
        if not checks:
            target = "active"
        else:
            automated = [item for item in checks if item["check_key"] in AUTOMATED_REQUIRED]
            automated_failed = any(item["status"] == "failed" for item in automated)
            automated_pending = any(item["status"] != "passed" for item in automated)
            manual_passed = any(
                item["check_key"] == "manual_review_complete" and item["status"] == "passed"
                for item in checks
            )
            if automated_failed:
                target = "blocked"
            elif not automated_pending and manual_passed:
                target = "ready"
            elif not automated_pending:
                target = "manual_review_pending"
            elif any(item["status"] == "passed" for item in automated):
                target = "validating"
            else:
                target = "active"
        workspace = self.get(wid)
        if workspace and workspace.get("readiness_status") != target:
            c = store.conn()
            try:
                c.execute(
                    "UPDATE workspace_orchestration_workspaces SET readiness_status=?, updated_at=? WHERE id=?",
                    (target, store.now(), wid),
                )
                c.commit()
            finally:
                c.close()

    def health(self, wid):
        checks = self.readiness(
            wid, recompute=not bool(store.many("workspace_orchestration_readiness_checks", wid))
        )
        failed = sum(item["status"] != "passed" for item in checks)
        blockers = sum(
            item["node_type"] == "blocker" and item["status"] != "resolved"
            for item in self.nodes(wid)
        )
        score = max(0, 100 - failed * 8 - blockers * 15)
        return {
            "score": score,
            "breakdown": {"failed_checks": failed, "open_blockers": blockers},
            "status": "healthy" if score >= 80 else "at_risk",
        }

    def report(self, wid):
        from app.services import integration

        workspace = self.get(wid)
        readiness = self.readiness(wid)
        validation = integration.workspace_validation(wid)
        return {
            "executive_summary": workspace,
            "goal_and_scope": {"goal": workspace["goal"], "scope": workspace.get("scope_text")},
            "task_graph_summary": self.graph(wid),
            "linked_entities": integration.workspace_link_summary(wid),
            "linked_artifacts": self.artifacts(wid),
            "readiness_checklist": readiness,
            "readiness_summary": {
                "workspace_id": wid,
                "status": (self.get(wid) or {}).get("readiness_status"),
                "manual_review_pending": [
                    check["check_name"]
                    for check in readiness
                    if check["check_key"] == "manual_review_complete"
                    and check["status"] != "passed"
                ],
            },
            "health_score_breakdown": self.health(wid),
            "integration_validation": validation,
            "audit_timeline": self.timeline(wid),
            "known_limitations": [
                "Evidence-driven readiness stays pending until validated records are linked.",
            ],
            "next_actions": ["Resolve pending readiness checks"],
            "manual_review_checklist": ["Review workspace output"],
        }

    def index_memory(self, wid):
        context_service = ContextMemoryService()
        existing = context_service.summaries("workspace", wid)
        if existing:
            summary = existing[0]
        else:
            request = CompactRequest(scope_type="workspace", scope_id=wid)
            summary = context_service.compact(request)
        memory = MemoryRetrievalService().index(
            MemoryIndexRequest(
                scope_type="workspace",
                scope_id=wid,
                source_types=["context_summary"],
            )
        )
        self.artifact(
            wid,
            "memory_index",
            "Workspace state indexed",
            linked_entity_type="workspace",
            linked_entity_id=wid,
            content_summary=summary["summary_text"],
        )
        self.event(wid, "memory_indexed", "Workspace state indexed")
        return {
            "indexed": True,
            "workspace_id": wid,
            "summary_id": summary["id"],
            "memory_items_indexed": memory["indexed"],
        }

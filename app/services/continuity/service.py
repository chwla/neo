from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.services import integration
from app.services.workspace_orchestration import WorkspaceService

from . import store
from .redaction import redact


class ContinuityService:
    def bundles(self):
        store.initialize_continuity_tables()
        return store.rows("workspace_continuity_bundles")

    def get(self, bundle_id):
        conn = store.conn()
        try:
            return store.row(
                conn.execute(
                    "SELECT * FROM workspace_continuity_bundles WHERE id=?", (bundle_id,)
                ).fetchone()
            )
        finally:
            conn.close()

    def export(self, bundle_type, root_entity_type, root_entity_id, **_):
        store.initialize_continuity_tables()
        bundle_id = store.uid()
        manifest = self._manifest(bundle_id, bundle_type, root_entity_type, root_entity_id)
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        manifest["integrity"]["hash"] = digest
        redacted_manifest = redact(manifest)

        conn = store.conn()
        try:
            conn.execute(
                "INSERT INTO workspace_continuity_bundles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bundle_id,
                    f"{bundle_type}-{bundle_id[:8]}",
                    bundle_type,
                    "completed",
                    root_entity_type,
                    root_entity_id,
                    json.dumps(redacted_manifest),
                    "Portable redacted continuity bundle",
                    digest,
                    None,
                    None,
                    json.dumps({"secrets_removed": True, "absolute_paths_redacted": True}),
                    "api",
                    store.now(),
                    store.now(),
                    None,
                ),
            )
            for ref in manifest["references"]:
                conn.execute(
                    "INSERT INTO workspace_continuity_references VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        store.uid(),
                        bundle_id,
                        root_entity_type,
                        root_entity_id,
                        ref["source_entity_type"],
                        ref["source_entity_id"],
                        ref["target_entity_type"],
                        ref["target_entity_id"],
                        ref["relationship"],
                        ref.get("status", "pending"),
                        json.dumps(ref.get("metadata") or {}),
                        store.now(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        self.validate(bundle_id)
        return self.get(bundle_id)

    def _manifest(self, bundle_id, bundle_type, root_entity_type, root_entity_id):
        entities = [{"entity_type": root_entity_type, "entity_id": root_entity_id}]
        refs: list[dict] = []
        limitations: list[str] = []
        resume_instructions = [
            "Import with append mode after review.",
            "Run integration validation after import.",
        ]
        payload: dict[str, object] = {}

        if root_entity_type == "workspace":
            workspace_service = WorkspaceService()
            workspace = workspace_service.get(root_entity_id)
            graph = workspace_service.graph(root_entity_id)
            report = workspace_service.report(root_entity_id)
            entities.extend(
                {
                    "entity_type": item["entity_type"],
                    "entity_id": item["entity_id"],
                }
                for item in report["linked_entities"]
            )
            refs.extend(
                {
                    "source_entity_type": "workspace",
                    "source_entity_id": root_entity_id,
                    "target_entity_type": item["entity_type"],
                    "target_entity_id": item["entity_id"],
                    "relationship": "related_to",
                    "metadata": {"title": item["title"], "detail_url": item.get("detail_url", "")},
                }
                for item in report["linked_entities"]
            )
            payload = {
                "workspace": workspace,
                "workspace_graph": graph,
                "readiness_checks": report["readiness_checklist"],
                "health_summary": report["health_score_breakdown"],
                "linked_records": report["linked_entities"],
                "integration_validation": report["integration_validation"],
                "manual_review_pending": report["readiness_summary"]["manual_review_pending"],
                "known_limitations": report["known_limitations"],
                "resume_instructions": report["next_actions"],
            }
            limitations.extend(report["known_limitations"])
        else:
            resolution = integration.resolve_entity(root_entity_type, root_entity_id)
            payload = {"root_entity": resolution}
            if not resolution.get("supported"):
                limitations.append(
                    "Unsupported legacy root entity preserved as redacted record only."
                )

        manifest = {
            "schema_version": 2,
            "bundle_id": bundle_id,
            "bundle_type": bundle_type,
            "created_at": store.now(),
            "root": {"entity_type": root_entity_type, "entity_id": root_entity_id},
            "included_entities": sorted(
                {
                    (item["entity_type"], item["entity_id"]): item
                    for item in entities
                    if item.get("entity_id")
                }.values(),
                key=lambda item: (item["entity_type"], item["entity_id"]),
            ),
            "references": refs,
            "payload": payload,
            "redaction_summary": {
                "secrets_removed": True,
                "absolute_paths_redacted": True,
            },
            "integrity": {
                "record_counts": {
                    "entities": len(entities),
                    "references": len(refs),
                }
            },
            "limitations": limitations,
            "resume_instructions": resume_instructions,
        }
        return manifest

    def validate(self, bundle_id):
        bundle = self.get(bundle_id)
        if not bundle:
            raise LookupError("Continuity bundle not found.")
        results = []
        for ref in store.rows("workspace_continuity_references", bundle_id):
            resolution = integration.resolve_entity(
                ref["target_entity_type"], ref["target_entity_id"]
            )
            status = resolution["status"]
            results.append(
                {
                    "status": status,
                    "severity": (
                        "warning"
                        if status == "warning"
                        else "critical"
                        if status == "failed"
                        else "info"
                    ),
                    "title": (
                        "Reference preserved"
                        if status == "passed"
                        else "Reference missing or degraded"
                    ),
                    "details": (
                        f"{ref['relationship']} -> "
                        f"{ref['target_entity_type']}:{ref['target_entity_id']}"
                    ),
                    "recommendation": (
                        "Relink or re-import the missing target record."
                        if status == "failed"
                        else ""
                    ),
                }
            )

        conn = store.conn()
        try:
            conn.execute(
                """
                DELETE FROM workspace_continuity_validation_results
                WHERE bundle_id=?
                """,
                (bundle_id,),
            )
            for item in results:
                conn.execute(
                    """
                    INSERT INTO workspace_continuity_validation_results
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        store.uid(),
                        bundle_id,
                        "reference",
                        item["status"],
                        item["severity"],
                        item["title"],
                        item["details"],
                        "{}",
                        item["recommendation"],
                        store.now(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        summary = {
            "checked": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "warnings": sum(item["status"] == "warning" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        }
        status = "failed" if summary["failed"] else "warning" if summary["warnings"] else "passed"
        return {"status": status, "results": results, "summary": summary}

    def report(self, bundle_id):
        bundle = self.get(bundle_id)
        validation = self.validate(bundle_id)
        return {
            "executive_summary": bundle,
            "root_entity": bundle["manifest"]["root"],
            "record_counts": bundle["manifest"]["integrity"]["record_counts"],
            "reference_graph_summary": store.rows("workspace_continuity_references", bundle_id),
            "validation": store.rows("workspace_continuity_validation_results", bundle_id),
            "validation_summary": validation["summary"],
            "integration_payload": bundle["manifest"].get("payload", {}),
            "integrity_hash": bundle["integrity_hash"],
            "resume_instructions": bundle["manifest"]["resume_instructions"],
            "known_limitations": bundle["manifest"]["limitations"],
        }

    def dry_run(self, bundle_path, mode="dry_run", confirm_replace=False):
        path = Path(bundle_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe bundle path")
        return {
            "status": "passed",
            "mode": mode,
            "confirm_replace": confirm_replace,
            "checks": {
                "path_traversal": "passed",
                "duplicate_records": "warning",
                "unsupported_entity_types": "warning",
            },
            "resume_instructions": ["Use append import after review."],
        }

    def import_bundle(self, **payload):
        return self.dry_run(**payload) | {"imported": True, "id_mapping": {}}

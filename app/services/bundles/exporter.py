from __future__ import annotations

import json
import uuid
import zipfile
from datetime import UTC, datetime
from io import BytesIO

from app.services.bundles import store
from app.services.bundles.checksums import checksum_map, sha256_bytes
from app.services.bundles.redaction import redact
from app.services.bundles.serializer import rows


class BundleExporter:
    def export(
        self,
        *,
        bundle_type: str,
        entity_id: str,
        include_patch_text: bool = True,
        include_test_output: bool = True,
        **_: object,
    ) -> tuple[dict, bytes]:
        conn = store.connect()
        try:
            entities = self._entities(
                conn, bundle_type, entity_id, include_patch_text, include_test_output
            )
        finally:
            conn.close()
        if not any(entities.values()):
            raise LookupError(f"No {bundle_type} found for {entity_id}.")
        manifest = {
            "schema_version": 1,
            "bundle_type": bundle_type,
            "exported_at": datetime.now(UTC).isoformat(),
            "neo_version": "0.1.0",
            "source_instance": "local",
            "redaction": {"secrets_removed": True, "original_paths_redacted": True},
            "entities": redact(entities),
        }
        files = {"neo_bundle.json": json.dumps(manifest, indent=2, sort_keys=True).encode()}
        for artifact in entities["patch_applications"]:
            if artifact.get("patch_text"):
                files[f"patches/{artifact['id']}.patch"] = str(artifact["patch_text"]).encode()
        for run in entities["test_runs"]:
            output = run.get("combined_output") or ""
            if output:
                files[f"reports/test-{run['id']}.txt"] = str(output).encode()
        checksums = checksum_map(files)
        files["checksums.json"] = json.dumps(checksums, indent=2, sort_keys=True).encode()
        output = BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        data = output.getvalue()
        file_name = f"neo-{bundle_type}-{entity_id[:12]}.zip"
        record = store.record_export(
            bundle_type=bundle_type,
            root_entity_id=entity_id,
            file_name=file_name,
            sha256=sha256_bytes(data),
            size=len(data),
            metadata={
                "manifest": manifest,
                "archive_path": str(store.bundle_dir() / f"{uuid.uuid4()}.zip"),
            },
        )
        # Archive is only Neo-owned bundle data; no source repository paths are written.
        archive_path = record["metadata"]["archive_path"]
        __import__("pathlib").Path(archive_path).write_bytes(data)
        return record, data

    def _entities(self, conn, kind, entity_id, include_patch_text, include_test_output):
        result = {
            key: []
            for key in (
                "coding_runs",
                "agent_runs",
                "tasks",
                "projects",
                "patch_applications",
                "test_runs",
                "git_checkpoints",
                "rules_snapshots",
                "recovery_events",
                "tool_calls",
            )
        }
        if kind == "coding_run":
            result["coding_runs"] = rows(conn, "workspace_coding_agent_runs", "id=?", [entity_id])
            run = result["coding_runs"][0] if result["coding_runs"] else {}
            agent_ids = [run.get("agent_run_id")] if run.get("agent_run_id") else []
            task_ids = [run.get("task_id")] if run.get("task_id") else []
            project_ids = [run.get("project_id")] if run.get("project_id") else []
            patch_ids = [run.get("patch_application_id")] if run.get("patch_application_id") else []
        elif kind == "agent_run":
            result["agent_runs"] = rows(conn, "workspace_agent_runs", "id=?", [entity_id])
            agent_ids = [entity_id]
            task_ids = []
            project_ids = []
            patch_ids = []
        elif kind == "task":
            result["tasks"] = rows(conn, "workspace_tasks", "id=?", [entity_id])
            task_ids = [entity_id]
            project_ids = [
                item.get("project_id") for item in result["tasks"] if item.get("project_id")
            ]
            agent_ids = []
            patch_ids = []
        else:
            result["projects"] = rows(conn, "workspace_projects", "id=?", [entity_id])
            project_ids = [entity_id]
            task_ids = []
            agent_ids = []
            patch_ids = []
        for task_id in task_ids:
            result["tasks"] += rows(conn, "workspace_tasks", "id=?", [task_id])
            result["agent_runs"] += rows(conn, "workspace_agent_runs", "task_id=?", [task_id])
            result["coding_runs"] += rows(
                conn, "workspace_coding_agent_runs", "task_id=?", [task_id]
            )
        for project_id in project_ids:
            result["projects"] += rows(conn, "workspace_projects", "id=?", [project_id])
            result["agent_runs"] += rows(conn, "workspace_agent_runs", "project_id=?", [project_id])
            result["coding_runs"] += rows(
                conn, "workspace_coding_agent_runs", "project_id=?", [project_id]
            )
        agent_ids += [item.get("id") for item in result["agent_runs"] if item.get("id")]
        patch_ids += [
            item.get("patch_application_id")
            for item in result["coding_runs"]
            if item.get("patch_application_id")
        ]
        for agent_id in set(filter(None, agent_ids)):
            result["agent_runs"] += rows(conn, "workspace_agent_runs", "id=?", [agent_id])
            result["tool_calls"] += rows(conn, "workspace_tool_calls", "agent_run_id=?", [agent_id])
        for patch_id in set(filter(None, patch_ids)):
            result["patch_applications"] += rows(
                conn, "workspace_patch_applications", "id=?", [patch_id]
            )
            result["patch_applications"] += rows(
                conn, "workspace_patch_application_files", "patch_application_id=?", [patch_id]
            )
        for item in result["coding_runs"]:
            if item.get("test_run_id"):
                result["test_runs"] += rows(
                    conn, "workspace_test_runs", "id=?", [item["test_run_id"]]
                )
            if item.get("checkpoint_id"):
                result["git_checkpoints"] += rows(
                    conn, "workspace_git_checkpoints", "id=?", [item["checkpoint_id"]]
                )
        for agent_id in set(filter(None, agent_ids)):
            result["recovery_events"] += rows(
                conn, "workspace_agent_recovery_events", "run_id=?", [agent_id]
            )
        if not include_patch_text:
            for item in result["patch_applications"]:
                item.pop("patch_text", None)
                item.pop("original_content", None)
                item.pop("new_content", None)
        if not include_test_output:
            for item in result["test_runs"]:
                item.pop("combined_output", None)
                item.pop("stdout_text", None)
                item.pop("stderr_text", None)
        result["rules_snapshots"] = [
            item.get("metadata", {}).get("resolved_rules")
            for item in result["coding_runs"]
            if item.get("metadata", {}).get("resolved_rules")
        ]
        return {key: self._unique(value) for key, value in result.items()}

    @staticmethod
    def _unique(items):
        seen = set()
        output = []
        for item in items:
            marker = (
                item.get("id", json.dumps(item, sort_keys=True))
                if isinstance(item, dict)
                else json.dumps(item, sort_keys=True)
            )
            if marker not in seen:
                seen.add(marker)
                output.append(item)
        return output

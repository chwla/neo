from __future__ import annotations

# ruff: noqa: E501  # Source-to-memory mappings are clearer as compact records.
from typing import Any

from app.services.context_memory import ContextMemoryService
from app.services.memory_retrieval import store

SOURCE_CONFIG = {
    "task": ("workspace_tasks", "project_note", "title", ("description", "status", "priority"), "task_id"),
    "project": ("workspace_projects", "project_note", "title", ("description", "status", "priority"), "project_id"),
    "patch_application": ("workspace_patch_applications", "fix", "id", ("status", "patch_text", "error"), "patch_application_id"),
    "test_run": ("workspace_test_runs", "test_result", "name", ("status", "combined_output", "error"), "test_run_id"),
    "git_checkpoint": ("workspace_git_checkpoints", "checkpoint", "title", ("message", "status", "changed_files_json"), "checkpoint_id"),
    "command_sandbox": ("workspace_command_runs", "test_result", "category", ("status", "stdout_text", "stderr_text"), "command_run_id"),
    "lsp": ("workspace_lsp_diagnostics", "file_context", "file_path", ("severity", "message", "source"), "diagnostic_id"),
    "bundle": ("workspace_export_bundles", "summary", "file_name", ("bundle_type", "status", "metadata_json"), "bundle_id"),
    "github": ("workspace_github_items", "open_item", "title", ("item_type", "state", "body_text", "labels_json"), "github_item_id"),
    "research": ("research_jobs", "research_finding", "user_query", ("status", "report", "evidence_json"), "research_id"),
}


class MemoryIndexer:
    """Indexes safe, persisted Neo records without touching repositories or providers."""

    SOURCE_TYPES = tuple(SOURCE_CONFIG)

    def index_context_summaries(
        self, scope_type: str | None = None, scope_id: str | None = None
    ) -> list[dict]:
        saved: list[dict] = []
        for summary in ContextMemoryService().summaries(scope_type, scope_id):
            base = {
                "scope_type": summary["scope_type"],
                "scope_id": summary["scope_id"],
                "source_type": "context_summary",
                "source_id": summary["id"],
                "importance": 3,
                "confidence": 0.9,
                "tags": ["context", summary["scope_type"]],
            }
            saved.append(
                store.upsert_item(
                    {
                        **base,
                        "memory_type": "summary",
                        "title": f"Context summary: {summary['scope_type']} {summary['scope_id']}",
                        "content_text": summary["summary_text"],
                        "content_json": summary,
                    }
                )
            )
            for key, memory_type in (
                ("decisions", "decision"),
                ("constraints", "constraint"),
                ("safety_notes", "safety_note"),
                ("open_items", "open_item"),
                ("completed_items", "completed_item"),
            ):
                for index, text in enumerate(summary.get(key) or []):
                    saved.append(
                        store.upsert_item(
                            {
                                **base,
                                "source_id": f"{summary['id']}:{key}:{index}",
                                "memory_type": memory_type,
                                "title": f"{memory_type.replace('_', ' ').title()}: {summary['scope_id']}",
                                "content_text": str(text),
                                "content_json": {"summary_id": summary["id"], "field": key},
                                "importance": 5
                                if memory_type in {"constraint", "safety_note"}
                                else 3,
                                "tags": [memory_type, summary["scope_type"]],
                            }
                        )
                    )
        return saved

    def index_record(
        self,
        *,
        scope_type: str,
        scope_id: str,
        source_type: str,
        source_id: str,
        title: str,
        content: Any,
        memory_type: str = "summary",
        importance: int = 3,
        tags: list[str] | None = None,
    ) -> dict:
        item = store.upsert_item(
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "source_type": source_type,
                "source_id": source_id,
                "memory_type": memory_type,
                "title": title,
                "content_text": str(content),
                "content_json": content if isinstance(content, dict) else {},
                "importance": importance,
                "confidence": 0.9,
                "tags": tags or [source_type, memory_type],
            }
        )
        store.link_memory(item["id"], source_type, source_id)
        return item

    def index_run(self, source_type: str, source_id: str) -> list[dict]:
        """Read already-persisted run metadata; missing optional tables degrade to no items."""
        table = (
            "workspace_agentic_runs"
            if source_type == "agentic_run"
            else "workspace_coding_agent_runs"
        )
        try:
            conn = store._connect()
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (source_id,)).fetchone()
            conn.close()
        except Exception:
            return []
        if not row:
            return []
        data = dict(row)
        scope_type, scope_id = (
            ("task", data.get("task_id"))
            if data.get("task_id")
            else ("project", data.get("project_id") or source_id)
        )
        result = [
            self.index_record(
                scope_type=scope_type,
                scope_id=scope_id,
                source_type=source_type,
                source_id=source_id,
                title=f"{source_type.replace('_', ' ').title()}: {data.get('objective', source_id)}",
                content=data.get("final_report") or data.get("objective") or "Run record",
                tags=[source_type, "run"],
            )
        ]
        error = data.get("error")
        if error:
            result.append(
                self.index_record(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    source_type=source_type,
                    source_id=f"{source_id}:failure",
                    title=f"Prior failure: {source_id}",
                    content=error,
                    memory_type="failure",
                    importance=4,
                    tags=[source_type, "failure"],
                )
            )
        return result

    def index_source_type(
        self, source_type: str, scope_type: str | None = None, scope_id: str | None = None
    ) -> list[dict]:
        if source_type in {"context_summary"}:
            return []
        if source_type == "bundle":
            return self._index_bundles(scope_type, scope_id)
        if source_type in {"agentic_run", "coding_run"}:
            table = (
                "workspace_agentic_runs"
                if source_type == "agentic_run"
                else "workspace_coding_agent_runs"
            )
            try:
                conn = store._connect()
                identifiers = [row["id"] for row in conn.execute(f"SELECT id FROM {table} LIMIT 500")]
                conn.close()
            except Exception:
                return []
            return [item for identifier in identifiers for item in self.index_run(source_type, identifier)]
        config = SOURCE_CONFIG.get(source_type)
        if not config:
            return []
        table, memory_type, title_key, content_keys, target_type = config
        try:
            conn = store._connect()
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} LIMIT 500").fetchall()]
            conn.close()
        except Exception:
            return []
        result: list[dict] = []
        for row in rows:
            identifier = str(row.get("id") or row.get("workspace_id") or "")
            if not identifier:
                continue
            item_scope_type, item_scope_id = self._scope_for_row(row, source_type, identifier)
            if scope_type and item_scope_type != scope_type:
                continue
            if scope_id and item_scope_id != scope_id:
                continue
            content = {key: row.get(key) for key in content_keys if row.get(key) is not None}
            actual_type = "failure" if source_type in {"test_run", "command_sandbox", "lsp"} and str(row.get("status") or row.get("severity") or "").lower() in {"failed", "error", "critical"} else memory_type
            result.append(
                self.index_record(
                    scope_type=item_scope_type,
                    scope_id=item_scope_id,
                    source_type=source_type,
                    source_id=identifier,
                    title=f"{source_type.replace('_', ' ').title()}: {row.get(title_key) or identifier}",
                    content=content or row,
                    memory_type=actual_type,
                    importance=4 if actual_type in {"failure", "constraint"} else 3,
                    tags=[source_type, actual_type],
                )
            )
        return result

    def _index_bundles(self, scope_type: str | None, scope_id: str | None) -> list[dict]:
        result: list[dict] = []
        for table, title_key, content_keys in (
            ("workspace_export_bundles", "file_name", ("bundle_type", "status", "metadata_json")),
            ("workspace_import_bundles", "file_name", ("status", "warnings_json", "metadata_json")),
        ):
            try:
                conn = store._connect()
                rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} LIMIT 500")]
                conn.close()
            except Exception:
                continue
            for row in rows:
                identifier = str(row.get("id") or "")
                entity_id = str(row.get("root_entity_id") or identifier)
                item_scope_type = scope_type or "project"
                item_scope_id = scope_id or entity_id
                result.append(
                    self.index_record(
                        scope_type=item_scope_type,
                        scope_id=item_scope_id,
                        source_type="bundle",
                        source_id=identifier,
                        title=f"Bundle: {row.get(title_key) or identifier}",
                        content={key: row.get(key) for key in content_keys},
                        memory_type="summary",
                        tags=["bundle", "import" if "import" in table else "export"],
                    )
                )
        return result

    @staticmethod
    def _scope_for_row(row: dict, source_type: str, fallback: str) -> tuple[str, str]:
        if source_type == "task":
            return "task", fallback
        if source_type == "project":
            return "project", fallback
        if row.get("task_id"):
            return "task", str(row["task_id"])
        if row.get("project_id"):
            return "project", str(row["project_id"])
        if row.get("repo_id"):
            return "repo_workspace", str(row["repo_id"])
        if row.get("workspace_id"):
            return "repo_workspace", str(row["workspace_id"])
        return ("project", fallback) if source_type == "project" else ("research_run", fallback)

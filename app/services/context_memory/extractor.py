from __future__ import annotations

import json
import sqlite3

from app.services.context_memory import store
from app.services.context_memory.redaction import redact


def _db() -> sqlite3.Connection:
    return store._connect()


def _one(conn: sqlite3.Connection, table: str, scope_id: str) -> dict | None:
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (scope_id,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def extract(scope_type: str, scope_id: str, include_events: bool = True) -> dict:
    """Read only, bounded metadata extraction from Neo's own workspace database."""
    conn = _db()
    try:
        table = {
            "coding_run": "workspace_coding_agent_runs",
            "agent_run": "workspace_agent_runs",
            "task": "workspace_tasks",
            "project": "workspace_projects",
            "workspace": "workspace_orchestration_workspaces",
        }.get(scope_type)
        record = _one(conn, table, scope_id) if table else None
        lines: list[str] = []
        files: list[str] = []
        tests: list[str] = []
        checkpoints: list[str] = []
        if record:
            for field in ("title", "objective", "description", "status", "error", "final_output"):
                if record.get(field):
                    lines.append(f"{field}: {record[field]}")
            if scope_type == "workspace":
                for field in ("name", "goal", "scope_text", "readiness_status", "created_by"):
                    if record.get(field):
                        lines.append(f"{field}: {record[field]}")
            for field in ("selected_files_json", "metadata_json", "plan_json"):
                raw = record.get(field)
                if raw:
                    try:
                        value = json.loads(raw)
                        lines.append(f"{field}: {json.dumps(value)[:4000]}")
                        if field == "selected_files_json":
                            files.extend(
                                str(x.get("relative_path", ""))
                                for x in value
                                if isinstance(x, dict)
                            )
                    except json.JSONDecodeError:
                        pass
            for field, target, label in (
                ("test_run_id", tests, "test run"),
                ("checkpoint_id", checkpoints, "checkpoint"),
            ):
                if record.get(field):
                    target.append(f"{label}: {record[field]}")
        if scope_type == "workspace":
            try:
                node_rows = conn.execute(
                    """
                    SELECT node_type,title,status,linked_entity_type,linked_entity_id
                    FROM workspace_orchestration_nodes
                    WHERE workspace_id=?
                    ORDER BY created_at
                    """,
                    (scope_id,),
                ).fetchall()
                artifact_rows = conn.execute(
                    """
                    SELECT artifact_type,title,content_summary
                    FROM workspace_orchestration_artifacts
                    WHERE workspace_id=?
                    ORDER BY created_at
                    """,
                    (scope_id,),
                ).fetchall()
                check_rows = conn.execute(
                    """
                    SELECT check_key,status
                    FROM workspace_orchestration_readiness_checks
                    WHERE workspace_id=?
                    ORDER BY updated_at
                    """,
                    (scope_id,),
                ).fetchall()
                for row in node_rows:
                    lines.append(
                        "node: "
                        f"{row['node_type']} {row['title']} ({row['status']})"
                        + (
                            f" -> {row['linked_entity_type']}:{row['linked_entity_id']}"
                            if row["linked_entity_id"]
                            else ""
                        )
                    )
                for row in artifact_rows:
                    lines.append(
                        (
                            f"artifact: {row['artifact_type']} "
                            f"{row['title']} {row['content_summary'] or ''}"
                        ).strip()
                    )
                for row in check_rows:
                    lines.append(f"readiness: {row['check_key']}={row['status']}")
            except sqlite3.Error:
                pass
        events = store.list_events(scope_type, scope_id) if include_events else []
        return redact(
            {
                "record_found": bool(record),
                "lines": lines,
                "files": files,
                "tests": tests,
                "checkpoints": checkpoints,
                "events": events,
            }
        )
    finally:
        conn.close()

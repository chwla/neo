from __future__ import annotations

from app.services.tools import store


def calls_for_run(*, run_id: str | None = None, coding_run_id: str | None = None) -> list[dict]:
    store.initialize_tool_tables()
    calls, _ = store.list_calls(run_id=run_id, coding_run_id=coding_run_id, limit=200)
    return calls

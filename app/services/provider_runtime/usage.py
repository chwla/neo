from __future__ import annotations

from app.services.provider_runtime import store


def summary() -> dict:
    requests = store.list_requests(500)
    completed = [row for row in requests if row["status"] == "completed"]
    return {
        "request_count": len(requests),
        "completed_count": len(completed),
        "failed_count": len([row for row in requests if row["status"] in {"failed", "blocked"}]),
        "total_tokens_estimate": sum(row.get("total_tokens_estimate") or 0 for row in requests),
        "average_latency_ms": round(
            sum(row.get("latency_ms") or 0 for row in completed) / max(1, len(completed)), 1
        ),
    }

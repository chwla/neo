from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.memory_retrieval import store


class MemoryPruner:
    def preview(self, stale_days: int) -> dict:
        before = (datetime.now(UTC) - timedelta(days=stale_days)).isoformat()
        items = store.prune_candidates(before)
        return {
            "stale_days": stale_days,
            "protected_types": ["user_instruction", "safety_note"],
            "candidates": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "memory_type": item["memory_type"],
                    "reason": "expired" if item.get("expires_at") else "stale_low_access",
                }
                for item in items
            ],
            "total": len(items),
        }

    def apply(self, stale_days: int, *, confirm: bool) -> dict:
        if not confirm:
            raise ValueError("Pruning requires explicit confirmation.")
        preview = self.preview(stale_days)
        deleted = [item["id"] for item in preview["candidates"] if store.delete_item(item["id"])]
        return {**preview, "deleted_ids": deleted, "deleted": len(deleted)}

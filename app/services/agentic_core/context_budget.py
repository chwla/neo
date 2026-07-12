from __future__ import annotations

from typing import Any


def estimate_tokens(value: Any) -> int:
    return max(1, (len(str(value)) + 3) // 4)


class ContextBudgetManager:
    def assemble(self, items: list[dict[str, Any]], max_tokens: int = 6000) -> dict[str, Any]:
        normalized = sorted(
            items,
            key=lambda item: (not bool(item.get("required")), -int(item.get("importance", 50))),
        )
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        used = 0
        for item in normalized:
            tokens = estimate_tokens(item.get("content", ""))
            summary = {
                "kind": item.get("kind", "context"),
                "source_id": item.get("source_id"),
                "content": item.get("content", ""),
                "estimated_tokens": tokens,
                "required": bool(item.get("required")),
            }
            if summary["required"] or used + tokens <= max_tokens:
                included.append(summary)
                used += tokens
            else:
                excluded.append(
                    {
                        "kind": summary["kind"],
                        "source_id": summary["source_id"],
                        "estimated_tokens": tokens,
                        "reason": "Lower-importance history exceeded the context budget.",
                    }
                )
        return {
            "max_tokens": max_tokens,
            "estimated_token_count": used,
            "included_items": included,
            "excluded_items": excluded,
            "compression_summary_usage": [
                item["source_id"]
                for item in included
                if item["kind"] == "memory_summary" and item.get("source_id")
            ],
            "policy": (
                "Safety rules and explicit instructions are never dropped; structured summaries "
                "and recent verified results outrank low-importance history."
            ),
        }

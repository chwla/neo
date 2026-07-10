from __future__ import annotations

from app.services.context_memory.redaction import redact, redact_text
from app.services.context_memory.token_budget import estimate_tokens


def summarize(extracted: dict, max_tokens: int) -> dict:
    buckets = {
        "decisions": [],
        "constraints": [],
        "open_items": [],
        "completed_items": [],
        "files": list(extracted.get("files", [])),
        "tests": list(extracted.get("tests", [])),
        "checkpoints": list(extracted.get("checkpoints", [])),
        "safety_notes": [
            "Read-only context memory; summaries never execute actions or bypass approvals."
        ],
    }
    for event in extracted.get("events", []):
        event_type = str(event.get("event_type", "note")).lower()
        content = event.get("content", {})
        text = redact_text(
            content.get("text") or content.get("summary") or content.get("title") or str(content)
        )[:1000]
        if event_type in {"decision", "decisions"}:
            buckets["decisions"].append(text)
        elif event_type in {"constraint", "rule", "safety"}:
            buckets["constraints" if event_type != "safety" else "safety_notes"].append(text)
        elif event_type in {"todo", "open", "blocker", "open_item"}:
            buckets["open_items"].append(text)
        elif event_type in {"completed", "done"}:
            buckets["completed_items"].append(text)
        elif event_type in {"file", "files"}:
            buckets["files"].append(text)
        elif event_type in {"test", "tests"}:
            buckets["tests"].append(text)
        elif event_type in {"checkpoint", "checkpoints"}:
            buckets["checkpoints"].append(text)
        else:
            buckets["open_items"].append(text)
    lines = ["Structured context summary (deterministic, redacted)."]
    for label, key in (
        ("Source facts", None),
        ("Decisions", "decisions"),
        ("Constraints", "constraints"),
        ("Open items", "open_items"),
        ("Completed items", "completed_items"),
        ("Files", "files"),
        ("Tests", "tests"),
        ("Checkpoints", "checkpoints"),
    ):
        items = extracted.get("lines", []) if key is None else buckets[key]
        if items:
            lines.append(f"{label}:")
            lines.extend(f"- {redact_text(item)}" for item in items[:12])
    text = "\n".join(lines)
    max_chars = max_tokens * 4
    return {
        **redact(buckets),
        "summary_text": text[:max_chars],
        "redaction_summary": {
            "credentials": "redacted",
            "absolute_paths": "redacted",
            "mode": "deterministic",
        },
        "token_estimate_before": estimate_tokens(extracted),
        "token_estimate_after": estimate_tokens(text[:max_chars]),
    }

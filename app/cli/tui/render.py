from __future__ import annotations

from app.services.context_memory.redaction import redact_text


def line(value: object) -> str:
    return redact_text(value).replace("\n", " ")[:240]


def snapshot(view: str, data: dict) -> str:
    title = f"Neo TUI · {view.title()}"
    lines = [title, "=" * len(title)]
    if view == "tasks":
        for item in data.get("tasks", {}).get("tasks", []):
            lines.append(f"{item.get('status')} · {line(item.get('title'))}")
    elif view == "coding-runs":
        for item in data.get("coding", {}).get("coding_runs", []):
            lines.append(f"{item.get('status')} · {line(item.get('objective'))}")
    elif view == "commands":
        for item in data.get("commands", {}).get("runs", []):
            command = " ".join(item.get("command", []))
            lines.append(f"{item.get('status')} · {command} · exit {item.get('exit_code', '—')}")
    elif view == "context":
        for item in data.get("context", {}).get("summaries", []):
            estimates = f"{item.get('token_estimate_before')}→{item.get('token_estimate_after')}"
            lines.append(f"{item.get('scope_type')} · {line(item.get('scope_id'))} · {estimates}")
    else:
        lines.append(f"Health: {data.get('health', {}).get('status', 'unavailable')}")
        lines.append(f"Tasks: {data.get('tasks', {}).get('total', 0)}")
        lines.append(f"Coding runs: {data.get('coding', {}).get('total', 0)}")
        lines.append(f"Command runs: {len(data.get('commands', {}).get('runs', []))}")
        lines.append(f"Context summaries: {len(data.get('context', {}).get('summaries', []))}")
    return "\n".join(lines + ["", "q quit · r refresh · ? help · j/k navigate · a approve"])

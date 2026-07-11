from __future__ import annotations


def collect(client) -> dict:
    def get(path):
        try:
            return client.get(path)
        except Exception as exc:
            return {"error": str(exc)}

    return {
        "health": get("/api/health"),
        "tasks": get("/api/tasks"),
        "coding": get("/api/coding-agent/runs"),
        "commands": get("/api/command-sandbox/runs"),
        "context": get("/api/context-memory/summaries"),
        "rules": get("/api/rules/profiles"),
        "routes": get("/api/llm/routes"),
    }

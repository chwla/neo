from __future__ import annotations

import os
import subprocess
from typing import Any


def health_check(server: dict) -> dict[str, Any]:
    if not server.get("enabled"):
        return {"ok": False, "status": "disabled"}
    if server["server_type"] == "builtin":
        return {"ok": True, "status": "ready"}
    if server["server_type"] == "http":
        return {
            "ok": bool(server.get("url")),
            "status": "configured" if server.get("url") else "missing_url",
        }
    argv = server.get("command_json") or []
    if not argv:
        return {"ok": False, "status": "missing_command"}
    try:
        result = subprocess.run(
            argv,
            input="",
            capture_output=True,
            text=True,
            timeout=3,
            env=_env_from_refs(server.get("env_json") or {}),
            shell=False,
        )
        return {
            "ok": result.returncode == 0,
            "status": "ok" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
        }
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc)}


def discover_tools(server: dict) -> list[dict]:
    metadata = server.get("metadata") or {}
    discovered = metadata.get("mock_tools") or metadata.get("discovered_tools") or []
    tools = []
    for item in discovered:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        tools.append(
            {
                "id": item.get("id") or f"mcp.{server['id']}.{name}",
                "server_id": server["id"],
                "name": name,
                "display_name": item.get("display_name") or name,
                "description": item.get("description") or "Discovered MCP tool.",
                "category": item.get("category") or "external_read",
                "input_schema": item.get("input_schema") or {},
                "output_schema": item.get("output_schema") or {},
                "permissions": {"source": "mcp_discovery"},
                "enabled": bool(item.get("enabled", True)),
                "built_in": False,
                "metadata": {"executor": "mcp.read_only", "discovered": True},
            }
        )
    return tools


def execute_mcp_read_only(server: dict, tool: dict, payload: dict[str, Any]) -> dict[str, Any]:
    if server["server_type"] == "http":
        return {
            "executed": False,
            "status": "safe_degraded",
            "message": "HTTP MCP execution is registered but not enabled in this safe build.",
            "tool": tool["name"],
        }
    return {
        "executed": False,
        "status": "safe_degraded",
        "message": "Stdio MCP execution is gated; discovery/health are supported.",
        "tool": tool["name"],
        "input_keys": sorted(payload.keys()),
    }


def _env_from_refs(env_refs: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    for key, ref in env_refs.items():
        if not key or not ref:
            continue
        env[key] = os.environ.get(str(ref), "")
    return env

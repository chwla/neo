from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SECRET_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "password",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                clean[key] = "[redacted]"
            else:
                clean[key] = redact(item)
        return clean
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def emit(data: Any, *, json_output: bool) -> None:
    safe = redact(data)
    if json_output:
        print(json.dumps(safe, indent=2, sort_keys=True, default=str))
    else:
        print_text(safe)


def write_json(path: str, data: Any) -> None:
    target = Path(path)
    target.write_text(
        json.dumps(redact(data), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def print_text(data: Any) -> None:
    if isinstance(data, list):
        if not data:
            print("No records.")
            return
        for item in data:
            print(_line(item))
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, list):
                print(f"{key}: {len(value)}")
                for item in value[:20]:
                    print(f"  {_line(item)}")
            elif isinstance(value, dict):
                print(f"{key}:")
                for subkey, subvalue in value.items():
                    print(f"  {subkey}: {_scalar(subvalue)}")
            else:
                print(f"{key}: {_scalar(value)}")
        return
    print(_scalar(data))


def _line(item: Any) -> str:
    if not isinstance(item, dict):
        return _scalar(item)
    parts = []
    for key in ("id", "name", "display_name", "title", "status", "enabled", "agent_type"):
        if key in item and item[key] is not None:
            parts.append(f"{key}={_scalar(item[key])}")
    return "  ".join(parts) if parts else json.dumps(redact(item), default=str)


def _scalar(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(redact(value), default=str)
    return str(value)

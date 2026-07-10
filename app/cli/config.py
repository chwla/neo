from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_OUTPUT = "table"


@dataclass(frozen=True)
class CliConfig:
    api_url: str
    output: str
    timeout: float
    color: bool


def from_args(args) -> CliConfig:
    return CliConfig(
        api_url=(args.api_url or os.getenv("NEO_API_URL") or DEFAULT_API_URL).rstrip("/"),
        output="json" if args.json else os.getenv("NEO_CLI_OUTPUT", DEFAULT_OUTPUT),
        timeout=float(args.timeout),
        color=not args.no_color,
    )

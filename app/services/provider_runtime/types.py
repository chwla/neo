from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.llm import LLMMessage

RequestType = Literal[
    "chat",
    "coding",
    "research",
    "summary",
    "memory",
    "search",
    "tool_reasoning",
    "embedding_if_available",
]


class RuntimeCompleteRequest(BaseModel):
    request_type: RequestType = "chat"
    route_name: str | None = None
    messages: list[LLMMessage] = Field(default_factory=list, min_length=1, max_length=100)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeHealthRequest(BaseModel):
    route_name: str | None = None
    provider_id: str | None = None
    model_id: str | None = None


class RuntimeResult(BaseModel):
    request_id: str
    status: str
    route: dict[str, Any]
    content: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = None
    retry_count: int = 0
    fallback_chain: list[str] = Field(default_factory=list)
    redaction_summary: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str | None = None

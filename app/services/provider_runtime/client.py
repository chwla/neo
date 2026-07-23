from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from app.services.llm import BaseLLMClient, LLMChatResult, LLMMessage
from app.services.provider_runtime.service import ProviderRuntimeService


class ProviderRuntimeClient(BaseLLMClient):
    def __init__(
        self,
        route_name: str = "chat",
        *,
        num_predict: int | None = None,
        timeout: int | None = None,
    ) -> None:
        self.route_name, self.num_predict, self.timeout = route_name, num_predict, timeout
        self.runtime = ProviderRuntimeService()
        self.model = "runtime"
        self.last_metadata: dict[str, Any] = {}

    def is_available(self) -> bool:
        try:
            return self.runtime.health_check(route_name=self.route_name)["status"] == "healthy"
        except Exception:
            return False

    def model_is_installed(self) -> bool:
        return self.is_available()

    def chat_with_metadata(
        self, messages: list[LLMMessage], temperature: float = 0.4, num_predict: int | None = None
    ) -> LLMChatResult:
        result = self.runtime.complete(
            request_type="chat",
            route_name=self.route_name,
            messages=messages,
            max_tokens=num_predict or self.num_predict,
            metadata={"temperature": temperature},
        )
        if result.status != "completed":
            raise RuntimeError(result.content or "Provider runtime request failed safely.")
        self.last_metadata = {
            "provider_request_id": result.request_id,
            "route_name": result.route.get("route_name"),
            "provider_id": result.route.get("provider_id"),
            "model_id": result.route.get("model_id"),
            "fallback_used": len(result.fallback_chain) > 1,
        }
        return LLMChatResult(
            content=result.content,
            prompt_tokens=result.usage.get("prompt_tokens"),
            completion_tokens=result.usage.get("completion_tokens"),
            total_tokens=result.usage.get("total_tokens"),
            duration_ms=result.latency_ms,
            route_name=self.last_metadata["route_name"],
            provider_id=self.last_metadata["provider_id"],
            model_id=self.last_metadata["model_id"],
            fallback_used=self.last_metadata["fallback_used"],
            provider_request_id=result.request_id,
        )

    def chat_stream(
        self, messages: list[LLMMessage], temperature: float = 0.4, num_predict: int | None = None
    ) -> Iterator[dict[str, Any]]:
        session = self.runtime.start_stream(
            request_type="chat",
            route_name=self.route_name,
            messages=messages,
            max_tokens=num_predict or self.num_predict,
            metadata={"temperature": temperature},
        )
        yield {"type": "start", "request_id": session["id"]}
        seen = 0
        import time

        while session["status"] in {"running", "streaming"}:
            time.sleep(0.02)
            session = self.runtime.request(session["id"]) or session
            partial = (session.get("metadata") or {}).get("partial_response", "")
            if len(partial) > seen:
                yield {"type": "chunk", "content": partial[seen:]}
                seen = len(partial)
        if session["status"] == "completed":
            usage = session.get("provider_usage") or {}
            yield {
                "type": "done",
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "duration_ms": session.get("latency_ms"),
            }
        else:
            raise RuntimeError(session.get("error_message") or "Provider stream failed safely.")

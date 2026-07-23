"""Bounded runtime owning all provider request audit and resilience behavior."""
# ruff: noqa: E501

from __future__ import annotations

import time
from typing import Any

from app.services.llm import LLMMessage
from app.services.llm_registry.providers import build_client
from app.services.provider_runtime import store
from app.services.provider_runtime.budget import context_check, estimate_tokens
from app.services.provider_runtime.errors import safe_error
from app.services.provider_runtime.health import check
from app.services.provider_runtime.rate_limits import decision
from app.services.provider_runtime.redaction import safe_value
from app.services.provider_runtime.retries import MAX_RETRIES, backoff_ms, retryable
from app.services.provider_runtime.router import select
from app.services.provider_runtime.streaming import cancel, clear, start
from app.services.provider_runtime.types import RuntimeResult
from app.services.provider_runtime.usage import summary


class ProviderRuntimeService:
    def __init__(self) -> None:
        store.initialize_provider_runtime_tables()

    def status(self) -> dict:
        return {
            "routes": [
                self._route_status(name)
                for name in (
                    "chat",
                    "coding_agent",
                    "research",
                    "agent",
                    "summarization",
                    "embedding",
                )
            ],
            "usage": summary(),
            "rate_limits": store.list_rates(),
        }

    def _route_status(self, name: str) -> dict:
        try:
            route = select("chat", name)
            return {
                key: route[key]
                for key in ("route_name", "provider", "model", "fallback_chain", "reason")
            }
        except Exception as exc:
            category, message, _ = safe_error(exc)
            return {
                "route_name": name,
                "status": "misconfigured",
                "error_category": category,
                "error_message": message,
            }

    def health_check(self, **kwargs) -> dict:
        return check(**kwargs)

    def health(self) -> list[dict]:
        return store.list_health()

    def request(self, request_id: str) -> dict | None:
        return store.get_request(request_id)

    def requests(self, limit: int = 100) -> list[dict]:
        return store.list_requests(limit)

    def rate_limits(self) -> list[dict]:
        return store.list_rates()

    def usage(self) -> dict:
        return summary()

    def complete(
        self,
        *,
        request_type: str,
        route_name: str | None,
        messages: list[LLMMessage],
        max_tokens: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeResult:
        return self._run(
            request_type, route_name, messages, max_tokens, metadata or {}, stream=False
        )

    def _run(
        self,
        request_type,
        route_name,
        messages,
        max_tokens,
        metadata,
        stream: bool,
        existing_id: str | None = None,
        cancelled=None,
    ) -> RuntimeResult:
        route = select(request_type, route_name)
        estimates = estimate_tokens(messages, max_tokens)
        budget = context_check(estimates, route["model_record"].get("context_window"), max_tokens)
        safe_metadata, redaction = safe_value(
            {**metadata, "budget": budget, "message_count": len(messages)}
        )
        request = (
            store.get_request(existing_id)
            if existing_id
            else store.create_request(
                {
                    "route_name": route["route_name"],
                    "provider_name": route["provider"],
                    "model_name": route["model"],
                    "request_type": request_type,
                    "streaming": stream,
                    **estimates,
                    "fallback_chain": route["fallback_chain"],
                    "metadata": safe_metadata,
                    "redaction_summary": redaction,
                }
            )
        )
        if budget["exceeds"]:
            return self._finish(
                request["id"],
                "blocked",
                route,
                "context_too_large",
                "Context budget exceeded; compact non-safety context first.",
                0,
                [],
                redaction,
            )
        limit = decision(
            store.rate_records(route["route_name"]),
            route["route_name"],
            estimates["total_tokens_estimate"],
        )
        if not limit["allowed"]:
            store.record_rate(
                route["route_name"],
                route["provider"],
                route["model"],
                estimates["total_tokens_estimate"],
                True,
            )
            return self._finish(
                request["id"],
                "blocked",
                route,
                "rate_limited",
                f"Rate limit: {limit['reason']}; retry after about {limit['reset_estimate_seconds']} seconds.",
                0,
                [],
                redaction,
            )
        attempts, fallback_chain, started = 0, [], time.perf_counter()
        targets = [(route["provider_record"], route["model_record"], False)]
        raw = route["route_record"]
        if raw.get("fallback_provider_id") and raw.get("fallback_model_id"):
            from app.services.llm_registry.service import LLMRegistryService

            registry = LLMRegistryService()
            provider, model = (
                registry.get_provider(raw["fallback_provider_id"]),
                registry.get_model(raw["fallback_model_id"]),
            )
            if provider and model:
                targets.append((provider, model, True))
        for provider, model, _fallback in targets:
            try:
                client = build_client(provider, model, num_predict=max_tokens)
                if stream:
                    partial = ""
                    stream_usage: dict[str, int | None] = {}
                    store.update_request(request["id"], status="streaming")
                    for event in client.chat_stream(messages, num_predict=max_tokens):
                        if cancelled and cancelled.is_set():
                            return self._finish(
                                request["id"],
                                "cancelled",
                                route,
                                None,
                                None,
                                attempts,
                                fallback_chain,
                                redaction,
                                partial=partial,
                            )
                        if event.get("type") == "chunk":
                            partial += event.get("content", "")
                            self._partial(request["id"], partial)
                        elif event.get("type") == "done":
                            stream_usage = {
                                key: event.get(key)
                                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                                if event.get(key) is not None
                            }
                    result_content, usage = (
                        partial,
                        stream_usage or {"total_tokens": estimates["total_tokens_estimate"]},
                    )
                else:
                    result = client.chat_with_metadata(messages, num_predict=max_tokens)
                    result_content = result.content
                    usage = {
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "total_tokens": result.total_tokens,
                    }
                store.record_rate(
                    route["route_name"],
                    provider["provider_type"],
                    model["model_name"],
                    estimates["total_tokens_estimate"],
                    False,
                )
                return self._finish(
                    request["id"],
                    "completed",
                    {**route, "provider": provider["provider_type"], "model": model["model_name"]},
                    None,
                    None,
                    attempts,
                    fallback_chain,
                    redaction,
                    content=result_content,
                    usage=usage,
                    latency=int((time.perf_counter() - started) * 1000),
                )
            except Exception as exc:
                category, message, error_redaction = safe_error(exc)
                redaction = error_redaction
                fallback_chain.append(f"{provider['id']}:{category}")
                if retryable(exc) and attempts < MAX_RETRIES:
                    time.sleep(backoff_ms(attempts) / 1000)
                    attempts += 1
                    try:
                        client = build_client(provider, model, num_predict=max_tokens)
                        result = client.chat_with_metadata(messages, num_predict=max_tokens)
                        return self._finish(
                            request["id"],
                            "completed",
                            route,
                            None,
                            None,
                            attempts,
                            fallback_chain,
                            redaction,
                            content=result.content,
                            usage={"total_tokens": result.total_tokens},
                            latency=int((time.perf_counter() - started) * 1000),
                        )
                    except Exception as retry_exc:
                        category, message, redaction = safe_error(retry_exc)
                if category == "auth_or_config":
                    break
        return self._finish(
            request["id"],
            "failed",
            route,
            category,
            message,
            attempts,
            fallback_chain,
            redaction,
            latency=int((time.perf_counter() - started) * 1000),
        )

    def _partial(self, request_id: str, partial: str) -> None:
        current = store.get_request(request_id) or {}
        metadata = current.get("metadata") or {}
        safe, _ = safe_value(partial)
        metadata["partial_response"] = safe
        store.update_request(request_id, metadata=metadata)

    def _finish(
        self,
        request_id,
        status,
        route,
        category,
        message,
        retries,
        fallback,
        redaction,
        content="",
        usage=None,
        latency=None,
        partial=None,
    ) -> RuntimeResult:
        metadata = (store.get_request(request_id) or {}).get("metadata") or {}
        if partial is not None:
            metadata["partial_response"] = partial
        store.update_request(
            request_id,
            status=status,
            provider_name=route.get("provider"),
            model_name=route.get("model"),
            retry_count=retries,
            fallback_chain=fallback,
            latency_ms=latency,
            error_category=category,
            error_message=message,
            redaction_summary=redaction,
            provider_usage=usage or {},
            metadata=metadata,
            completed_at=store.now(),
        )
        return RuntimeResult(
            request_id=request_id,
            status=status,
            route={
                key: value
                for key, value in route.items()
                if key not in {"provider_record", "model_record", "route_record"}
            },
            content=content or message or "",
            usage=usage or {},
            latency_ms=latency,
            retry_count=retries,
            fallback_chain=fallback,
            redaction_summary=redaction,
        )

    def start_stream(self, **kwargs) -> dict:
        route = select(kwargs["request_type"], kwargs.get("route_name"))
        estimates = estimate_tokens(kwargs["messages"], kwargs.get("max_tokens"))
        request = store.create_request(
            {
                "route_name": route["route_name"],
                "provider_name": route["provider"],
                "model_name": route["model"],
                "request_type": kwargs["request_type"],
                "streaming": True,
                **estimates,
                "fallback_chain": route["fallback_chain"],
                "metadata": kwargs.get("metadata") or {},
            }
        )
        start(
            request["id"],
            lambda cancelled: (
                self._run(**kwargs, stream=True, existing_id=request["id"], cancelled=cancelled),
                clear(request["id"]),
            ),
        )
        return request

    def cancel_stream(self, request_id: str) -> dict | None:
        cancel(request_id)
        return store.update_request(request_id, status="cancelled", completed_at=store.now())

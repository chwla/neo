"""Bounded runtime owning all provider request audit and resilience behavior."""
# ruff: noqa: E501

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from app.core.config import get_settings
from app.services.llm import (
    LLMMessage,
    forget_loaded_models,
    ollama_loaded_model,
    ollama_loaded_model_names,
)
from app.services.llm_registry.providers import build_client
from app.services.provider_runtime import store
from app.services.provider_runtime.budget import (
    DEFAULT_CONTEXT_WINDOW,
    context_check,
    estimate_tokens,
    fit_context,
)
from app.services.provider_runtime.errors import safe_error, user_message
from app.services.provider_runtime.health import check
from app.services.provider_runtime.rate_limits import decision
from app.services.provider_runtime.redaction import safe_stream_text, safe_value
from app.services.provider_runtime.retries import MAX_RETRIES, backoff_ms, retryable
from app.services.provider_runtime.router import select
from app.services.provider_runtime.streaming import cancel, clear, start
from app.services.provider_runtime.types import RuntimeResult
from app.services.provider_runtime.usage import summary

#: Provider failures are recorded here in full. Everything the caller sees instead
#: carries only the one safe sentence from ``user_message``.
_LOG = logging.getLogger("neo.provider_runtime")


def _sampling(metadata: dict[str, Any]) -> dict[str, float]:
    """Sampling options a caller asked for, passed to the provider client.

    Callers put ``temperature`` in the request metadata; without this the provider client
    fell back to its own default and a caller asking for near-deterministic output never got it.
    """
    value = metadata.get("temperature")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {}
    return {"temperature": min(2.0, max(0.0, float(value)))}


#: Usage fields a provider may report for one request.
_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "load_ms",
    "prefill_ms",
    "decode_ms",
)


def _result_usage(result: Any) -> dict[str, Any]:
    return {
        key: getattr(result, key, None)
        for key in _USAGE_KEYS
        if getattr(result, key, None) is not None
    }


#: Smallest output limit worth sending; below this a request is refused instead.
_MIN_FITTED_OUTPUT = 256


def _fit_output_to_window(prompt_tokens: int, max_tokens: int | None, window: int) -> int | None:
    """``max_tokens`` lowered so prompt plus output fits ``window``, when it can.

    The prompt estimate is padded by 10% because it counts characters, not the
    model's tokens. A prompt too large to leave a useful output is left alone,
    and the context check refuses it as before.
    """

    if not max_tokens or not window:
        return max_tokens
    padded_prompt = int(prompt_tokens * 1.1) + 64
    if padded_prompt + max_tokens <= window:
        return max_tokens
    room = window - padded_prompt
    if room < _MIN_FITTED_OUTPUT:
        return max_tokens
    return room


def _reuse_loaded_context(provider: dict, model: dict, needed: int) -> int:
    """Keep the context size the model is already loaded with, when it is larger.

    A local runtime reloads the model whenever the requested context size
    changes. Requests of different sizes -- a small helper call, then a long
    answer -- therefore reloaded the model back and forth within one turn, 2-3 s
    each time. Asking for the size that is already loaded never allocates new
    memory (it is held either way) and avoids the reload. Growing is still
    allowed: a request that needs more than is loaded gets what it needs.

    Only Ollama exposes the loaded size, and only when the setting allows it.
    Anything unknown falls back to the size the request needs.
    """

    if provider.get("provider_type") != "ollama":
        return needed
    if str(getattr(get_settings(), "ollama_context_reuse", "on")).lower() == "off":
        return needed
    base_url = provider.get("base_url")
    if not base_url:
        return needed
    loaded = ollama_loaded_model(str(base_url), str(model.get("model_name") or ""))
    if loaded is None:
        return needed
    window = model.get("context_window")
    loaded_context = int(loaded["context_length"])
    if loaded_context <= needed:
        return needed
    if isinstance(window, int) and window > 0 and loaded_context > window:
        return needed
    return loaded_context


class ProviderRuntimeService:
    def __init__(self, *, initialize: bool = True, disable_thinking: bool = False) -> None:
        if initialize:
            store.initialize_provider_runtime_tables()
        #: Ask the routed model not to reason before answering. Set for a
        #: low-effort turn; ignored by any model that cannot be told.
        self.disable_thinking = disable_thinking

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
        tools: list[dict[str, Any]] | None = None,
    ) -> RuntimeResult:
        return self._run(
            request_type,
            route_name,
            messages,
            max_tokens,
            metadata or {},
            stream=False,
            tools=tools,
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
        tools: list[dict[str, Any]] | None = None,
    ) -> RuntimeResult:
        route = select(request_type, route_name)
        estimates = estimate_tokens(messages, max_tokens)
        # An output limit is a ceiling. When the prompt fits but prompt plus limit
        # would not, lower the limit to what the model's window can hold instead
        # of refusing the request -- that is what lets the same budgets work on a
        # model with a small context window.
        fitted = _fit_output_to_window(
            estimates["prompt_tokens_estimate"],
            max_tokens,
            route["model_record"].get("context_window") or DEFAULT_CONTEXT_WINDOW,
        )
        if fitted != max_tokens:
            max_tokens = fitted
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
                (
                    f"This conversation needs about {budget['required_tokens']:,} tokens, "
                    f"but {route['model']} only holds {budget['context_window']:,}. "
                    f"{budget['suggestion']}"
                ),
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

            registry = LLMRegistryService(initialize=False)
            provider, model = (
                registry.get_provider(raw["fallback_provider_id"]),
                registry.get_model(raw["fallback_model_id"]),
            )
            if provider and model:
                targets.append((provider, model, True))
        for provider, model, _fallback in targets:
            try:
                # The fallback model has its own window, so size the request against the
                # model actually about to serve it rather than reusing the primary's.
                num_ctx = _reuse_loaded_context(
                    provider,
                    model,
                    fit_context(
                        budget["required_tokens"],
                        model.get("context_window") or DEFAULT_CONTEXT_WINDOW,
                    ),
                )
                client = build_client(
                    provider,
                    model,
                    num_predict=max_tokens,
                    num_ctx=num_ctx,
                    disable_thinking=self.disable_thinking,
                )
                tool_calls: list[dict[str, Any]] = []
                if stream:
                    partial = ""
                    thinking = ""
                    stream_usage: dict[str, int | None] = {}
                    finish_reason: str | None = None
                    store.update_request(request["id"], status="streaming")
                    for event in client.chat_stream(
                        messages, num_predict=max_tokens, **_sampling(metadata)
                    ):
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
                        elif event.get("type") == "thinking":
                            thinking += event.get("content", "")
                            self._thinking(request["id"], thinking)
                        elif event.get("type") == "done":
                            finish_reason = event.get("finish_reason")
                            stream_usage = {
                                key: event.get(key)
                                for key in _USAGE_KEYS
                                if event.get(key) is not None
                            }
                    result_content, usage = (
                        partial,
                        stream_usage or {"total_tokens": estimates["total_tokens_estimate"]},
                    )
                else:
                    result = client.chat_with_metadata(
                        messages, num_predict=max_tokens, tools=tools, **_sampling(metadata)
                    )
                    result_content = result.content
                    tool_calls = result.tool_calls
                    thinking = result.thinking or ""
                    usage = _result_usage(result)
                    finish_reason = result.finish_reason
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
                    tool_calls=tool_calls,
                    usage=usage,
                    latency=int((time.perf_counter() - started) * 1000),
                    thinking=thinking,
                    finish_reason=finish_reason,
                )
            except Exception as exc:
                category, detail, error_redaction = safe_error(exc)
                redaction = error_redaction
                _LOG.warning(
                    "provider_request_failed request=%s route=%s provider=%s model=%s category=%s",
                    request["id"],
                    route["route_name"],
                    provider["provider_type"],
                    model["model_name"],
                    category,
                    exc_info=exc,
                )
                fallback_chain.append(f"{provider['id']}:{category}")
                if retryable(exc) and attempts < MAX_RETRIES:
                    time.sleep(backoff_ms(attempts) / 1000)
                    attempts += 1
                    try:
                        client = build_client(
                            provider,
                            model,
                            num_predict=max_tokens,
                            num_ctx=num_ctx,
                            disable_thinking=self.disable_thinking,
                        )
                        result = client.chat_with_metadata(
                            messages, num_predict=max_tokens, tools=tools, **_sampling(metadata)
                        )
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
                            tool_calls=result.tool_calls,
                            usage=_result_usage(result),
                            latency=int((time.perf_counter() - started) * 1000),
                            thinking=result.thinking,
                            finish_reason=result.finish_reason,
                        )
                    except Exception as retry_exc:
                        category, detail, redaction = safe_error(retry_exc)
                        _LOG.warning(
                            "provider_retry_failed request=%s route=%s category=%s",
                            request["id"],
                            route["route_name"],
                            category,
                            exc_info=retry_exc,
                        )
                if category == "auth_or_config":
                    break
        # ``message`` is what the caller is allowed to render, so it is derived from the
        # category rather than from the provider's own text. The provider's text stays in
        # ``detail``, which reaches the logs and the audit row's metadata and stops there.
        return self._finish(
            request["id"],
            "failed",
            route,
            category,
            user_message(category, route.get("provider")),
            attempts,
            fallback_chain,
            redaction,
            latency=int((time.perf_counter() - started) * 1000),
            detail=detail,
        )

    def _partial(self, request_id: str, partial: str) -> None:
        current = store.get_request(request_id) or {}
        metadata = current.get("metadata") or {}
        metadata["partial_response"] = safe_stream_text(partial)
        store.update_request(request_id, metadata=metadata)

    def _thinking(self, request_id: str, thinking: str) -> None:
        current = store.get_request(request_id) or {}
        metadata = current.get("metadata") or {}
        metadata["thinking"] = safe_stream_text(thinking)
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
        tool_calls=None,
        usage=None,
        latency=None,
        partial=None,
        thinking=None,
        finish_reason=None,
        detail=None,
    ) -> RuntimeResult:
        # The request that just ended may have loaded or resized a model, so the
        # next request must look again rather than trust what was cached before.
        forget_loaded_models()
        metadata = (store.get_request(request_id) or {}).get("metadata") or {}
        if partial is not None:
            metadata["partial_response"] = partial
        if detail:
            # The provider's own words, kept for whoever debugs the failure. ``message``
            # is the only field a caller may show, so this never travels with it.
            metadata["error_detail"] = detail
        if thinking:
            metadata["thinking"] = safe_stream_text(thinking)
        if finish_reason:
            metadata["finish_reason"] = finish_reason
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
            tool_calls=list(tool_calls or []),
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
            finish_reason=finish_reason,
            error_category=category,
            error_detail=detail or "",
        )

    def warm(self, route_name: str = "chat") -> dict[str, Any]:
        """Load the routed chat model before the first message needs it.

        Local Ollama only, and only when nothing would be lost: the model is not
        already loaded, and no other chat model is loaded that the load might
        evict on a machine short of memory. The setting ``chat_model_warmup``
        turns it off. Loads at the context size an ordinary turn asks for, so
        the first message reuses the instance instead of reloading it.
        """

        settings = get_settings()
        if not getattr(settings, "chat_model_warmup", True):
            return {"status": "disabled"}
        try:
            route = select("chat", route_name)
        except Exception:
            return {"status": "no_route"}
        provider, model = route["provider_record"], route["model_record"]
        base_url = str(provider.get("base_url") or "").rstrip("/")
        name = str(model.get("model_name") or "")
        if provider.get("provider_type") != "ollama" or not base_url or not name:
            return {"status": "not_local"}
        loaded = ollama_loaded_model_names(base_url)
        if loaded is None:
            return {"status": "unreachable"}
        if name in loaded:
            return {"status": "already_loaded"}
        ignored = {name}
        try:
            ignored.add(str(select("chat", "embedding")["model"]))
        except Exception:
            pass
        others = [item for item in loaded if item not in ignored]
        if others:
            return {"status": "skipped", "reason": "another model is loaded", "loaded": others}
        # An ordinary turn: a prompt of roughly a thousand tokens plus the answer limit.
        num_ctx = fit_context(
            1200 + settings.chat_num_predict,
            model.get("context_window") or DEFAULT_CONTEXT_WINDOW,
        )
        started = time.perf_counter()
        try:
            requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": name,
                    "messages": [],
                    "keep_alive": settings.ollama_keep_alive,
                    "options": {"num_ctx": num_ctx},
                },
                timeout=provider.get("timeout_seconds") or 120,
            ).raise_for_status()
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__}
        finally:
            forget_loaded_models()
        return {
            "status": "warmed",
            "model": name,
            "num_ctx": num_ctx,
            "ms": int((time.perf_counter() - started) * 1000),
        }

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

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import requests

from app.services.llm import BaseLLMClient, LLMChatResult, LLMMessage
from app.services.llm_registry.providers import build_client
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.usage import record_call, safe_error


class RoutedLLMClient(BaseLLMClient):
    def __init__(
        self,
        route_name: str,
        *,
        config_id: str | None = None,
        num_predict: int | None = None,
        timeout: int | None = None,
    ) -> None:
        self.route_name = route_name
        self.service = LLMRegistryService()
        self.route = self.service.resolve(route_name, config_id)
        self.num_predict, self.timeout = num_predict, timeout
        model = self.service.get_model(self.route["model_id"])
        self.model = model["model_name"] if model else "unconfigured"
        self.last_metadata: dict[str, Any] = {}

    def _target(self, fallback: bool = False):
        prefix = "fallback_" if fallback else ""
        provider_id, model_id = (
            self.route.get(f"{prefix}provider_id"),
            self.route.get(f"{prefix}model_id"),
        )
        if not provider_id or not model_id:
            return None
        provider, model = self.service.get_provider(provider_id), self.service.get_model(model_id)
        if not provider or not model:
            raise LookupError("Configured LLM provider/model was not found.")
        try:
            client = build_client(
                provider, model, timeout=self.timeout, num_predict=self.num_predict
            )
        except Exception as exc:
            record_call(
                route_name=self.route_name,
                provider_id=provider["id"],
                model_id=model["id"],
                status="failed",
                error=safe_error(exc, provider),
                fallback_used=fallback,
            )
            raise
        return provider, model, client

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        return isinstance(exc, (requests.RequestException, TimeoutError, ConnectionError))

    def is_available(self) -> bool:
        try:
            target = self._target()
            return bool(target and target[2].is_available())
        except Exception:
            return False

    def model_is_installed(self) -> bool:
        try:
            target = self._target()
            return bool(target and target[2].model_is_installed())
        except Exception:
            return False

    def chat_with_metadata(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
    ) -> LLMChatResult:
        effective_temperature = self.route.get("temperature")
        temperature = effective_temperature if effective_temperature is not None else temperature
        output_limit = num_predict or self.route.get("max_output_tokens") or self.num_predict
        primary = self._target()
        if not primary:
            raise RuntimeError(f"LLM route '{self.route_name}' has no primary provider/model.")
        try:
            result = primary[2].chat_with_metadata(messages, temperature, output_limit)
            return self._success(result, primary[0], primary[1], False)
        except Exception as exc:
            record_call(
                route_name=self.route_name,
                provider_id=primary[0]["id"],
                model_id=primary[1]["id"],
                status="failed",
                error=safe_error(exc, primary[0]),
                fallback_used=False,
            )
            fallback = self._target(True)
            if not fallback or not self._retryable(exc):
                raise
            try:
                result = fallback[2].chat_with_metadata(messages, temperature, output_limit)
                return self._success(result, fallback[0], fallback[1], True)
            except Exception as fallback_exc:
                record_call(
                    route_name=self.route_name,
                    provider_id=fallback[0]["id"],
                    model_id=fallback[1]["id"],
                    status="failed",
                    error=safe_error(fallback_exc, fallback[0]),
                    fallback_used=True,
                )
                raise

    def _success(self, result, provider, model, fallback_used):
        result = result.model_copy(
            update={
                "route_name": self.route_name,
                "provider_id": provider["id"],
                "model_id": model["id"],
                "fallback_used": fallback_used,
            }
        )
        record_call(
            route_name=self.route_name,
            provider_id=provider["id"],
            model_id=model["id"],
            status="success",
            result=result,
            fallback_used=fallback_used,
        )
        self.last_metadata = {
            "route_name": self.route_name,
            "provider_id": provider["id"],
            "model_id": model["id"],
            "fallback_used": fallback_used,
        }
        return result

    def chat_stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.4,
        num_predict: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        effective_temperature = self.route.get("temperature")
        temperature = effective_temperature if effective_temperature is not None else temperature
        output_limit = num_predict or self.route.get("max_output_tokens") or self.num_predict
        yielded = False
        primary = self._target()
        if not primary:
            raise RuntimeError(f"LLM route '{self.route_name}' has no primary provider/model.")
        started = time.perf_counter()
        try:
            for event in primary[2].chat_stream(messages, temperature, output_limit):
                yielded = yielded or event.get("type") == "chunk"
                if event.get("type") == "done":
                    result = LLMChatResult(
                        content="",
                        **{
                            k: event.get(k)
                            for k in (
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                                "duration_ms",
                            )
                        },
                    )
                    record_call(
                        route_name=self.route_name,
                        provider_id=primary[0]["id"],
                        model_id=primary[1]["id"],
                        status="success",
                        result=result,
                    )
                    event.update(
                        route_name=self.route_name,
                        provider_id=primary[0]["id"],
                        model_id=primary[1]["id"],
                        fallback_used=False,
                    )
                yield event
            return
        except Exception as exc:
            record_call(
                route_name=self.route_name,
                provider_id=primary[0]["id"],
                model_id=primary[1]["id"],
                status="failed",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=safe_error(exc, primary[0]),
            )
            fallback = self._target(True)
            if yielded or not fallback or not self._retryable(exc):
                raise
        fallback_started = time.perf_counter()
        try:
            for event in fallback[2].chat_stream(messages, temperature, output_limit):
                if event.get("type") == "done":
                    result = LLMChatResult(
                        content="",
                        **{
                            k: event.get(k)
                            for k in (
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                                "duration_ms",
                            )
                        },
                    )
                    record_call(
                        route_name=self.route_name,
                        provider_id=fallback[0]["id"],
                        model_id=fallback[1]["id"],
                        status="success",
                        result=result,
                        fallback_used=True,
                    )
                    event.update(
                        route_name=self.route_name,
                        provider_id=fallback[0]["id"],
                        model_id=fallback[1]["id"],
                        fallback_used=True,
                    )
                yield event
        except Exception as exc:
            record_call(
                route_name=self.route_name,
                provider_id=fallback[0]["id"],
                model_id=fallback[1]["id"],
                status="failed",
                latency_ms=int((time.perf_counter() - fallback_started) * 1000),
                error=safe_error(exc, fallback[0]),
                fallback_used=True,
            )
            raise


def get_routed_client(
    route_name: str = "chat",
    *,
    config_id: str | None = None,
    num_predict: int | None = None,
    timeout: int | None = None,
) -> RoutedLLMClient:
    return RoutedLLMClient(
        route_name, config_id=config_id, num_predict=num_predict, timeout=timeout
    )

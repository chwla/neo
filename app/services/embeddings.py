from __future__ import annotations

import math
import time
from typing import Protocol

import requests

from app.core.config import get_settings
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.usage import record_call, safe_error


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str

    def embed(self, text: str) -> list[float]: ...


class MemoryEmbeddingProvider(Protocol):
    provider_name: str
    model_name: str
    provider_version: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...

    def health(self) -> bool: ...


class EmbeddingValidationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ValidatedMemoryEmbeddingProvider:
    """Provider-only validation boundary; it has no canonical or index access."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        dimension: int,
        provider_version: str = "1",
        maximum_dimension: int = 65_536,
        cooldown_seconds: int = 0,
        clock=time.monotonic,
    ) -> None:
        if not 1 <= dimension <= maximum_dimension:
            raise ValueError("embedding_dimension_out_of_range")
        provider_name = str(getattr(provider, "provider_name", "")).strip()
        model_name = str(getattr(provider, "model_name", "")).strip()
        if not provider_name or len(provider_name) > 80:
            raise ValueError("embedding_provider_identity_invalid")
        if not model_name or len(model_name) > 160:
            raise ValueError("embedding_model_identity_invalid")
        if not provider_version.strip() or len(provider_version) > 80:
            raise ValueError("embedding_provider_version_invalid")
        self._provider = provider
        self.provider_name = provider_name
        self.model_name = model_name
        self.provider_version = provider_version
        self.dimension = dimension
        self.maximum_dimension = maximum_dimension
        self.cooldown_seconds = max(0, cooldown_seconds)
        self.clock = clock
        self.cooldown_until = 0.0
        self.consecutive_failures = 0

    def embed(self, text: str) -> list[float]:
        if not text.strip() or len(text) > 12_000:
            raise EmbeddingValidationError("embedding_invalid_input")
        if self.clock() < self.cooldown_until:
            raise EmbeddingValidationError("embedding_unavailable")
        try:
            raw = self._provider.embed(text)
        except (TimeoutError, requests.Timeout) as exc:
            self._record_failure()
            raise EmbeddingValidationError("embedding_timeout") from exc
        except Exception as exc:
            self._record_failure()
            raise EmbeddingValidationError("embedding_unavailable") from exc
        if not isinstance(raw, list) or not raw:
            self._record_failure()
            raise EmbeddingValidationError("embedding_invalid_response")
        try:
            vector = [float(value) for value in raw]
        except (TypeError, ValueError) as exc:
            self._record_failure()
            raise EmbeddingValidationError("embedding_invalid_response") from exc
        if len(vector) != self.dimension:
            self._record_failure()
            raise EmbeddingValidationError("embedding_dimension_mismatch")
        if len(vector) > self.maximum_dimension or any(
            not math.isfinite(value) for value in vector
        ):
            self._record_failure()
            raise EmbeddingValidationError("embedding_invalid_response")
        self.consecutive_failures = 0
        self.cooldown_until = 0
        return vector

    def health(self) -> bool:
        if self.clock() < self.cooldown_until:
            return False
        health = getattr(self._provider, "health", None)
        if health is None:
            return True
        try:
            result = health()
            if isinstance(result, bool):
                return result
            structured = getattr(result, "healthy", None)
            return structured if isinstance(structured, bool) else False
        except Exception:
            return False

    def _record_failure(self) -> None:
        self.consecutive_failures += 1
        self.cooldown_until = self.clock() + self.cooldown_seconds


class OllamaEmbeddingProvider:
    provider_name = "ollama"

    def __init__(
        self,
        model_name: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
    ) -> None:
        settings = get_settings()
        self._use_registry = model_name is None and base_url is None and timeout is None
        self.model_name = model_name or settings.embedding_model
        self.base_url = (base_url or settings.ollama_url).rstrip("/")
        self.timeout = timeout or settings.embedding_timeout_seconds

    def embed(self, text: str) -> list[float]:
        provider = model = None
        if self._use_registry:
            service = LLMRegistryService()
            route = service.resolve("embedding")
            provider, model = (
                service.get_provider(route["provider_id"]),
                service.get_model(route["model_id"]),
            )
            if not provider or not model or provider["provider_type"] != "ollama":
                raise RuntimeError("Embedding route requires an enabled Ollama provider/model.")
            if not provider["enabled"] or not model["enabled"]:
                raise RuntimeError("Embedding route provider/model is disabled.")
            self.provider_name = provider["provider_type"]
            self.model_name = model["model_name"]
            self.base_url = provider["base_url"].rstrip("/")
            self.timeout = provider["timeout_seconds"]
        started = time.perf_counter()
        try:
            response = requests.post(
                f"{self.base_url}/api/embeddings",
                json={"model": self.model_name, "prompt": text},
                timeout=self.timeout,
            )
            response.raise_for_status()
            embedding = response.json().get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise RuntimeError("Ollama embedding response did not include a vector.")
            if provider and model:
                record_call(
                    route_name="embedding",
                    provider_id=provider["id"],
                    model_id=model["id"],
                    status="success",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            return [float(value) for value in embedding]
        except Exception as exc:
            if provider and model:
                record_call(
                    route_name="embedding",
                    provider_id=provider["id"],
                    model_id=model["id"],
                    status="failed",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=safe_error(exc, provider),
                )
            raise

    def health(self) -> bool:
        """Check the configured Ollama endpoint and exact model without exposing its response."""
        try:
            response = requests.get(
                f"{self.base_url}/api/tags",
                timeout=min(float(self.timeout), 5.0),
            )
            if not response.ok:
                return False
            payload = response.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return False
            names = {
                str(item.get("name") or item.get("model") or "").strip()
                for item in models
                if isinstance(item, dict)
            }
            return self.model_name in names
        except (TypeError, ValueError, requests.RequestException):
            return False

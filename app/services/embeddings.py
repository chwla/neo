from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import requests

from app.core.config import get_settings
from app.models import Memory, MemoryEmbedding
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.usage import record_call, safe_error


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str

    def embed(self, text: str) -> list[float]: ...


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


@dataclass(frozen=True)
class EmbeddingResult:
    status: str
    embedding: MemoryEmbedding


class MemoryEmbeddingService:
    """Best-effort embedding lifecycle for accepted memories."""

    def __init__(self, provider: EmbeddingProvider | None = None) -> None:
        self.provider = provider or OllamaEmbeddingProvider()

    def content_hash(self, memory: Memory) -> str:
        payload = "|".join(
            [
                memory.memory_text or "",
                memory.canonical_slot or "",
                memory.source_sentence or "",
            ],
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def embedding_text(self, memory: Memory) -> str:
        parts = [
            memory.canonical_slot or "",
            memory.memory_type.value
            if hasattr(memory.memory_type, "value")
            else str(memory.memory_type),
            memory.memory_text,
            memory.source_sentence or "",
        ]
        return "\n".join(part for part in parts if part)

    def needs_embedding(self, memory: Memory, existing: MemoryEmbedding | None) -> bool:
        if not memory.is_active or memory.status != "active":
            return False
        expected_hash = self.content_hash(memory)
        return (
            existing is None
            or existing.status != "ready"
            or existing.model != self.provider.model_name
            or existing.provider != self.provider.provider_name
            or existing.content_hash != expected_hash
        )

    def upsert_embedding(
        self,
        memory: Memory,
        existing: MemoryEmbedding | None = None,
        dry_run: bool = False,
    ) -> EmbeddingResult:
        embedding = existing or MemoryEmbedding(memory_id=memory.id)
        if dry_run:
            embedding.status = "missing" if existing is None else "stale"
            return EmbeddingResult(status=embedding.status, embedding=embedding)

        embedding.model = self.provider.model_name
        embedding.provider = self.provider.provider_name
        embedding.content_hash = self.content_hash(memory)
        try:
            vector = self.provider.embed(self.embedding_text(memory))
        except Exception as exc:
            embedding.status = "failed"
            embedding.error = str(exc)[:1000]
            embedding.dimensions = None
            embedding.vector_json = None
            embedding.embedded_at = datetime.now(UTC)
            return EmbeddingResult(status="failed", embedding=embedding)

        embedding.status = "ready"
        embedding.error = None
        embedding.dimensions = len(vector)
        embedding.vector_json = json.dumps(vector, separators=(",", ":"))
        embedding.embedded_at = datetime.now(UTC)
        return EmbeddingResult(status="ready", embedding=embedding)


def decode_vector(vector_json: str | None) -> list[float]:
    if not vector_json:
        return []
    try:
        values = json.loads(vector_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    return [float(value) for value in values]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)

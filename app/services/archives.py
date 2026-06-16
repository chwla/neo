from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http import models

from app.core.config import get_settings


class ArchiveSearchResult(BaseModel):
    collection: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class HashEmbeddingProvider:
    """Deterministic local embeddings for archive plumbing and tests."""

    dimension: int = 384

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        words = text.lower().split()
        if not words:
            return vector
        for word in words:
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        magnitude = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / magnitude for value in vector]


class QdrantArchiveService:
    """Archive adapter for conversations, documents, and notes."""

    COLLECTIONS = ("conversation_archive", "document_archive", "notes_archive")

    def __init__(
        self,
        client: QdrantClient | None = None,
        embeddings: HashEmbeddingProvider | None = None,
    ) -> None:
        self.embeddings = embeddings or HashEmbeddingProvider()
        self.client = client or QdrantClient(url=get_settings().qdrant_url, timeout=2)

    def ensure_collections(self) -> None:
        existing = {collection.name for collection in self.client.get_collections().collections}
        for collection in self.COLLECTIONS:
            if collection in existing:
                continue
            self.client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=self.embeddings.dimension,
                    distance=models.Distance.COSINE,
                ),
            )

    def archive_text(self, collection: str, text: str, metadata: dict[str, Any]) -> str:
        self._validate_collection(collection)
        self.ensure_collections()
        point_id = str(uuid.uuid4())
        payload = {"text": text, **metadata}
        self.client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=self.embeddings.embed(text),
                    payload=payload,
                )
            ],
        )
        return point_id

    def search(self, query: str, collections: list[str] | None = None, limit: int = 5):
        selected = collections or list(self.COLLECTIONS)
        for collection in selected:
            self._validate_collection(collection)
        self.ensure_collections()
        vector = self.embeddings.embed(query)
        results: list[ArchiveSearchResult] = []
        for collection in selected:
            hits = self.client.query_points(
                collection_name=collection,
                query=vector,
                limit=limit,
                with_payload=True,
            ).points
            for hit in hits:
                payload = hit.payload or {}
                results.append(
                    ArchiveSearchResult(
                        collection=collection,
                        text=str(payload.get("text", "")),
                        score=float(hit.score),
                        metadata={key: value for key, value in payload.items() if key != "text"},
                    )
                )
        return sorted(results, key=lambda item: item.score, reverse=True)[:limit]

    def _validate_collection(self, collection: str) -> None:
        if collection not in self.COLLECTIONS:
            raise ValueError(f"Unsupported archive collection: {collection}")


"""Semantic search over what an image turned out to contain.

The embedding is of the *words* the vision model produced -- caption, transcript,
tags -- not of the pixels. Neo's embedding provider is text-only, and a
description embedding answers the question people actually ask ("the one where the
approval button was broken") better than visual similarity would.

Storage and scoring deliberately mirror ``SqliteMemoryVectorIndex``: float32
blobs, a pre-normalised query, a full scan with a heap for the top k. A personal
gallery is a few thousand rows at most, so an approximate index would add a
dependency and a rebuild story to save microseconds.
"""

from __future__ import annotations

import hashlib
import heapq
import math
import struct

from app.core.config import get_settings
from app.services.embeddings import (
    EmbeddingValidationError,
    OllamaEmbeddingProvider,
    ValidatedMemoryEmbeddingProvider,
)
from app.services.gallery import store

#: Embedding input is capped by the provider at 12k characters. A long OCR
#: transcript is truncated rather than refused: the head of a screenshot's text
#: carries the title and headings, which is what a query usually matches.
_MAX_EMBED_CHARS = 8000


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes, dimension: int) -> list[float]:
    return list(struct.unpack(f"<{dimension}f", blob))


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    # Clamped because float error can push an identical pair a hair over 1.0,
    # which would let a single result outrank a perfect lexical match.
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def embedding_text(item: dict) -> str:
    """What gets embedded, and what the content hash is taken over."""

    parts = [
        item.get("title") or "",
        item.get("caption") or "",
        item.get("ocr_text") or "",
        " ".join(item.get("tags") or []),
    ]
    return "\n".join(part for part in parts if part.strip())[:_MAX_EMBED_CHARS]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GalleryVectorIndex:
    def __init__(self, provider=None) -> None:
        settings = get_settings()
        self.dimension = settings.memory_embedding_dimension
        self._provider = provider or ValidatedMemoryEmbeddingProvider(
            OllamaEmbeddingProvider(),
            dimension=self.dimension,
            provider_version=settings.memory_embedding_version,
            cooldown_seconds=settings.memory_provider_cooldown_seconds,
        )

    def index_item(self, item_id: str) -> bool:
        """Embed an item's words. Returns whether a vector was written.

        Skips silently when the text has not changed since the last pass, so
        editing a tag does not re-embed an unchanged caption, and re-enrolling a
        photo costs nothing.
        """

        item = store.get_item(item_id)
        if not item:
            return False
        text = embedding_text(item)
        if not text.strip():
            return False
        digest = content_hash(text)
        existing = store.get_vector(item_id)
        if existing and existing["content_hash"] == digest:
            return False
        try:
            vector = self._provider.embed(text)
        except EmbeddingValidationError:
            # The provider is down or the text is unusable. Lexical search still
            # works, and the next describe or edit will retry.
            return False
        store.upsert_vector(
            {
                "item_id": item_id,
                "provider": self._provider.provider_name,
                "model": self._provider.model_name,
                "provider_version": self._provider.provider_version,
                "dimension": len(vector),
                "content_hash": digest,
                "vector_blob": _pack(vector),
            }
        )
        return True

    def search(self, query: str, limit: int = 50) -> dict[str, float]:
        """Item id -> cosine similarity, for the ranker to blend.

        An unavailable embedder returns nothing rather than raising: search
        degrades to lexical, which is the difference between worse results and no
        results.
        """

        if not query.strip():
            return {}
        try:
            embedded = self._provider.embed(query[:_MAX_EMBED_CHARS])
        except EmbeddingValidationError:
            return {}
        scored: list[tuple[float, str]] = []
        for row in store.all_vectors():
            dimension = int(row["dimension"])
            if dimension != len(embedded):
                # A model change leaves old vectors behind. Skip rather than
                # compare across spaces; the next index pass rewrites them.
                continue
            try:
                candidate = _unpack(row["vector_blob"], dimension)
            except struct.error:
                continue
            score = _cosine(embedded, candidate)
            if score > 0:
                heapq.heappush(scored, (score, row["item_id"]))
                if len(scored) > limit:
                    heapq.heappop(scored)
        return {item_id: score for score, item_id in scored}

    def reindex_all(self) -> int:
        """Embed everything that has words and no current vector."""

        written = 0
        for item in store.scan_items():
            if self.index_item(item["id"]):
                written += 1
        return written

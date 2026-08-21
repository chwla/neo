"""Reconstructible owner-aware FTS and SQLite vector adapters for Phase 6."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as _numpy
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.db.memory_migrations import MEMORY_FTS5_TABLE
from app.models.memory import MemoryFtsDocument, MemoryRecord, MemoryVectorPoint
from app.services.memory.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory.index_contracts import (
    DerivedDocument,
    EmbeddingDocument,
    ProviderHealth,
    VectorCandidate,
)
from app.services.memory.taxonomy import MemoryType
from app.services.memory.versions import (
    DERIVED_DOCUMENT_VERSION,
    EMBEDDING_DOCUMENT_VERSION,
    EMBEDDING_IDENTITY_VERSION,
    VECTOR_METADATA_VERSION,
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, label))


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(value.casefold()))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DerivedDocumentBuilder:
    """Build the exact deterministic safe material sent to derived systems."""

    def build(self, record: MemoryRecord, *, now: datetime) -> DerivedDocument | None:
        if record.status != MemoryLifecycleState.ACTIVE.value:
            return None
        if record.expires_at is not None and _aware(record.expires_at) <= _aware(now):
            return None
        if record.sensitivity != Sensitivity.NORMAL.value:
            return None
        display = (record.display_text or "").strip()
        if not display or len(display) > 12_000:
            return None
        material = {
            "canonical_revision": record.revision,
            "display_text": display,
            "domain_key": record.domain_key,
            "memory_id": record.id,
            "memory_type": record.memory_type,
            "owner_id": record.owner_id,
            "schema_version": DERIVED_DOCUMENT_VERSION,
            "slot_key": record.slot_key,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return DerivedDocument(
            memory_id=UUID(record.id),
            owner_id=UUID(record.owner_id),
            content_hash=hashlib.sha256(encoded.encode()).hexdigest(),
            canonical_content_hash=record.canonical_fingerprint,
            canonical_revision=record.revision,
            memory_type=MemoryType(record.memory_type),
            domain_key=record.domain_key,
            slot_key=record.slot_key,
            display_text=display,
        )

    @staticmethod
    def build_embedding(document: DerivedDocument) -> EmbeddingDocument:
        material = json.dumps(
            {
                "text": document.display_text,
                "version": EMBEDDING_DOCUMENT_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return EmbeddingDocument(
            content_hash=hashlib.sha256(material.encode()).hexdigest(),
            text=document.display_text,
        )


class SqliteMemoryFtsIndex:
    """Dedicated memory FTS5 namespace; results remain untrusted candidate IDs."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._available: bool | None = None

    def _is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            with self.engine.connect() as connection:
                found = connection.scalar(
                    text(
                        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = :name"
                    ),
                    {"name": MEMORY_FTS5_TABLE},
                )
            self._available = bool(found)
        except Exception:
            self._available = False
        return self._available

    def _require_available(self) -> None:
        if not self._is_available():
            raise RuntimeError("fts_unavailable")

    def upsert(self, document: DerivedDocument) -> None:
        self._require_available()
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MemoryFtsDocument).where(
                    MemoryFtsDocument.owner_id == str(document.owner_id),
                    MemoryFtsDocument.memory_id == str(document.memory_id),
                )
            )
            values = {
                "content_hash": document.content_hash,
                "canonical_revision": document.canonical_revision,
                "memory_type": document.memory_type.value,
                "domain_key": document.domain_key,
                "slot_key": document.slot_key,
                "display_text": document.display_text,
                "derived_schema_version": document.schema_version,
            }
            if row is None:
                session.add(
                    MemoryFtsDocument(
                        id=_id(f"fts:{document.owner_id}:{document.memory_id}"),
                        owner_id=str(document.owner_id),
                        memory_id=str(document.memory_id),
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            session.execute(
                text(
                    f"DELETE FROM {MEMORY_FTS5_TABLE} "
                    "WHERE owner_id = :owner_id AND memory_id = :memory_id"
                ),
                {
                    "owner_id": str(document.owner_id),
                    "memory_id": str(document.memory_id),
                },
            )
            session.execute(
                text(
                    f"INSERT INTO {MEMORY_FTS5_TABLE} "
                    "(owner_id, memory_id, content_hash, display_text) "
                    "VALUES (:owner_id, :memory_id, :content_hash, :display_text)"
                ),
                {
                    "owner_id": str(document.owner_id),
                    "memory_id": str(document.memory_id),
                    "content_hash": document.content_hash,
                    "display_text": document.display_text,
                },
            )

    def delete(self, owner_id: str, memory_id: str, expected_hash: str | None = None) -> bool:
        self._require_available()
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MemoryFtsDocument).where(
                    MemoryFtsDocument.owner_id == owner_id,
                    MemoryFtsDocument.memory_id == memory_id,
                )
            )
            if row is None:
                return False
            if expected_hash is not None and row.content_hash != expected_hash:
                return False
            session.execute(
                text(
                    f"DELETE FROM {MEMORY_FTS5_TABLE} "
                    "WHERE owner_id = :owner_id AND memory_id = :memory_id"
                ),
                {"owner_id": owner_id, "memory_id": memory_id},
            )
            session.delete(row)
            return True

    def search(self, owner_id: str, query: str, limit: int) -> list[dict[str, object]]:
        self._require_available()
        bounded_limit = min(max(1, int(limit)), 500)
        terms = sorted(set(_tokens(query)))
        if not terms:
            return []
        match_query = " OR ".join(f'"{term}"' for term in terms)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT owner_id, memory_id, content_hash, "
                    f"bm25({MEMORY_FTS5_TABLE}) AS rank "
                    f"FROM {MEMORY_FTS5_TABLE} "
                    f"WHERE {MEMORY_FTS5_TABLE} MATCH :query "
                    "AND owner_id = :owner_id ORDER BY rank, memory_id LIMIT :limit"
                ),
                {"query": match_query, "owner_id": owner_id, "limit": bounded_limit},
            ).mappings()
            return [
                {
                    "owner_id": row["owner_id"],
                    "memory_id": row["memory_id"],
                    "content_hash": row["content_hash"],
                    "score": 1 / (1 + abs(float(row["rank"]))),
                }
                for row in rows
            ]

    def get_metadata(self, owner_id: str, memory_id: str) -> dict[str, object] | None:
        with self._sessions() as session:
            row = session.scalar(
                select(MemoryFtsDocument).where(
                    MemoryFtsDocument.owner_id == owner_id,
                    MemoryFtsDocument.memory_id == memory_id,
                )
            )
            if row is None:
                return None
            return {
                "owner_id": row.owner_id,
                "memory_id": row.memory_id,
                "content_hash": row.content_hash,
                "canonical_revision": row.canonical_revision,
                "derived_schema_version": row.derived_schema_version,
            }

    def list_metadata_for_owner(
        self,
        owner_id: str,
        *,
        after_memory_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        if limit is not None and not 1 <= limit <= 1_001:
            raise ValueError("fts_metadata_limit_out_of_range")
        statement = select(MemoryFtsDocument).where(MemoryFtsDocument.owner_id == owner_id)
        if after_memory_id is not None:
            statement = statement.where(MemoryFtsDocument.memory_id > str(UUID(after_memory_id)))
        statement = statement.order_by(MemoryFtsDocument.memory_id)
        if limit is not None:
            statement = statement.limit(limit)
        with self._sessions() as session:
            rows = session.scalars(statement)
            return [
                {
                    "owner_id": row.owner_id,
                    "memory_id": row.memory_id,
                    "content_hash": row.content_hash,
                    "canonical_revision": row.canonical_revision,
                    "derived_schema_version": row.derived_schema_version,
                }
                for row in rows
            ]

    def clear_owner(self, owner_id: str) -> int:
        self._require_available()
        with self._sessions.begin() as session:
            session.execute(
                text(f"DELETE FROM {MEMORY_FTS5_TABLE} WHERE owner_id = :owner_id"),
                {"owner_id": owner_id},
            )
            result = session.execute(
                delete(MemoryFtsDocument).where(MemoryFtsDocument.owner_id == owner_id)
            )
            return int(result.rowcount or 0)

    def health(self) -> ProviderHealth:
        healthy = self._is_available()
        return ProviderHealth(
            provider="sqlite",
            model="memory_fts",
            provider_version="1",
            healthy=healthy,
            failure_code=None if healthy else "fts_unavailable",
        )


class SqliteMemoryVectorIndex:
    """Local owner-keyed vector points; authorization still comes from canonical SQL."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def upsert(
        self,
        document: DerivedDocument,
        vector: Sequence[float],
        provider,
        *,
        embedding_document: EmbeddingDocument | None = None,
    ) -> None:
        values = [float(item) for item in vector]
        if not values or any(not math.isfinite(item) for item in values):
            raise ValueError("embedding_invalid_response")
        if len(values) != int(provider.dimension):
            raise ValueError("embedding_dimension_mismatch")
        embedding = embedding_document or DerivedDocumentBuilder.build_embedding(document)
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MemoryVectorPoint).where(
                    MemoryVectorPoint.owner_id == str(document.owner_id),
                    MemoryVectorPoint.memory_id == str(document.memory_id),
                )
            )
            data = {
                "content_hash": document.content_hash,
                "canonical_revision": document.canonical_revision,
                "provider": provider.provider_name,
                "model": provider.model_name,
                "provider_version": provider.provider_version,
                "dimension": len(values),
                "vector_json": json.dumps(values, separators=(",", ":")),
                "vector_blob": _pack_vector(values),
                "metadata_version": VECTOR_METADATA_VERSION,
                "derived_schema_version": document.schema_version,
                "embedding_document_version": embedding.version,
                "embedding_content_hash": embedding.content_hash,
                "embedding_identity_version": EMBEDDING_IDENTITY_VERSION,
            }
            if row is None:
                session.add(
                    MemoryVectorPoint(
                        id=_id(f"vector:{document.owner_id}:{document.memory_id}"),
                        owner_id=str(document.owner_id),
                        memory_id=str(document.memory_id),
                        **data,
                    )
                )
            else:
                for key, value in data.items():
                    setattr(row, key, value)

    def delete(self, owner_id: str, memory_id: str, expected_hash: str | None = None) -> bool:
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MemoryVectorPoint).where(
                    MemoryVectorPoint.owner_id == owner_id,
                    MemoryVectorPoint.memory_id == memory_id,
                )
            )
            if row is None:
                return False
            if expected_hash is not None and row.content_hash != expected_hash:
                return False
            session.delete(row)
            return True

    def search(self, query_vector: Sequence[float], owner_id: str, limit: int):
        bounded_limit = min(max(1, int(limit)), 500)
        vector = [float(item) for item in query_vector]
        query = _numpy_query(vector)
        best: list[tuple[float, int, VectorCandidate]] = []
        with self._sessions() as session:
            rows = session.scalars(
                select(MemoryVectorPoint)
                .where(MemoryVectorPoint.owner_id == owner_id)
                .order_by(MemoryVectorPoint.memory_id)
                .execution_options(yield_per=100)
            )
            for row in rows:
                score = _score_row(row, vector, query)
                candidate = VectorCandidate(
                    owner_id=UUID(row.owner_id),
                    memory_id=UUID(row.memory_id),
                    content_hash=row.content_hash,
                    canonical_revision=row.canonical_revision,
                    score=score,
                    provider=row.provider,
                    model=row.model,
                    provider_version=row.provider_version,
                    dimension=row.dimension,
                    metadata_version=row.metadata_version,
                    derived_schema_version=row.derived_schema_version,
                    embedding_document_version=row.embedding_document_version,
                    embedding_content_hash=row.embedding_content_hash,
                    embedding_identity_version=row.embedding_identity_version,
                )
                entry = (score, -candidate.memory_id.int, candidate)
                if len(best) < bounded_limit:
                    heapq.heappush(best, entry)
                elif entry[:2] > best[0][:2]:
                    heapq.heapreplace(best, entry)
        return [
            item[2] for item in sorted(best, key=lambda item: (-item[0], str(item[2].memory_id)))
        ]

    def get_metadata(self, owner_id: str, memory_id: str) -> dict[str, object] | None:
        with self._sessions() as session:
            row = session.scalar(
                select(MemoryVectorPoint).where(
                    MemoryVectorPoint.owner_id == owner_id,
                    MemoryVectorPoint.memory_id == memory_id,
                )
            )
            if row is None:
                return None
            return {
                "owner_id": row.owner_id,
                "memory_id": row.memory_id,
                "content_hash": row.content_hash,
                "canonical_revision": row.canonical_revision,
                "provider": row.provider,
                "model": row.model,
                "provider_version": row.provider_version,
                "dimension": row.dimension,
                "metadata_version": row.metadata_version,
                "derived_schema_version": row.derived_schema_version,
                "embedding_document_version": row.embedding_document_version,
                "embedding_content_hash": row.embedding_content_hash,
                "embedding_identity_version": row.embedding_identity_version,
            }

    def list_metadata_for_owner(
        self,
        owner_id: str,
        *,
        after_memory_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        if limit is not None and not 1 <= limit <= 1_001:
            raise ValueError("vector_metadata_limit_out_of_range")
        statement = select(MemoryVectorPoint).where(MemoryVectorPoint.owner_id == owner_id)
        if after_memory_id is not None:
            statement = statement.where(MemoryVectorPoint.memory_id > str(UUID(after_memory_id)))
        statement = statement.order_by(MemoryVectorPoint.memory_id)
        if limit is not None:
            statement = statement.limit(limit)
        with self._sessions() as session:
            rows = session.scalars(statement)
            return [
                {
                    "owner_id": row.owner_id,
                    "memory_id": row.memory_id,
                    "content_hash": row.content_hash,
                    "canonical_revision": row.canonical_revision,
                    "provider": row.provider,
                    "model": row.model,
                    "provider_version": row.provider_version,
                    "dimension": row.dimension,
                    "metadata_version": row.metadata_version,
                    "derived_schema_version": row.derived_schema_version,
                    "embedding_document_version": row.embedding_document_version,
                    "embedding_content_hash": row.embedding_content_hash,
                    "embedding_identity_version": row.embedding_identity_version,
                }
                for row in rows
            ]

    def clear_owner(self, owner_id: str) -> int:
        with self._sessions.begin() as session:
            result = session.execute(
                delete(MemoryVectorPoint).where(MemoryVectorPoint.owner_id == owner_id)
            )
            return int(result.rowcount or 0)

    def health(self) -> ProviderHealth:
        try:
            with self.engine.connect() as connection:
                connection.execute(select(1))
            healthy = True
        except Exception:
            healthy = False
        return ProviderHealth(
            provider="sqlite",
            model="memory_vectors",
            provider_version="1",
            healthy=healthy,
            failure_code=None if healthy else "vector_unavailable",
        )


def _pack_vector(values: Sequence[float]) -> bytes:
    """Pack a vector as little-endian float32 for the vectorised scan."""

    return _numpy.asarray(values, dtype="<f4").tobytes()


def _numpy_query(vector: Sequence[float]):
    """Return the L2-normalised query, or ``None`` when it cannot be scored."""

    if not vector:
        return None
    query = _numpy.asarray(vector, dtype="<f4")
    norm = float(_numpy.linalg.norm(query))
    if not norm:
        return None
    return query / norm


def _score_row(row, vector: Sequence[float], query) -> float:
    """Cosine against one stored point, preferring the packed representation.

    Scoring read ``vector_json`` and multiplied element by element in Python, so
    a recall parsed every stored array on every turn: about 1.3 seconds at five
    thousand memories, on a code path that runs before each reply.  The blob is
    the same numbers without the parse, and numpy does the arithmetic in one
    call.  Rows written before revision 0005 have no blob, so the original path
    stays as the fallback rather than requiring a backfill to have run.
    """

    blob = getattr(row, "vector_blob", None)
    if blob and query is not None:
        stored = _numpy.frombuffer(blob, dtype="<f4")
        if stored.size != query.size:
            return 0
        norm = float(_numpy.linalg.norm(stored))
        if not norm:
            return 0
        return max(-1.0, min(1.0, float(stored @ query) / norm))
    return _cosine(vector, json.loads(row.vector_json))


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0
    return max(-1, min(1, dot / (left_norm * right_norm)))

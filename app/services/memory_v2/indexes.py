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

from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.db.memory_v2_migrations import MEMORY_V2_FTS5_TABLE
from app.models.memory_v2 import MemoryFtsDocumentV2, MemoryRecordV2, MemoryVectorPointV2
from app.services.memory_v2.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory_v2.phase6_contracts import (
    DerivedDocument,
    EmbeddingDocument,
    ProviderHealth,
    VectorCandidate,
)
from app.services.memory_v2.taxonomy import MemoryType
from app.services.memory_v2.versions import (
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

    def build(self, record: MemoryRecordV2, *, now: datetime) -> DerivedDocument | None:
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


class SqliteMemoryV2FtsIndex:
    """Dedicated v2 FTS5 namespace; results remain untrusted candidate IDs."""

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
                    {"name": MEMORY_V2_FTS5_TABLE},
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
                select(MemoryFtsDocumentV2).where(
                    MemoryFtsDocumentV2.owner_id == str(document.owner_id),
                    MemoryFtsDocumentV2.memory_id == str(document.memory_id),
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
                    MemoryFtsDocumentV2(
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
                    f"DELETE FROM {MEMORY_V2_FTS5_TABLE} "
                    "WHERE owner_id = :owner_id AND memory_id = :memory_id"
                ),
                {
                    "owner_id": str(document.owner_id),
                    "memory_id": str(document.memory_id),
                },
            )
            session.execute(
                text(
                    f"INSERT INTO {MEMORY_V2_FTS5_TABLE} "
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
                select(MemoryFtsDocumentV2).where(
                    MemoryFtsDocumentV2.owner_id == owner_id,
                    MemoryFtsDocumentV2.memory_id == memory_id,
                )
            )
            if row is None:
                return False
            if expected_hash is not None and row.content_hash != expected_hash:
                return False
            session.execute(
                text(
                    f"DELETE FROM {MEMORY_V2_FTS5_TABLE} "
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
                    f"bm25({MEMORY_V2_FTS5_TABLE}) AS rank "
                    f"FROM {MEMORY_V2_FTS5_TABLE} "
                    f"WHERE {MEMORY_V2_FTS5_TABLE} MATCH :query "
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
                select(MemoryFtsDocumentV2).where(
                    MemoryFtsDocumentV2.owner_id == owner_id,
                    MemoryFtsDocumentV2.memory_id == memory_id,
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
        statement = select(MemoryFtsDocumentV2).where(MemoryFtsDocumentV2.owner_id == owner_id)
        if after_memory_id is not None:
            statement = statement.where(MemoryFtsDocumentV2.memory_id > str(UUID(after_memory_id)))
        statement = statement.order_by(MemoryFtsDocumentV2.memory_id)
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
                text(f"DELETE FROM {MEMORY_V2_FTS5_TABLE} WHERE owner_id = :owner_id"),
                {"owner_id": owner_id},
            )
            result = session.execute(
                delete(MemoryFtsDocumentV2).where(MemoryFtsDocumentV2.owner_id == owner_id)
            )
            return int(result.rowcount or 0)

    def health(self) -> ProviderHealth:
        healthy = self._is_available()
        return ProviderHealth(
            provider="sqlite",
            model="memory_v2_fts",
            provider_version="1",
            healthy=healthy,
            failure_code=None if healthy else "fts_unavailable",
        )


class SqliteMemoryV2VectorIndex:
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
                select(MemoryVectorPointV2).where(
                    MemoryVectorPointV2.owner_id == str(document.owner_id),
                    MemoryVectorPointV2.memory_id == str(document.memory_id),
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
                "metadata_version": VECTOR_METADATA_VERSION,
                "derived_schema_version": document.schema_version,
                "embedding_document_version": embedding.version,
                "embedding_content_hash": embedding.content_hash,
                "embedding_identity_version": EMBEDDING_IDENTITY_VERSION,
            }
            if row is None:
                session.add(
                    MemoryVectorPointV2(
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
                select(MemoryVectorPointV2).where(
                    MemoryVectorPointV2.owner_id == owner_id,
                    MemoryVectorPointV2.memory_id == memory_id,
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
        best: list[tuple[float, int, VectorCandidate]] = []
        with self._sessions() as session:
            rows = session.scalars(
                select(MemoryVectorPointV2)
                .where(MemoryVectorPointV2.owner_id == owner_id)
                .order_by(MemoryVectorPointV2.memory_id)
                .execution_options(yield_per=100)
            )
            for row in rows:
                stored = json.loads(row.vector_json)
                score = _cosine(vector, stored)
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
                select(MemoryVectorPointV2).where(
                    MemoryVectorPointV2.owner_id == owner_id,
                    MemoryVectorPointV2.memory_id == memory_id,
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
        statement = select(MemoryVectorPointV2).where(MemoryVectorPointV2.owner_id == owner_id)
        if after_memory_id is not None:
            statement = statement.where(MemoryVectorPointV2.memory_id > str(UUID(after_memory_id)))
        statement = statement.order_by(MemoryVectorPointV2.memory_id)
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
                delete(MemoryVectorPointV2).where(MemoryVectorPointV2.owner_id == owner_id)
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
            model="memory_v2_vectors",
            provider_version="1",
            healthy=healthy,
            failure_code=None if healthy else "vector_unavailable",
        )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0
    return max(-1, min(1, dot / (left_norm * right_norm)))

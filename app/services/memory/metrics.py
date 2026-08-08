"""Owner-bound, text-free counters for Phase 6 derived recall diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.memory import MemoryDerivedMetric
from app.repositories.memory import MemoryRepository
from app.services.memory.index_contracts import DerivedMetricCode


class MemoryDerivedMetrics:
    """Persist only stable metric codes and counts; never candidate content."""

    def __init__(self, engine: Engine, *, owner_id: str, database_identity: str) -> None:
        self.engine = engine
        self.owner_id = str(UUID(owner_id))
        self.database_identity = database_identity
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def record(self, counts: Mapping[DerivedMetricCode | str, int]) -> None:
        values = {
            DerivedMetricCode(code): int(amount)
            for code, amount in counts.items()
            if int(amount) > 0
        }
        if not values:
            return
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            for code, amount in values.items():
                identifier = str(
                    uuid5(NAMESPACE_URL, f"memory-metric:{self.owner_id}:{code.value}")
                )
                statement = insert(MemoryDerivedMetric).values(
                    id=identifier,
                    owner_id=self.owner_id,
                    metric_code=code.value,
                    count=amount,
                    updated_at=now,
                )
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=(
                            MemoryDerivedMetric.owner_id,
                            MemoryDerivedMetric.metric_code,
                        ),
                        set_={
                            "count": MemoryDerivedMetric.count + amount,
                            "updated_at": now,
                        },
                    )
                )

    def snapshot(self) -> dict[DerivedMetricCode, int]:
        with self._sessions() as session:
            MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            rows = session.scalars(
                select(MemoryDerivedMetric).where(MemoryDerivedMetric.owner_id == self.owner_id)
            )
            return {DerivedMetricCode(item.metric_code): int(item.count) for item in rows}

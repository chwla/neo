"""Tier 5 — the semantic recall path (plan section RCL-47..54).

Vector search is the one part of recall that can return a memory the canonical
store would never have handed over. The index is a *derived* copy: it can lag a
delete, keep a row for a record that no longer exists, or — if a bug ever wrote
one — hold a row stamped with the wrong owner. So every hit is re-validated
against the canonical record before it is allowed to influence a result, and the
tests here are almost entirely about hits that get thrown away.

The validation ladder, in the order `_semantic` applies it: owner, then canonical
existence and eligibility, then the caller's type/domain filters, then indexing
policy, then a full identity check (content hash, revision, provider, model,
dimension, and four schema versions). A hit must survive all of it. Each rung
also files a repair, because a hit that failed validation is evidence the index
is wrong and will keep being wrong until something fixes it.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.memory import MemoryRecord as MemoryRecordRow
from app.repositories.memory import MemoryRepository
from app.services.memory.contracts import MemoryLifecycleState
from app.services.memory.index_contracts import DerivedMetricCode, DerivedTarget
from app.services.memory.indexes import DerivedDocumentBuilder
from app.services.memory.queries import (
    MemoryQueryContext,
    RecallMode,
    RecallQuery,
    RecallReasonCode,
)
from app.services.memory.recall import CanonicalRecallService
from app.services.memory.settings import MemorySettings
from app.services.memory.versions import EMBEDDING_IDENTITY_VERSION, VECTOR_METADATA_VERSION
from tests.memory import factories
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID

QUERY_TEXT = "urban sketching"


class StubSemanticProvider:
    """An embedding provider shaped the way *recall* expects one.

    Deliberately not `doubles.FakeEmbeddingProvider`: that one returns a
    `ProviderHealth` object from `health()`, which suits the vector-index tests
    but is always truthy. Recall does `if not health(): degrade`, and the
    production `ValidatedMemoryEmbeddingProvider.health()` returns a plain
    `bool`. Reusing the other double here would make the unhealthy branch
    unreachable and the degradation test pass for the wrong reason.
    """

    provider_name = "fake"
    model_name = "fake-embed-v1"
    provider_version = "1"
    dimension = 8

    def __init__(self, *, healthy: bool = True, embed_error: Exception | None = None) -> None:
        self._healthy = healthy
        self._embed_error = embed_error
        self.embed_calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        if self._embed_error is not None:
            raise self._embed_error
        return [0.1] * self.dimension

    def health(self) -> bool:
        return self._healthy


class StubVectorIndex:
    """Returns scripted hits, so each rung of the validation ladder is reachable.

    A real `SqliteMemoryVectorIndex` filters by owner in SQL, which makes the
    wrong-owner case impossible to construct — and that case is exactly the one
    worth testing, since it is the shape a corrupted index takes.
    """

    def __init__(self, hits: list[dict] | None = None, error: Exception | None = None) -> None:
        self.hits = hits or []
        self.error = error
        self.searches: list[tuple[str, int]] = []

    def search(self, vector, owner_id: str, limit: int):
        self.searches.append((owner_id, limit))
        if self.error is not None:
            raise self.error
        return list(self.hits)


class RecordingScheduler:
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list = []
        self.error = error

    def __call__(self, request) -> None:
        self.requests.append(request)
        if self.error is not None:
            raise self.error


def _row(engine: Engine, record_id: str) -> MemoryRecordRow:
    with Session(engine) as session:
        return session.scalar(
            select(MemoryRecordRow).where(MemoryRecordRow.id == record_id)
        )


def _valid_hit(engine: Engine, record_id: str, *, score: float = 0.9, **overrides) -> dict:
    """A hit that passes every validation rung, before overrides are applied.

    Built from the record's own derived document so the identity fields are
    genuinely correct. Each drop test then changes exactly one field, which is
    what makes the resulting failure attributable.
    """

    builder = DerivedDocumentBuilder()
    row = _row(engine, record_id)
    document = builder.build(row, now=FROZEN_NOW)
    embedding = builder.build_embedding(document)
    hit = {
        "owner_id": OWNER_ID,
        "memory_id": record_id,
        "content_hash": document.content_hash,
        "canonical_revision": row.revision,
        "score": score,
        "provider": StubSemanticProvider.provider_name,
        "model": StubSemanticProvider.model_name,
        "provider_version": StubSemanticProvider.provider_version,
        "dimension": StubSemanticProvider.dimension,
        "metadata_version": VECTOR_METADATA_VERSION,
        "derived_schema_version": document.schema_version,
        "embedding_document_version": embedding.version,
        "embedding_content_hash": embedding.content_hash,
        "embedding_identity_version": EMBEDDING_IDENTITY_VERSION,
    }
    hit.update(overrides)
    return hit


def _service(
    engine: Engine,
    session: Session,
    database_identity: str,
    *,
    hits: list[dict] | None = None,
    provider: StubSemanticProvider | None = None,
    vector_index: StubVectorIndex | None = None,
    scheduler: RecordingScheduler | None = None,
    metrics=None,
    **flag_overrides,
) -> CanonicalRecallService:
    repository = MemoryRepository(
        session, owner_id=OWNER_ID, database_identity=database_identity
    )
    flag_values = {"semantic_recall_enabled": True}
    flag_values.update(flag_overrides)
    flags = MemorySettings(**flag_values)
    return CanonicalRecallService(
        repository,
        flags=flags,
        semantic_provider=provider or StubSemanticProvider(),
        vector_index=vector_index if vector_index is not None else StubVectorIndex(hits),
        repair_scheduler=scheduler,
        metric_recorder=metrics,
    )


def _query(database_identity: str, text: str = QUERY_TEXT, **context_overrides) -> RecallQuery:
    base = {
        "owner_id": OWNER_ID,
        "database_identity": database_identity,
        "profile_id": "profile-1",
        "request_id": "request-1",
        "current_time": FROZEN_NOW,
        "mode": RecallMode.SCOPED_LEXICAL,
    }
    base.update(context_overrides)
    return RecallQuery(context=MemoryQueryContext(**base), text=text)


class TestValidatedHits:
    def test_a_fully_valid_hit_is_used(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """The control case. Every drop test below is this, minus one field.

        Without it, a bug that rejected everything would make all the drop tests
        pass while semantic recall returned nothing at all.

        The record shares no token with the query, so it cannot arrive by the
        lexical route. That is what makes its presence evidence about the
        semantic path specifically rather than about recall in general.
        """

        record_id = factories.insert_record(engine, display_text="plays the cello on tuesdays")
        service = _service(
            engine, session, database_identity, hits=[_valid_hit(engine, record_id)]
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_candidate_count == 1
        assert result.diagnostic.semantic_validated_count == 1
        assert result.canonical_ids == (UUID(record_id),)

    def test_the_search_is_owner_scoped_at_the_call(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        index = StubVectorIndex([])
        service = _service(engine, session, database_identity, vector_index=index)

        service.recall(_query(database_identity))

        assert index.searches and index.searches[0][0] == OWNER_ID


class TestDroppedHits:
    """Each case drops a hit, counts it, and files a repair for the index."""

    def test_a_wrong_owner_hit_is_dropped_and_repaired(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-47 — the one that would be a cross-owner leak if it were used.

        The stored row claims another owner. Even though the search was made with
        this owner's id, the hit is re-checked, because an index row carrying the
        wrong owner is precisely the corruption this guard exists for.
        """

        # Deliberately shares no token with the query, so the lexical path cannot
        # return it. If this id shows up in the result, the only route it could
        # have taken is the semantic one — which is the leak being tested for.
        record_id = factories.insert_record(engine, display_text="plays the cello on tuesdays")
        scheduler = RecordingScheduler()
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id, owner_id=OTHER_OWNER_ID)],
            scheduler=scheduler,
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_wrong_owner_drop_count == 1
        assert result.diagnostic.semantic_validated_count == 0
        assert UUID(record_id) not in result.canonical_ids
        assert len(scheduler.requests) == 1
        assert scheduler.requests[0].action == "delete"
        assert scheduler.requests[0].reason == "wrong_owner_metadata"
        assert scheduler.requests[0].target is DerivedTarget.VECTOR

    def test_a_stale_hit_is_dropped_and_scheduled_for_reindexing(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-48 — the record changed after it was indexed.

        The repair is an `upsert`, not a `delete`: the record is fine, its
        derived copy is out of date. Using the stale vector would rank the
        memory by text it no longer contains.
        """

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        scheduler = RecordingScheduler()
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id, content_hash="b" * 64)],
            scheduler=scheduler,
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_stale_drop_count == 1
        assert result.diagnostic.semantic_validated_count == 0
        assert scheduler.requests[0].action == "upsert"
        assert scheduler.requests[0].reason == "semantic_hash_or_model_stale"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("provider", "someone-else"),
            ("model", "different-model"),
            ("provider_version", "99"),
            ("dimension", 16),
            ("embedding_content_hash", "c" * 64),
            ("canonical_revision", 99),
        ],
    )
    def test_every_identity_component_can_invalidate_a_hit(
        self,
        engine: Engine,
        session: Session,
        database_identity: str,
        field: str,
        value: object,
    ) -> None:
        """RCL-48b — vectors are only comparable within one model and version.

        A vector produced by a different model is not a worse match; it is a
        number from a different space. Parametrised so a component dropped from
        the identity check fails here rather than silently mixing spaces.
        """

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id, **{field: value})],
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_stale_drop_count == 1
        assert result.diagnostic.semantic_validated_count == 0

    def test_a_ghost_hit_is_dropped_and_deleted(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-49 — an index row whose canonical record does not exist at all."""

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        ghost = _valid_hit(engine, record_id, memory_id=str(uuid4()))
        scheduler = RecordingScheduler()
        service = _service(
            engine, session, database_identity, hits=[ghost], scheduler=scheduler
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_ghost_drop_count == 1
        assert scheduler.requests[0].action == "delete"
        assert scheduler.requests[0].reason == "semantic_ghost"

    def test_an_inactive_hit_is_dropped_and_distinguished_from_a_ghost(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-50 — the record exists but is forgotten, which is not the same thing.

        The distinction matters for diagnosis: ghosts mean the index kept a row
        past a hard delete, inactive means it kept one past a lifecycle change.
        Both are repaired, but conflating them hides which bug is happening.
        """

        # The index row is built while the record is still active, then the
        # record is forgotten — which is the real sequence. Building the hit from
        # an already-forgotten record is impossible: the document builder refuses
        # to produce one, which is IDX-01's guarantee.
        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        hit = _valid_hit(engine, record_id)
        with Session(engine) as forgetting:
            forgetting.execute(
                update(MemoryRecordRow)
                .where(MemoryRecordRow.id == record_id)
                .values(status=MemoryLifecycleState.FORGOTTEN.value)
            )
            forgetting.commit()
        scheduler = RecordingScheduler()
        service = _service(
            engine,
            session,
            database_identity,
            hits=[hit],
            scheduler=scheduler,
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_inactive_drop_count == 1
        assert result.diagnostic.semantic_ghost_drop_count == 0
        assert scheduler.requests[0].reason == "semantic_inactive"

    def test_drops_are_reported_to_the_metric_recorder(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-47b — the wrong-owner counter is a signal someone should act on."""

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        recorded: list[dict] = []
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id, owner_id=OTHER_OWNER_ID)],
            metrics=recorded.append,
        )

        service.recall(_query(database_identity))

        assert recorded
        counts = recorded[0]
        # Named explicitly rather than "some counter is 1": the wrong-owner
        # counter is the one an operator would alert on, and a test that accepts
        # any non-zero counter would pass if the drop were miscategorised.
        assert counts[DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT] == 1
        assert counts[DerivedMetricCode.SEMANTIC_GHOST_HIT_DROP] == 0
        assert counts[DerivedMetricCode.SEMANTIC_STALE_HIT_DROP] == 0


class TestRepairIsBestEffort:
    def test_a_failing_repair_scheduler_does_not_fail_recall(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-51 — repair is maintenance; the user still asked a question.

        The hit is still dropped. What must not happen is the exception reaching
        the caller, which would turn a stale index row into a failed chat turn.
        """

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        good = factories.insert_record(engine, display_text="urban sketching supplies")
        scheduler = RecordingScheduler(error=RuntimeError("outbox is unavailable"))
        service = _service(
            engine,
            session,
            database_identity,
            hits=[
                _valid_hit(engine, record_id, owner_id=OTHER_OWNER_ID),
                _valid_hit(engine, good),
            ],
            scheduler=scheduler,
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_wrong_owner_drop_count == 1
        assert result.diagnostic.semantic_repair_count == 0
        assert UUID(good) in result.canonical_ids

    def test_no_scheduler_at_all_is_tolerated(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id, owner_id=OTHER_OWNER_ID)],
            scheduler=None,
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_repair_count == 0


class TestHybridScoring:
    def test_a_semantic_hit_is_blended_and_bounded(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-52 — every component and the total stay inside 0–1.

        The contract bounds these, so an out-of-range blend raises rather than
        ranking wrongly; asserting them here pins that the blend cannot produce
        one in the first place.
        """

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id, score=1.0)],
        )

        result = service.recall(_query(database_identity))

        assert result.items
        score = result.items[0].score
        assert 0.0 <= score.semantic <= 1.0
        assert 0.0 <= score.total <= 1.0
        assert score.semantic > 0

    def test_a_hit_below_the_threshold_is_not_scored(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """A weak vector match must not drag in an unrelated memory.

        The raw cosine is mapped from -1..1 onto 0..1, so a score of -1 becomes
        0 — far below any sane threshold.
        """

        record_id = factories.insert_record(engine, display_text="something unrelated entirely")
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id, score=-1.0)],
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.semantic_validated_count == 0
        assert UUID(record_id) not in result.canonical_ids


class TestDegradation:
    def test_an_unhealthy_provider_degrades_without_embedding(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """The health pre-check runs before any work is attempted."""

        factories.insert_record(engine, display_text="improve at urban sketching")
        provider = StubSemanticProvider(healthy=False)
        service = _service(engine, session, database_identity, provider=provider)

        result = service.recall(_query(database_identity))

        assert result.diagnostic.degraded_semantic_reason == "embedding_unhealthy"
        assert provider.embed_calls == []

    def test_a_failing_vector_search_degrades_to_lexical(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """The lexical answer still gets returned — semantic is an enhancement."""

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        service = _service(
            engine,
            session,
            database_identity,
            vector_index=StubVectorIndex(error=RuntimeError("vector store is down")),
        )

        result = service.recall(_query(database_identity))

        assert result.diagnostic.degraded_semantic_reason == "semantic_unavailable"
        assert UUID(record_id) in result.canonical_ids

    def test_lexical_disabled_still_returns_semantic_results(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-53 — corrected on the flag, right on the behaviour.

        With lexical off and semantic up, recall returns semantic-only results,
        as the plan expected. But `degraded_lexical` is **False**, not True: the
        flag means "lexical was attempted and failed", and `_fetch` sets it only
        when the FTS query raises. A deliberately disabled lexical path is not a
        degradation, it is a configuration — arguable either way, but pinned as
        it is so the diagnostic is not read as covering both. See
        `decisions.md` 46.
        """

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        service = _service(
            engine,
            session,
            database_identity,
            hits=[_valid_hit(engine, record_id)],
        )

        result = service.recall(_query(database_identity, lexical_available=False))

        assert result.canonical_ids == (UUID(record_id),)
        assert result.diagnostic.semantic_validated_count == 1
        assert result.diagnostic.degraded_lexical is False

    def test_both_paths_unavailable_returns_an_empty_result(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """RCL-54 — corrected: one reason code, not two.

        The plan expected both codes. Recall short-circuits before `_semantic`
        ever runs, so there is no semantic diagnosis to report and the result
        carries `LEXICAL_UNAVAILABLE` alone. No exception either way, which is
        the part that actually matters.
        """

        factories.insert_record(engine, display_text="improve at urban sketching")
        service = _service(
            engine,
            session,
            database_identity,
            semantic_recall_enabled=False,
        )

        result = service.recall(_query(database_identity, lexical_available=False))

        assert result.items == ()
        assert result.diagnostic.reason_codes == (RecallReasonCode.LEXICAL_UNAVAILABLE,)
        assert result.diagnostic.degraded_lexical is True
        assert result.diagnostic.degraded_semantic_reason is None

    def test_a_short_query_does_not_reach_the_vector_index(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """Semantic search needs at least two tokens to mean anything."""

        factories.insert_record(engine, display_text="improve at urban sketching")
        index = StubVectorIndex([])
        service = _service(engine, session, database_identity, vector_index=index)

        service.recall(_query(database_identity, text="sketching"))

        assert index.searches == []

    def test_deterministic_mode_does_not_use_semantic_search(
        self, engine: Engine, session: Session, database_identity: str
    ) -> None:
        """Deterministic recall answers by id or slot; a fuzzy match would defeat it."""

        record_id = factories.insert_record(engine, display_text="improve at urban sketching")
        index = StubVectorIndex([])
        service = _service(engine, session, database_identity, vector_index=index)

        service.recall(
            RecallQuery(
                context=MemoryQueryContext(
                    owner_id=OWNER_ID,
                    database_identity=database_identity,
                    profile_id="profile-1",
                    request_id="request-1",
                    current_time=FROZEN_NOW,
                    mode=RecallMode.DETERMINISTIC,
                ),
                text=QUERY_TEXT,
                canonical_id=record_id,
            )
        )

        assert index.searches == []

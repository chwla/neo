"""Tier 6 — runtime assembly and the inline index worker (plan section RUN-12..22).

Three things live here, and they fail in three different ways.

`_ensure_memory_schema` is a process-lifetime cache in front of a migration. Its
job is to run `upgrade_memory` once per profile database instead of once per
runtime build — a runtime is built several times per chat turn, and each build
otherwise opened a write-capable connection to the database the chat worker was
writing to.

`build_memory_runtime` assembles everything a turn needs, and the property worth
pinning is that the owner binding survives assembly intact.

`drain_memory_outbox` is the inline index worker. Writing a memory only
*enqueues* its indexing work; nothing in the deployed app drained that queue for
a while, so the derived tables stayed empty and recall fell back to literal word
overlap. It is best-effort by design and must never raise into the write that
produced the work.

**No real profile directories are created.** `_root()` is redirected to the
test's `tmp_path` and every profile is a guest, which avoids the account
registry entirely.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.db.memory_migrations import memory_migration_state
from app.models.memory import MemoryOutbox
from app.services import profile_accounts
from app.services.memory import factory, runtime
from app.services.memory.settings import MemorySettings
from tests.memory import factories
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    """`_verified_memory_schemas` is process-lifetime state keyed by profile.

    Without clearing it, the first test to migrate a path decides the answer for
    every later test using that path — and worse, a test asserting the migration
    *ran* would pass or fail depending on ordering alone.
    """

    factory._verified_memory_schemas.clear()
    yield
    factory._verified_memory_schemas.clear()


@pytest.fixture
def profile_root(tmp_path, monkeypatch):
    """Redirect the profile tree into tmp_path.

    `database_url_for` calls `mkdir(parents=True)`, so without this a test would
    create real profile directories under the user's data dir — which then have
    to be cleaned up by hand.
    """

    root = tmp_path / "neo-data"
    root.mkdir()
    base = get_settings().model_copy(update={"data_dir": str(root)})
    monkeypatch.setattr(profile_accounts, "get_base_settings", lambda: base)
    # A guest profile derives its key material from an `owner_id` file written
    # when the profile is created. Seeding it keeps the whole fixture inside
    # tmp_path instead of reaching for the real guest registry.
    guest_dir = root / "profiles" / "guests" / "guest-1"
    guest_dir.mkdir(parents=True)
    (guest_dir / "owner_id").write_text(OWNER_ID)
    return root


def _url(tmp_path, name: str = "profile.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _enqueue(engine: Engine, memory_id: str, *, owner: str = OWNER_ID) -> str:
    """One pending canonical_upsert event for a record.

    Written locally rather than added to `factories.py`: that file is shared with
    the session working Tier 3/4, and a one-caller helper does not justify a
    change to it.
    """

    event_id = str(uuid4())
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT canonical_fingerprint, revision FROM memory_records WHERE id = :i"),
            {"i": memory_id},
        ).first()
        connection.execute(
            insert(MemoryOutbox).values(
                id=event_id,
                owner_id=owner,
                event_kind="canonical_upsert",
                memory_id=memory_id,
                canonical_revision=row[1],
                content_hash=row[0],
                event_payload_json={},
                state="pending",
                attempts=0,
                event_idempotency_key=f"key-{event_id}",
                schema_version=1,
                created_at=FROZEN_NOW,
                updated_at=FROZEN_NOW,
            )
        )
    return event_id


class TestEnsureMemorySchema:
    def test_a_fresh_database_is_migrated(self, tmp_path) -> None:
        """RUN-12"""

        url = _url(tmp_path)
        factory._ensure_memory_schema(url, OWNER_ID, "account-profile:p1")

        engine = create_engine(url, future=True)
        try:
            state = memory_migration_state(engine)
        finally:
            engine.dispose()

        assert state.current_revision is not None
        assert state.applied_revisions

    def test_calling_twice_does_not_reopen_the_database(self, tmp_path, monkeypatch) -> None:
        """RUN-13 — idempotent *and* cached, which are different claims.

        Asserting only that a second call succeeds would pass even if the cache
        were removed and the migration re-run every time — which is the exact
        contention this function exists to avoid. So the assertion is on the
        number of engine builds, not on the outcome.
        """

        url = _url(tmp_path)
        factory._ensure_memory_schema(url, OWNER_ID, "account-profile:p1")

        builds: list[str] = []
        real_build = factory.build_engine
        monkeypatch.setattr(
            factory,
            "build_engine",
            lambda value: (builds.append(value), real_build(value))[1],
        )

        factory._ensure_memory_schema(url, OWNER_ID, "account-profile:p1")

        assert builds == []

    def test_a_different_owner_on_the_same_database_is_refused(self, tmp_path) -> None:
        """RUN-14 — the cache key includes the binding, so this is still checked.

        Caching on the URL alone would let a database reached with a different
        owner be silently accepted after the first verification — one profile
        reading another's store, which is the failure the binding exists to stop.
        """

        url = _url(tmp_path)
        factory._ensure_memory_schema(url, OWNER_ID, "account-profile:p1")

        with pytest.raises(Exception):
            factory._ensure_memory_schema(url, OTHER_OWNER_ID, "account-profile:p1")

    def test_a_different_identity_on_the_same_database_is_refused(self, tmp_path) -> None:
        url = _url(tmp_path)
        factory._ensure_memory_schema(url, OWNER_ID, "account-profile:p1")

        with pytest.raises(Exception):
            factory._ensure_memory_schema(url, OWNER_ID, "account-profile:p2")


class TestBuildMemoryRuntime:
    @pytest.fixture(autouse=True)
    def _no_live_model(self, monkeypatch):
        """Extraction off, so assembly never reaches for a model over the network."""

        base = get_settings().model_copy(update={"memory_extraction_enabled": False})
        monkeypatch.setattr(factory, "get_settings", lambda: base)
        monkeypatch.setattr(runtime, "get_settings", lambda: base, raising=False)

    def _profile(self) -> dict:
        return {"id": "guest-1", "owner_id": OWNER_ID, "is_guest": True}

    def test_the_runtime_carries_the_profiles_owner_and_identity(
        self, profile_root
    ) -> None:
        """RUN-15 — the binding must survive assembly.

        Everything downstream trusts `execution`: get the owner wrong here and
        every read and write in the turn is scoped to the wrong store.
        """

        built = factory.build_memory_runtime(self._profile())

        assert built.execution.owner_id == OWNER_ID
        assert built.execution.database_identity == "guest-profile:guest-1"
        assert built.execution.database_url.endswith("neo.db")
        assert "guests/guest-1" in built.execution.database_url

    def test_building_twice_is_safe_and_migrates_once(self, profile_root) -> None:
        """RUN-16 — a runtime is built several times per turn."""

        first = factory.build_memory_runtime(self._profile())
        second = factory.build_memory_runtime(self._profile())

        assert first.execution.owner_id == second.execution.owner_id
        assert first.execution.database_url == second.execution.database_url
        assert len(factory._verified_memory_schemas) == 1

    def test_the_runtime_exposes_its_adapters(self, profile_root) -> None:
        built = factory.build_memory_runtime(self._profile())

        assert built.adapter is not None
        assert built.chat_adapter is not None
        assert built.extraction is not None

    def test_no_model_is_built_when_extraction_is_disabled(self, profile_root) -> None:
        """The guard that keeps a disabled deployment from probing a model."""

        built = factory.build_memory_runtime(self._profile())

        assert built.extraction.model is None


class TestRecallDependencies:
    def test_disabling_the_worker_removes_the_fts_index(
        self, engine: Engine, database_identity: str
    ) -> None:
        """RUN-17a"""

        dependencies = runtime.build_memory_recall_dependencies(
            engine,
            owner_id=OWNER_ID,
            database_identity=database_identity,
            flags=MemorySettings(fts_index_enabled=False),
            settings=get_settings(),
        )

        assert dependencies.fts_index is None

    def test_disabling_semantic_recall_removes_the_provider_and_vector_index(
        self, engine: Engine, database_identity: str
    ) -> None:
        """RUN-17b — and the FTS index survives, so the two are independent."""

        dependencies = runtime.build_memory_recall_dependencies(
            engine,
            owner_id=OWNER_ID,
            database_identity=database_identity,
            flags=MemorySettings(semantic_recall_enabled=False),
            settings=get_settings(),
        )

        assert dependencies.semantic_provider is None
        assert dependencies.vector_index is None
        assert dependencies.fts_index is not None

    def test_a_disabled_owner_gets_nothing(
        self, engine: Engine, database_identity: str
    ) -> None:
        """A blank owner must not reach a store, so nothing is wired at all."""

        dependencies = runtime.build_memory_recall_dependencies(
            engine,
            owner_id="",
            database_identity=database_identity,
            flags=MemorySettings(),
            settings=get_settings(),
        )

        assert dependencies.fts_index is None
        assert dependencies.vector_index is None
        assert dependencies.semantic_provider is None

    def test_a_non_ollama_embedding_provider_yields_no_semantic_path(
        self, engine: Engine, database_identity: str
    ) -> None:
        dependencies = runtime.build_memory_recall_dependencies(
            engine,
            owner_id=OWNER_ID,
            database_identity=database_identity,
            flags=MemorySettings(),
            settings=get_settings().model_copy(
                update={"memory_embedding_provider": "none"}
            ),
        )

        assert dependencies.semantic_provider is None
        assert dependencies.fts_index is not None


class TestSemanticDuplicateFinder:
    def _finder(self, tmp_path, database_identity, **flag_overrides):
        flags = MemorySettings(**flag_overrides)
        return runtime.build_semantic_duplicate_finder(
            database_url=_url(tmp_path),
            owner_id=OWNER_ID,
            database_identity=database_identity,
            flags=flags,
            settings=get_settings(),
        )

    def test_semantic_off_returns_no_finder(self, tmp_path, database_identity) -> None:
        """RUN-18 — None, not a callable that always misses.

        Failing to notice a duplicate must never block a write, so the absent
        case is expressed by having nothing to call rather than by a callable
        that quietly returns None forever.
        """

        assert self._finder(tmp_path, database_identity, semantic_recall_enabled=False) is None

    def test_a_disabled_owner_returns_no_finder(self, tmp_path, database_identity) -> None:
        finder = runtime.build_semantic_duplicate_finder(
            database_url=_url(tmp_path),
            owner_id="",
            database_identity=database_identity,
            flags=MemorySettings(),
            settings=get_settings(),
        )

        assert finder is None

    @pytest.mark.parametrize(
        ("text", "allowed"),
        [("", frozenset({uuid4()})), ("some text", frozenset())],
        ids=["blank_text", "nothing_to_compare"],
    )
    def test_the_finder_short_circuits_with_nothing_to_do(
        self, tmp_path, database_identity, monkeypatch, text: str, allowed: frozenset
    ) -> None:
        """No embedding call when there is nothing it could match.

        Asserted on the provider not being built, because the cost this avoids is
        a model round trip on every write that has no comparable records.
        """

        def _fail(*args, **kwargs):
            raise AssertionError("dependencies must not be built with nothing to compare")

        monkeypatch.setattr(runtime, "build_memory_recall_dependencies", _fail)
        finder = self._finder(tmp_path, database_identity)
        assert finder is not None

        assert finder(text, allowed, threshold=0.8) is None

    @pytest.mark.parametrize(
        ("score", "threshold", "expected_match"),
        [(0.90, 0.85, True), (0.85, 0.85, True), (0.84, 0.85, False)],
        ids=["above", "exactly_at", "below"],
    )
    def test_the_threshold_is_inclusive_at_the_boundary(
        self,
        tmp_path,
        database_identity,
        monkeypatch,
        score: float,
        threshold: float,
        expected_match: bool,
    ) -> None:
        """RUN-19 — both sides of the boundary, plus the boundary itself.

        The equal case is the one that decides whether a restatement scoring
        exactly at the threshold is treated as the same memory or stored twice,
        and it is the case a `>` written for `>=` gets wrong.
        """

        target = uuid4()

        class Hit:
            memory_id = target

            def __init__(self, value: float) -> None:
                self.score = value

        class Provider:
            def embed(self, text: str) -> list[float]:
                return [0.1] * 8

        class Index:
            def search(self, vector, owner_id, limit):
                return [Hit(score)]

        monkeypatch.setattr(
            runtime,
            "build_memory_recall_dependencies",
            lambda *a, **k: runtime.MemoryRecallDependencies(
                semantic_provider=Provider(), vector_index=Index()
            ),
        )
        finder = self._finder(tmp_path, database_identity)
        assert finder is not None

        result = finder("a restated preference", frozenset({target}), threshold=threshold)

        assert (result == target) is expected_match

    def test_a_hit_outside_the_allowed_set_is_ignored(
        self, tmp_path, database_identity, monkeypatch
    ) -> None:
        """The index may return anything; only comparable records may match."""

        class Hit:
            memory_id = uuid4()
            score = 0.99

        class Provider:
            def embed(self, text: str) -> list[float]:
                return [0.1] * 8

        class Index:
            def search(self, vector, owner_id, limit):
                return [Hit()]

        monkeypatch.setattr(
            runtime,
            "build_memory_recall_dependencies",
            lambda *a, **k: runtime.MemoryRecallDependencies(
                semantic_provider=Provider(), vector_index=Index()
            ),
        )
        finder = self._finder(tmp_path, database_identity)
        assert finder is not None

        assert finder("text", frozenset({uuid4()}), threshold=0.5) is None

    def test_an_embedding_failure_never_blocks_the_write(
        self, tmp_path, database_identity, monkeypatch
    ) -> None:
        """A duplicate check that fails must degrade to "not a duplicate"."""

        class Provider:
            def embed(self, text: str) -> list[float]:
                raise RuntimeError("embedding timed out")

        monkeypatch.setattr(
            runtime,
            "build_memory_recall_dependencies",
            lambda *a, **k: runtime.MemoryRecallDependencies(
                semantic_provider=Provider(), vector_index=object()
            ),
        )
        finder = self._finder(tmp_path, database_identity)
        assert finder is not None

        assert finder("text", frozenset({uuid4()}), threshold=0.5) is None


class TestDrainMemoryOutbox:
    def _drain(self, engine: Engine, database_identity: str, **flag_overrides) -> int:
        flag_values = {"semantic_recall_enabled": False}
        flag_values.update(flag_overrides)
        return runtime.drain_memory_outbox(
            engine,
            owner_id=OWNER_ID,
            database_identity=database_identity,
            flags=MemorySettings(**flag_values),
            settings=get_settings(),
        )

    def test_pending_events_are_processed_and_counted(
        self, engine: Engine, database_identity: str
    ) -> None:
        """RUN-20 — the regression that left the derived tables permanently empty.

        Writing a memory only enqueues the indexing work. With nothing draining
        the queue, `memory_fts_documents` stayed empty and recall degraded to
        literal word overlap — which is why "V60 dripper" was unreachable from
        "what tools do i use to make coffee".
        """

        record_id = factories.insert_record(engine, display_text="V60 dripper")
        _enqueue(engine, record_id)

        completed = self._drain(engine, database_identity)

        assert completed >= 1

    def test_draining_an_empty_queue_returns_zero(
        self, engine: Engine, database_identity: str
    ) -> None:
        assert self._drain(engine, database_identity) == 0

    def test_a_disabled_worker_is_a_no_op(
        self, engine: Engine, database_identity: str
    ) -> None:
        """RUN-21 — nothing wired means nothing drained, and no error either."""

        record_id = factories.insert_record(engine, display_text="V60 dripper")
        _enqueue(engine, record_id)

        completed = self._drain(engine, database_identity, fts_index_enabled=False)

        assert completed == 0
        with engine.begin() as connection:
            remaining = connection.execute(select(MemoryOutbox.id)).fetchall()
        assert remaining, "a disabled worker must leave the work queued, not discard it"

    def test_a_processing_failure_propagates_out_of_drain(
        self, engine: Engine, database_identity: str, monkeypatch
    ) -> None:
        """RUN-22 — corrected: the guarantee is at the call site, not in here.

        `drain_memory_outbox`'s docstring says it "never raises into the caller".
        It has no `try`/`except` at all — a failure from `lease_batch` or
        `process_batch` propagates. The property the docstring describes is real,
        but it is implemented one layer up: `NeoChatService._build_memory_indexes`
        wraps the call and logs `memory_index_build_failed`, so an indexing
        failure never loses a memory that was stored correctly.

        Pinned as it is rather than as documented, because the difference matters
        for the next caller: anyone reading that docstring and calling this
        directly inherits a promise the function does not keep. See
        `decisions.md` 52.
        """

        record_id = factories.insert_record(engine, display_text="V60 dripper")
        _enqueue(engine, record_id)

        class Exploding:
            def lease_batch(self, **kwargs):
                raise RuntimeError("database is locked")

        monkeypatch.setattr(runtime, "MemoryOutboxProcessor", lambda *a, **k: Exploding())

        with pytest.raises(RuntimeError, match="database is locked"):
            self._drain(engine, database_identity)

    def test_the_chat_call_site_is_the_one_that_swallows_it(self) -> None:
        """Where the guarantee actually lives, asserted rather than assumed.

        Reading the source is not evidence the handler is still there, and this
        is the only thing standing between a locked database and a lost memory.
        """

        import inspect

        from app.services.chat import NeoChatService

        source = inspect.getsource(NeoChatService._build_memory_indexes)

        assert "drain_memory_outbox" in source
        assert "except Exception" in source
        assert "memory_index_build_failed" in source

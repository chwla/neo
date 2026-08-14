"""Shared fixtures for the memory-layer suite.

Tests run against a real SQLite file rather than a mock or an in-memory URL.
The memory schema does much of the safety work itself — UUID-shape checks,
payload-shape-by-sensitivity checks, partial unique indexes that stop two active
records occupying one exclusive slot — and none of that is exercised unless a
real database gets the chance to reject a bad row.  A file-backed database also
keeps WAL and FTS5 behaving the way they do in the running app.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.memory_migrations import upgrade_memory
from app.services.memory.local_crypto import LocalMemoryCrypto

OWNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER_ID = "22222222-2222-4222-8222-222222222222"
PROFILE_ID = "profile-1"
CRYPTO_SEED = b"neo-memory-test-seed-value-32bytes!!"

# A fixed instant so every expiry, freshness, tombstone, backoff, and lease
# assertion is reproducible.  Wall-clock time in these tests means either
# sleeping or writing something that passes today and fails at midnight.
FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


class NetworkBlockedInTests(RuntimeError):
    """Raised when a test opens a socket the memory suite should never need."""


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_network: this test genuinely needs a socket; states it in its own source",
    )


@pytest.fixture(autouse=True)
def block_network(request) -> Iterator[None]:
    """Fail any test that opens a socket, and name it.

    Every external collaborator in this layer has a double, so a socket here is
    always a mistake — but a quiet one.  Both sessions working on this suite hit
    the same failure independently: a fixture built a real runtime, which probed
    a live Ollama endpoint with a 300-second warmup timeout.  It looked fine
    locally because a running service answers fast, so the dependency was
    invisible precisely while it was harmless.  On a machine without Ollama the
    suite went from 72 seconds to still-running at ten minutes, at 0% CPU.

    **Attempts are recorded before the raise, and the test fails at teardown
    even if it passed.**  Raising alone is not enough: code that catches
    connection errors keeps the test green, and two health tests were doing
    exactly that — passing via the failure path, with a round trip as the only
    observable difference.  Recording is what turns "something connects" into
    "these two tests, by name".

    A test that genuinely needs a socket marks itself ``allow_network``, so the
    exception is visible in its own source rather than in nobody noticing.
    """

    if request.node.get_closest_marker("allow_network"):
        yield
        return

    attempts: list[object] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        attempts.append(address)
        raise NetworkBlockedInTests(f"network access attempted to {address!r}")

    def guarded_connect_ex(self, address):
        # connect_ex returns an error code rather than raising, so it would
        # otherwise slip past a guard that only patches connect.
        attempts.append(address)
        raise NetworkBlockedInTests(f"network access attempted to {address!r}")

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex

    if attempts:
        pytest.fail(
            f"{request.node.nodeid} attempted {len(attempts)} network "
            f"connection(s): {attempts}. Every external collaborator in this "
            "layer has a double; add one, or mark the test 'allow_network' if "
            "it genuinely needs a socket."
        )


def _build_engine(path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - event hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        # Off by default in SQLite.  Without it every cross-owner foreign-key
        # test would pass for the wrong reason.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.fixture
def crypto() -> LocalMemoryCrypto:
    """Deterministic key material, so keyed fingerprints are reproducible."""

    return LocalMemoryCrypto(seed=CRYPTO_SEED)


@pytest.fixture
def owner_id() -> str:
    return OWNER_ID


@pytest.fixture
def other_owner_id() -> str:
    return OTHER_OWNER_ID


@pytest.fixture
def database_identity(tmp_path) -> str:
    return str(tmp_path / "memory.db")


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    """A migrated, owner-bound memory database."""

    path = tmp_path / "memory.db"
    built = _build_engine(str(path))
    upgrade_memory(built, owner_id=OWNER_ID, database_identity=str(path))
    try:
        yield built
    finally:
        built.dispose()


@pytest.fixture
def other_engine(tmp_path) -> Iterator[Engine]:
    """A second profile's database, for every cross-owner isolation test."""

    path = tmp_path / "other-memory.db"
    built = _build_engine(str(path))
    upgrade_memory(built, owner_id=OTHER_OWNER_ID, database_identity=str(path))
    try:
        yield built
    finally:
        built.dispose()


@pytest.fixture
def unmigrated_engine(tmp_path) -> Iterator[Engine]:
    """An empty database, for migration and binding tests."""

    path = tmp_path / "fresh.db"
    built = _build_engine(str(path))
    try:
        yield built
    finally:
        built.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def session(session_factory: sessionmaker) -> Iterator[Session]:
    with session_factory() as opened:
        yield opened


@pytest.fixture
def mutation_service(engine: Engine, crypto: LocalMemoryCrypto, tmp_path):
    """A real mutation service against the migrated database.

    ``LocalMemoryCrypto`` satisfies all four provider protocols the service
    needs — payload encryption, keyed fingerprints, tombstone HMACs, and key
    version resolution — so one deterministic object wires the whole thing.
    """

    from app.services.memory.mutations import MemoryMutationService

    return MemoryMutationService(
        engine,
        owner_id=OWNER_ID,
        database_identity=str(tmp_path / "memory.db"),
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
    )


@pytest.fixture
def mutation_service_factory(engine: Engine, crypto: LocalMemoryCrypto, tmp_path):
    """Build a mutation service with non-default retry or failure injection."""

    from app.services.memory.mutations import MemoryMutationService

    def _build(**overrides):
        return MemoryMutationService(
            engine,
            owner_id=OWNER_ID,
            database_identity=str(tmp_path / "memory.db"),
            payload_provider=crypto,
            fingerprint_provider=crypto,
            tombstone_provider=crypto,
            key_versions=crypto,
            **overrides,
        )

    return _build


@pytest.fixture
def memory_settings():
    """Default runtime flags, with the model-backed paths left off."""

    from app.services.memory.settings import MemorySettings

    return MemorySettings()


@pytest.fixture
def recall_service(session, engine: Engine, tmp_path, memory_settings):
    """A recall service reading the migrated database, lexical path only."""

    from app.repositories.memory import MemoryRepository
    from app.services.memory.recall import CanonicalRecallService

    repository = MemoryRepository(
        session, owner_id=OWNER_ID, database_identity=str(tmp_path / "memory.db")
    )
    return CanonicalRecallService(repository, flags=memory_settings)


@pytest.fixture
def execution_context(tmp_path):
    """The owner/profile binding every coordinator call is checked against."""

    from app.services.memory.coordinator import MemoryExecutionContext

    # The identity is a logical binding string, not a path: the coordinator
    # requires the ``account-profile:``/``guest-profile:`` prefix so a permanent
    # profile can never be served from a guest database or the reverse.
    path = tmp_path / "profile.db"
    return MemoryExecutionContext(
        owner_id=OWNER_ID,
        database_identity=f"account-profile:{PROFILE_ID}",
        database_url=f"sqlite:///{path}",
        profile_id=PROFILE_ID,
    )


@pytest.fixture
def adapter_context(execution_context):
    """A chat turn's worth of context: who is acting, and on which message."""

    from app.services.memory.adapters import MemoryAdapterContext
    from app.services.memory.contracts import ActorKind, SourceKind

    return MemoryAdapterContext(
        execution=execution_context,
        actor_kind=ActorKind.USER,
        actor_id=OWNER_ID,
        source_kind=SourceKind.CHAT_MESSAGE,
        request_id="request-1",
        session_id="s1",
        conversation_id="c1",
        message_id="m1",
    )


@pytest.fixture
def mutation_coordinator(crypto: LocalMemoryCrypto, memory_settings):
    """The real coordinator, which builds and migrates its own database.

    Deliberately *not* handed the ``engine`` fixture: the coordinator opening,
    migrating and disposing an engine per call is the behaviour under test, and
    a pre-built engine would hide a migration or binding failure.
    """

    from app.services.memory.coordinator import MemoryMutationCoordinator

    return MemoryMutationCoordinator(
        flags=memory_settings,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
    )


@pytest.fixture
def coordinator_with_flags(crypto: LocalMemoryCrypto, memory_settings):
    """Build the whole adapter chain with one runtime flag changed.

    ``MemorySettings`` is a frozen dataclass — deliberately, so nothing can flip
    a memory setting mid-turn — so a test that needs a different flag has to
    rebuild rather than mutate.
    """

    from dataclasses import replace as dataclass_replace

    from app.services.memory.adapters import ChatMemoryAdapter
    from app.services.memory.coordinator import MemoryMutationCoordinator
    from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator

    def _build(model=None, **flags):
        settings = dataclass_replace(memory_settings, **flags)
        coordinator = MemoryMutationCoordinator(
            flags=settings,
            payload_provider=crypto,
            fingerprint_provider=crypto,
            tombstone_provider=crypto,
            key_versions=crypto,
        )
        return MemoryExtractionCoordinator(ChatMemoryAdapter(coordinator), model=model)

    return _build


@pytest.fixture
def chat_adapter(mutation_coordinator):
    from app.services.memory.adapters import ChatMemoryAdapter

    return ChatMemoryAdapter(mutation_coordinator)


@pytest.fixture
def extraction_coordinator_factory(chat_adapter):
    """Build an extraction coordinator with a scripted model.

    The model is the only faked collaborator; the adapter underneath is the real
    one writing to a real database, so an accepted candidate really is persisted
    and really is visible to the next call.
    """

    from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator

    def _build(model=None, **overrides):
        return MemoryExtractionCoordinator(chat_adapter, model=model, **overrides)

    return _build


@dataclass
class FrozenClock:
    """A clock that only moves when a test moves it."""

    now: datetime = FROZEN_NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()

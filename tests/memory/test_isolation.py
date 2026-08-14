"""Tier 7 — owner isolation, asserted at every layer (plan section ISO).

Profiles get separate database files, so the obvious cross-owner test — point
one profile's service at another's database — is prevented by construction and
proves little. The threat these tests model instead is a **foreign row inside
your own database**: a bug, a restored backup, a merged file, or the
wrong-owner index metadata that RCL-47 already showed can exist.

Every layer therefore gets the same shape: seed a row owned by someone else in
this owner's database, then assert the layer refuses to see, return, or touch
it. A layer that filters by owner in SQL passes trivially; one that trusts its
input does not — and which is which is exactly what these record.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.repositories.memory import MemoryRepository, MemoryRepositoryError
from app.services.memory.contracts import (
    MemoryErrorCode,
    MemoryLifecycleState,
    MemoryOutcome,
    Sensitivity,
)
from app.services.memory.crypto import build_associated_data
from app.services.memory.local_crypto import LocalMemoryCrypto
from app.services.memory.mutations import MemoryMutationService
from app.services.memory.normalization import canonical_fingerprint
from app.services.memory.taxonomy import MemoryType
from app.services.memory.tombstones import tombstone_digest
from tests.memory import factories
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID

VALUE = "improve at urban sketching"
ALL_STATUSES = tuple(MemoryLifecycleState)


@pytest.fixture
def foreign_record(engine: Engine) -> str:
    """A row belonging to another owner, sitting in this owner's database.

    This is the state every assertion below is about. It is reachable in
    production through an index row carrying the wrong owner (RCL-47), a
    restored backup, or any write path that stops filtering.
    """

    return factories.insert_record(engine, owner=OTHER_OWNER_ID, display_text="their secret")


class TestBindings:
    def test_two_profiles_never_share_a_binding(
        self, engine: Engine, other_engine: Engine
    ) -> None:
        """ISO-01"""

        with engine.begin() as connection:
            mine = connection.execute(
                text("SELECT owner_id, database_identity FROM memory_owner_bindings")
            ).all()
        with other_engine.begin() as connection:
            theirs = connection.execute(
                text("SELECT owner_id, database_identity FROM memory_owner_bindings")
            ).all()

        assert mine and theirs
        assert {row[0] for row in mine}.isdisjoint({row[0] for row in theirs})
        assert {row[1] for row in mine}.isdisjoint({row[1] for row in theirs})

    def test_a_repository_refuses_a_database_bound_to_another_owner(
        self, session, other_engine: Engine, tmp_path
    ) -> None:
        """The binding is checked at construction, before any query runs."""

        with pytest.raises(MemoryRepositoryError):
            MemoryRepository(
                session,
                owner_id=OTHER_OWNER_ID,
                database_identity=str(tmp_path / "memory.db"),
            )


class TestRepository:
    def _repository(self, session, tmp_path) -> MemoryRepository:
        return MemoryRepository(
            session, owner_id=OWNER_ID, database_identity=str(tmp_path / "memory.db")
        )

    def test_get_record_does_not_return_a_foreign_row(
        self, session, tmp_path, foreign_record: str
    ) -> None:
        """ISO-02 — asked for the id directly, by an owner who does not own it."""

        repository = self._repository(session, tmp_path)

        assert repository.get_record(foreign_record, statuses=ALL_STATUSES) is None

    def test_recall_eligibility_does_not_return_a_foreign_row(
        self, session, tmp_path, foreign_record: str
    ) -> None:
        from tests.memory.conftest import FROZEN_NOW

        repository = self._repository(session, tmp_path)

        assert repository.get_recall_eligible_by_id(foreign_record, now=FROZEN_NOW) is None

    def test_listing_excludes_foreign_rows(
        self, session, tmp_path, engine: Engine, foreign_record: str
    ) -> None:
        """The positive control matters here: the listing must not be empty for both."""

        from tests.memory.conftest import FROZEN_NOW

        mine = factories.insert_record(engine, display_text=VALUE)
        repository = self._repository(session, tmp_path)

        listed = {str(record.id) for record in repository.list_recall_eligible(now=FROZEN_NOW)}

        assert mine in listed
        assert foreign_record not in listed

    def test_the_historical_lookup_is_also_owner_scoped(
        self, session, tmp_path, foreign_record: str
    ) -> None:
        """`get_owner_record_any_lifecycle` sees forgotten rows, so it needs the same guard.

        A lookup that deliberately ignores lifecycle is the one most likely to
        forget to filter on owner as well.
        """

        repository = self._repository(session, tmp_path)

        assert repository.get_owner_record_any_lifecycle(foreign_record) is None


class TestMutations:
    @pytest.mark.parametrize(
        "command",
        ["forget", "erase", "archive"],
    )
    def test_no_lifecycle_command_touches_a_foreign_record(
        self,
        mutation_service: MemoryMutationService,
        engine: Engine,
        foreign_record: str,
        command: str,
    ) -> None:
        """ISO-03 — parametrised, because each command has its own planning path.

        Testing one and assuming the rest would miss exactly the command whose
        author forgot the guard.
        """

        from uuid import UUID

        builder = {
            "forget": factories.forget_command,
            "erase": factories.erase_command,
            "archive": factories.archive_command,
        }[command]

        result = mutation_service.execute(builder(memory_id=UUID(foreign_record)))

        # Exact, not a set of plausible refusals: all three fail identically, and
        # a set would have hidden one of them starting to succeed as NEEDS_REVIEW.
        assert result.outcome is MemoryOutcome.FAILED
        with engine.begin() as connection:
            status = connection.execute(
                text("SELECT status FROM memory_records WHERE id = :i"),
                {"i": foreign_record},
            ).scalar()
        assert status == "active", "a foreign record must be left exactly as it was"

    def test_the_active_listing_never_includes_a_foreign_record(
        self, mutation_service: MemoryMutationService, foreign_record: str
    ) -> None:
        active = mutation_service.list_active_records()

        assert foreign_record not in {str(item.memory_id) for item in active}

    def test_a_command_carrying_a_foreign_owner_is_refused(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """The service is bound to one owner; a command naming another is invalid."""

        result = mutation_service.execute(factories.create_command(owner=OTHER_OWNER_ID))

        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code is MemoryErrorCode.OWNER_MISMATCH


class TestRecall:
    def test_recall_never_returns_a_foreign_record(
        self, recall_service, tmp_path, engine: Engine, foreign_record: str
    ) -> None:
        """ISO-04 — including by deterministic id, which bypasses scoring entirely."""

        from app.services.memory.queries import MemoryQueryContext, RecallMode, RecallQuery
        from tests.memory.conftest import FROZEN_NOW

        context = MemoryQueryContext(
            owner_id=OWNER_ID,
            database_identity=str(tmp_path / "memory.db"),
            profile_id="profile-1",
            request_id="request-1",
            current_time=FROZEN_NOW,
            mode=RecallMode.DETERMINISTIC,
        )

        result = recall_service.recall(
            RecallQuery(context=context, canonical_id=foreign_record)
        )

        assert result.items == ()

    def test_a_lexical_query_does_not_surface_a_foreign_record(
        self, recall_service, tmp_path, foreign_record: str
    ) -> None:
        from app.services.memory.queries import MemoryQueryContext, RecallMode, RecallQuery
        from tests.memory.conftest import FROZEN_NOW

        context = MemoryQueryContext(
            owner_id=OWNER_ID,
            database_identity=str(tmp_path / "memory.db"),
            profile_id="profile-1",
            request_id="request-1",
            current_time=FROZEN_NOW,
            mode=RecallMode.SCOPED_LEXICAL,
        )

        result = recall_service.recall(RecallQuery(context=context, text="their secret"))

        assert result.canonical_ids == ()


class TestDerivedIndexes:
    def test_neither_index_returns_a_foreign_row(self, engine: Engine) -> None:
        """ISO-05 — both indexes filter in SQL, which is why this passes cheaply.

        Asserted anyway: the guarantee is currently structural, and a future
        change to either search method is exactly where it would be lost.
        """

        from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex

        fts = SqliteMemoryFtsIndex(engine)
        vector = SqliteMemoryVectorIndex(engine)

        assert list(fts.search(OTHER_OWNER_ID, "secret", 10)) == []
        assert list(vector.search([0.1] * 8, OTHER_OWNER_ID, 10)) == []


class TestFingerprintsAndTombstones:
    def _fingerprint(self, owner: str, sensitivity: Sensitivity, crypto) -> str:
        return canonical_fingerprint(
            owner_id=owner,
            subject_key="user",
            memory_type=MemoryType.GOAL,
            domain_key="global",
            slot_key="goal:global:current_primary_goal",
            canonical_value=VALUE,
            sensitivity=sensitivity,
            scope_type="global",
            scope_project_id=None,
            keyed_provider=crypto,
        )

    def test_a_sensitive_fingerprint_is_owner_bound(self, crypto: LocalMemoryCrypto) -> None:
        """ISO-09a — a keyed fingerprint cannot be used to probe another owner.

        For SENSITIVE facts the digest is HMAC'd with the owner, so possessing
        one profile's fingerprint says nothing about whether another profile
        stored the same fact.
        """

        mine = self._fingerprint(OWNER_ID, Sensitivity.SENSITIVE, crypto)
        theirs = self._fingerprint(OTHER_OWNER_ID, Sensitivity.SENSITIVE, crypto)

        assert mine.startswith("keyed:")
        assert mine != theirs

    def test_a_normal_fingerprint_is_not_owner_bound_and_that_is_the_point(
        self, crypto: LocalMemoryCrypto
    ) -> None:
        """ISO-09b — the cross-cutting clarification worth having written down.

        NRM-15 pins that a NORMAL fingerprint ignores the owner: two profiles
        storing the same ordinary fact produce the *same* digest. That is not a
        leak, because isolation for normal records comes from the `owner_id`
        column and the unique index being owner-scoped (SCH-16) — not from the
        digest.

        Stating it here is the point of a cross-cutting tier: the two halves live
        in different files and neither one alone tells you where isolation
        actually comes from.
        """

        mine = self._fingerprint(OWNER_ID, Sensitivity.NORMAL, crypto)
        theirs = self._fingerprint(OTHER_OWNER_ID, Sensitivity.NORMAL, crypto)

        assert mine.startswith("sha256:")
        assert mine == theirs

    def test_a_tombstone_never_matches_across_owners(self, crypto: LocalMemoryCrypto) -> None:
        """ISO-09c — otherwise one owner's forget could block another's write."""

        fingerprint = self._fingerprint(OWNER_ID, Sensitivity.NORMAL, crypto)

        mine = tombstone_digest(fingerprint, owner_id=OWNER_ID, provider=crypto)
        theirs = tombstone_digest(fingerprint, owner_id=OTHER_OWNER_ID, provider=crypto)

        assert mine.digest != theirs.digest

    def test_encryption_aad_binds_the_owner(self, crypto: LocalMemoryCrypto) -> None:
        """ISO-09d — ciphertext copied between profiles must not decrypt.

        The associated data binds the owner, so a row lifted from another
        database fails authentication rather than silently decrypting.
        """

        common = {
            "memory_type": "goal",
            "domain_key": "global",
            "slot_key": "goal:global:current_primary_goal",
            "record_id": "33333333-3333-4333-8333-333333333333",
            "schema_version": 1,
            "key_version": "local-memory-v1",
            "purpose": "canonical",
        }
        mine = build_associated_data(owner_id=OWNER_ID, **common)
        theirs = build_associated_data(owner_id=OTHER_OWNER_ID, **common)

        assert mine != theirs

        envelope = crypto.encrypt(VALUE.encode(), associated_data=mine)
        with pytest.raises(Exception):
            crypto.decrypt(envelope, associated_data=theirs)


class TestGuestProfiles:
    def test_a_guest_profile_is_stored_separately_from_accounts(self) -> None:
        """ISO-10 — a guest's data must not land in the registered profile tree.

        The identity prefix is what the coordinator checks, so a guest database
        can never be served to an account profile even if the paths were
        confused.
        """

        from app.services.profile_accounts import database_identity_for_profile

        guest = database_identity_for_profile("p1", guest=True)
        account = database_identity_for_profile("p1", guest=False)

        assert guest.startswith("guest-profile:")
        assert account.startswith("account-profile:")
        assert guest != account

    def test_the_same_profile_id_yields_different_identities(self) -> None:
        """The prefix is load-bearing: ids alone would collide across the two trees."""

        from app.services.profile_accounts import database_identity_for_profile

        assert database_identity_for_profile("shared-id", guest=True) != (
            database_identity_for_profile("shared-id", guest=False)
        )


class TestAsyncLayers:
    """ISO-06 / ISO-07 — the layers that run without a request behind them.

    The outbox worker and the maintenance sweep both operate on whatever they
    find in the database, with no caller supplying a scope. That makes them the
    two places where a foreign row is most likely to be picked up by accident,
    and the two where nobody is watching when it happens.
    """

    def _enqueue(self, engine: Engine, memory_id: str, owner: str) -> str:
        from uuid import uuid4

        from sqlalchemy import insert

        from app.models.memory import MemoryOutbox
        from tests.memory.conftest import FROZEN_NOW

        event_id = str(uuid4())
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT canonical_fingerprint, revision FROM memory_records WHERE id = :i"
                ),
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

    def test_the_outbox_never_leases_a_foreign_event(
        self, engine: Engine, tmp_path, foreign_record: str
    ) -> None:
        """ISO-06 — a worker bound to one owner must not drain another's queue."""

        from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
        from app.services.memory.outbox import MemoryOutboxProcessor
        from tests.memory.doubles import FakeEmbeddingProvider

        mine = factories.insert_record(engine, display_text=VALUE)
        my_event = self._enqueue(engine, mine, OWNER_ID)
        their_event = self._enqueue(engine, foreign_record, OTHER_OWNER_ID)

        processor = MemoryOutboxProcessor(
            engine,
            owner_id=OWNER_ID,
            database_identity=str(tmp_path / "memory.db"),
            fts_index=SqliteMemoryFtsIndex(engine),
            vector_index=SqliteMemoryVectorIndex(engine),
            embedding_provider=FakeEmbeddingProvider(),
        )

        batch = processor.lease_batch(worker_id="isolation-worker")
        leased = {str(lease.event_id) for lease in batch.leases}

        assert my_event in leased, "the owner's own event must still be processed"
        assert their_event not in leased

    def test_maintenance_enumerates_only_this_owners_records(
        self, session, tmp_path, engine: Engine, foreign_record: str
    ) -> None:
        """ISO-07 — asserted at the enumeration maintenance actually reads through.

        `rebuild_owner` is the widest-reaching operation in the system: it clears
        both indexes and reconstructs them from canonical. It builds that set via
        `repository.list_index_candidates`, which filters by owner in SQL — so the
        guarantee is structural rather than a check maintenance performs.

        Asserted here rather than through `rebuild_owner` itself because the
        rebuild has its own eligibility rules, and a test that ran it and found
        nothing indexed would report isolation while proving only that the
        rebuild did nothing. Testing the enumeration keeps the positive control
        meaningful.
        """

        from tests.memory.conftest import FROZEN_NOW

        mine = factories.insert_record(engine, display_text=VALUE)
        repository = MemoryRepository(
            session, owner_id=OWNER_ID, database_identity=str(tmp_path / "memory.db")
        )

        candidates = {
            str(record.id) for record in repository.list_index_candidates(now=FROZEN_NOW)
        }

        assert mine in candidates
        assert foreign_record not in candidates

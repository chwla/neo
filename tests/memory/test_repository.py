"""Tier 3 — the repository (plan section REP).

Every read and write the memory layer performs goes through this class, and its
job is narrower than it looks: keep one owner's data separate from every other
owner's, and refuse anything the layers above it should never have asked for.

The isolation tests here matter more than they read.  A single query that
forgets its `owner_id` filter defeats the per-profile databases, the owner-bound
foreign keys, and the owner-binding table all at once, and the symptom is one
person's memories appearing in another person's chat.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.memory import MemoryOutbox, MemoryRelation, MemorySource, MemoryTombstone
from app.repositories.memory import (
    MemoryBindingError,
    MemoryNotFoundError,
    MemoryProhibitedContentError,
    MemoryRepository,
    MemoryRevisionConflict,
)
from app.services.memory.contracts import MemoryLifecycleState
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory import factories
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID

ACTIVE = (MemoryLifecycleState.ACTIVE,)
ALL_STATUSES = tuple(MemoryLifecycleState)
UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture
def identity(engine: Engine) -> str:
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT database_identity FROM memory_owner_bindings")
        ).scalar_one()


@pytest.fixture
def repo(session: Session, identity: str) -> MemoryRepository:
    return MemoryRepository(session, owner_id=OWNER_ID, database_identity=identity)


class TestBinding:
    def test_an_unmigrated_database_is_refused(self, unmigrated_engine: Engine) -> None:
        """REP-01 — no schema means no binding to trust."""

        maker = sessionmaker(bind=unmigrated_engine, future=True)
        with maker() as opened, pytest.raises(MemoryBindingError):
            MemoryRepository(opened, owner_id=OWNER_ID, database_identity="whatever")

    def test_a_different_owner_is_refused(self, session: Session, identity: str) -> None:
        """REP-02 — opening someone else's database must fail immediately."""

        with pytest.raises(MemoryBindingError, match="binding_mismatch"):
            MemoryRepository(session, owner_id=OTHER_OWNER_ID, database_identity=identity)

    def test_a_different_database_identity_is_refused(self, session: Session) -> None:
        """REP-03 — a moved or copied file is not the file it claims to be."""

        with pytest.raises(MemoryBindingError, match="binding_mismatch"):
            MemoryRepository(session, owner_id=OWNER_ID, database_identity="/elsewhere/neo.db")

    @pytest.mark.parametrize("identity_value", ["", "   "])
    def test_a_blank_database_identity_is_refused(
        self, session: Session, identity_value: str
    ) -> None:
        """REP-03b"""

        with pytest.raises(MemoryBindingError, match="database_identity_required"):
            MemoryRepository(session, owner_id=OWNER_ID, database_identity=identity_value)

    def test_a_malformed_owner_is_refused(self, session: Session, identity: str) -> None:
        """REP-03c"""

        with pytest.raises(ValueError, match="canonical_uuid_required"):
            MemoryRepository(session, owner_id="not-a-uuid", database_identity=identity)

    def test_a_correct_binding_opens(self, repo: MemoryRepository) -> None:
        """REP-01b"""

        assert repo.owner_id == OWNER_ID


class TestOwnershipGuards:
    def test_adding_another_owners_record_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-04 / REP-23 — the guard above the foreign key."""

        from app.models.memory import MemoryRecord

        operation_id = factories.insert_operation(engine)
        values = factories.record_values(owner=OTHER_OWNER_ID, operation_id=operation_id)
        with pytest.raises(MemoryNotFoundError, match="owner_bound_reference_not_found"):
            repo.add_record(MemoryRecord(**values))

    def test_an_entity_with_a_malformed_owner_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-04b"""

        from app.models.memory import MemoryRecord

        operation_id = factories.insert_operation(engine)
        values = factories.record_values(operation_id=operation_id)
        values["owner_id"] = "not-a-uuid"
        with pytest.raises(MemoryBindingError, match="entity_owner_id_invalid"):
            repo.add_record(MemoryRecord(**values))

    def test_get_record_returns_none_for_an_unknown_id(self, repo: MemoryRepository) -> None:
        """REP-05 — a miss is not an error."""

        assert repo.get_record(UUID_A, statuses=ALL_STATUSES) is None

    def test_get_record_never_crosses_owners(
        self, engine: Engine, other_engine: Engine, session: Session, identity: str
    ) -> None:
        """REP-06 — the id exists, but not for this owner."""

        foreign_id = factories.insert_record(other_engine, owner=OTHER_OWNER_ID)
        repo = MemoryRepository(session, owner_id=OWNER_ID, database_identity=identity)
        assert repo.get_record(foreign_id, statuses=ALL_STATUSES) is None

    def test_get_owner_record_any_lifecycle_is_owner_scoped(
        self, other_engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-34b — even the validation read used for untrusted hits."""

        foreign_id = factories.insert_record(other_engine, owner=OTHER_OWNER_ID)
        assert repo.get_owner_record_any_lifecycle(foreign_id) is None


class TestProhibitedMaterial:
    def test_a_prohibited_record_payload_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-07 — the last stop before a secret reaches disk."""

        from app.models.memory import MemoryRecord

        operation_id = factories.insert_operation(engine)
        values = factories.record_values(
            operation_id=operation_id,
            canonical_payload="my password is hunter2",
            display_text="my password is hunter2",
        )
        with pytest.raises(MemoryProhibitedContentError):
            repo.add_record(MemoryRecord(**values))

    def test_a_prohibited_operation_payload_is_refused(self, repo: MemoryRepository) -> None:
        """REP-07b"""

        from app.models.memory import MemoryOperation

        values = factories.operation_values(
            normalized_command_json={"note": "my api key is abcd1234efgh"}
        )
        with pytest.raises(MemoryProhibitedContentError):
            repo.add_operation(MemoryOperation(**values))

    def test_a_uuid_in_the_payload_does_not_trip_the_scanner(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-07c — UUIDs are masked first, or they read as card numbers.

        A bare UUID is a long run of hex digits; without the masking step the
        prohibited-content scanner would occasionally refuse perfectly ordinary
        rows because their identifiers looked like payment data.
        """

        from app.models.memory import MemoryRecord

        operation_id = factories.insert_operation(engine)
        values = factories.record_values(
            operation_id=operation_id,
            canonical_payload={"related": UUID_A, "other": UUID_B},
            display_text="a note about two other memories",
        )
        assert repo.add_record(MemoryRecord(**values))


class TestStatusFiltering:
    def test_an_empty_status_filter_is_refused(self, repo: MemoryRepository) -> None:
        """REP-08a — callers must say which lifecycle states they mean."""

        with pytest.raises(ValueError, match="explicit_status_filter_required"):
            repo.get_record(UUID_A, statuses=())

    @pytest.mark.parametrize(
        "status", ["haunted", "active'; DROP TABLE memory_records; --", "ACTIVE"]
    )
    def test_an_unknown_status_is_refused(self, repo: MemoryRepository, status: str) -> None:
        """REP-08b — no unvalidated string reaches the query."""

        with pytest.raises(ValueError, match="invalid_memory_status_filter"):
            repo.get_record(UUID_A, statuses=(status,))

    def test_a_record_is_found_only_under_its_own_status(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-08c"""

        record_id = factories.insert_record(engine, status=MemoryLifecycleState.ARCHIVED)
        assert repo.get_record(record_id, statuses=ACTIVE) is None
        assert repo.get_record(record_id, statuses=(MemoryLifecycleState.ARCHIVED,))


class TestEligibility:
    def test_inactive_records_are_excluded(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-09"""

        factories.insert_record(engine, status=MemoryLifecycleState.FORGOTTEN)
        assert repo.list_recall_eligible(now=FROZEN_NOW) == []

    def test_expired_records_are_excluded(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-10"""

        factories.insert_record(engine, expires_at=FROZEN_NOW - timedelta(days=1))
        assert repo.list_recall_eligible(now=FROZEN_NOW) == []

    def test_a_record_expiring_later_is_still_eligible(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-10b"""

        factories.insert_record(engine, expires_at=FROZEN_NOW + timedelta(days=1))
        assert len(repo.list_recall_eligible(now=FROZEN_NOW)) == 1

    def test_expiry_is_exclusive_at_the_boundary(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-10c — expiring exactly now counts as expired."""

        factories.insert_record(engine, expires_at=FROZEN_NOW)
        assert repo.list_recall_eligible(now=FROZEN_NOW) == []

    def test_a_project_record_is_invisible_outside_its_project(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-11a — the guarantee that project notes stay in their project."""

        factories.insert_record(engine, scope_type="project", scope_project_id="alpha")
        assert repo.list_recall_eligible(now=FROZEN_NOW) == []
        assert repo.list_recall_eligible(now=FROZEN_NOW, project_id="beta") == []

    def test_a_project_record_is_visible_inside_its_project(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-11b"""

        factories.insert_record(engine, scope_type="project", scope_project_id="alpha")
        assert len(repo.list_recall_eligible(now=FROZEN_NOW, project_id="alpha")) == 1

    def test_a_global_record_is_visible_inside_a_project(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-11c — personal facts stay readable from anywhere."""

        factories.insert_record(engine)
        assert len(repo.list_recall_eligible(now=FROZEN_NOW, project_id="alpha")) == 1

    def test_memory_types_filter(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-11d"""

        factories.insert_record(engine, memory_type=MemoryType.GOAL)
        factories.insert_record(
            engine,
            memory_type=MemoryType.KNOWLEDGE,
            slot_key=f"knowledge:global:item:{UUID_B}",
        )
        found = repo.list_recall_eligible(now=FROZEN_NOW, memory_types=(MemoryType.GOAL,))
        assert [item.memory_type for item in found] == ["goal"]

    def test_domain_keys_filter(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-11e"""

        factories.insert_record(engine, domain_key="global")
        factories.insert_record(
            engine,
            domain_key="learning",
            slot_key=f"goal:learning:independent:{UUID_B}",
        )
        found = repo.list_recall_eligible(now=FROZEN_NOW, domain_keys=("learning",))
        assert [item.domain_key for item in found] == ["learning"]

    def test_eligible_records_never_cross_owners(
        self, other_engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-09b"""

        factories.insert_record(other_engine, owner=OTHER_OWNER_ID)
        assert repo.list_recall_eligible(now=FROZEN_NOW) == []


class TestFilterCounts:
    def test_counts_report_inactive_and_expired_separately(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-12 — these numbers become the recall diagnostic the user sees."""

        factories.insert_record(engine, status=MemoryLifecycleState.ARCHIVED)
        factories.insert_record(engine, status=MemoryLifecycleState.FORGOTTEN)
        factories.insert_record(engine, expires_at=FROZEN_NOW - timedelta(days=1))
        factories.insert_record(engine)

        inactive, expired = repo.recall_filter_counts(now=FROZEN_NOW)
        assert (inactive, expired) == (2, 1)
        assert len(repo.list_recall_eligible(now=FROZEN_NOW)) == 1

    def test_counts_are_owner_scoped(self, other_engine: Engine, repo: MemoryRepository) -> None:
        """REP-12b"""

        factories.insert_record(
            other_engine, owner=OTHER_OWNER_ID, status=MemoryLifecycleState.ARCHIVED
        )
        assert repo.recall_filter_counts(now=FROZEN_NOW) == (0, 0)


class TestLimits:
    @pytest.mark.parametrize("limit", [0, -1, 501])
    def test_an_out_of_range_recall_limit_is_refused(
        self, repo: MemoryRepository, limit: int
    ) -> None:
        """REP-13a"""

        with pytest.raises(ValueError, match="recall_candidate_limit_out_of_range"):
            repo.list_recall_eligible(now=FROZEN_NOW, limit=limit)

    def test_the_recall_limit_is_honoured(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-13b"""

        for index in range(5):
            factories.insert_record(
                engine, slot_key=f"goal:global:independent:{UUID_A[:-1]}{index}"
            )
        assert len(repo.list_recall_eligible(now=FROZEN_NOW, limit=3)) == 3

    @pytest.mark.parametrize("limit", [0, 1_002])
    def test_an_out_of_range_index_limit_is_refused(
        self, repo: MemoryRepository, limit: int
    ) -> None:
        """REP-33a"""

        with pytest.raises(ValueError, match="index_candidate_limit_out_of_range"):
            repo.list_index_candidates(now=FROZEN_NOW, limit=limit)

    @pytest.mark.parametrize("limit", [0, 1_001])
    def test_an_out_of_range_record_limit_is_refused(
        self, repo: MemoryRepository, limit: int
    ) -> None:
        """REP-13c"""

        with pytest.raises(ValueError, match="record_limit_out_of_range"):
            repo.list_records(statuses=ACTIVE, limit=limit)


class TestSlotLookups:
    def test_an_ineligible_record_is_not_returned_by_id(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-14"""

        record_id = factories.insert_record(engine, status=MemoryLifecycleState.ARCHIVED)
        assert repo.get_recall_eligible_by_id(record_id, now=FROZEN_NOW) is None

    def test_an_exact_slot_is_matched(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-15a"""

        factories.insert_record(
            engine,
            memory_type=MemoryType.PREFERENCE,
            slot_key="preference:global:verbosity",
            cardinality=Cardinality.EXCLUSIVE,
        )
        found = repo.find_recall_eligible_slot(
            now=FROZEN_NOW,
            memory_type=MemoryType.PREFERENCE,
            domain_key="global",
            slot_key="preference:global:verbosity",
        )
        assert found is not None

    def test_a_near_miss_slot_is_not_matched(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-15b — slots are exact keys, never prefixes."""

        factories.insert_record(
            engine,
            memory_type=MemoryType.PREFERENCE,
            slot_key="preference:global:verbosity",
            cardinality=Cardinality.EXCLUSIVE,
        )
        assert (
            repo.find_recall_eligible_slot(
                now=FROZEN_NOW,
                memory_type=MemoryType.PREFERENCE,
                domain_key="global",
                slot_key="preference:global:verb",
            )
            is None
        )

    def test_trusted_slots_return_one_record_each(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-16"""

        factories.insert_record(
            engine,
            memory_type=MemoryType.IDENTITY,
            slot_key="identity:global:name",
            cardinality=Cardinality.EXCLUSIVE,
        )
        factories.insert_record(
            engine,
            memory_type=MemoryType.PREFERENCE,
            slot_key="preference:global:verbosity",
            cardinality=Cardinality.EXCLUSIVE,
        )
        found = repo.list_recall_eligible_for_slots(
            ("identity:global:name", "preference:global:verbosity"), now=FROZEN_NOW
        )
        assert len(found) == 2

    def test_blank_slot_keys_are_ignored(self, repo: MemoryRepository) -> None:
        """REP-16b"""

        assert repo.list_recall_eligible_for_slots(("", "   "), now=FROZEN_NOW) == []

    def test_duplicate_slot_keys_are_collapsed(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-16c"""

        factories.insert_record(
            engine,
            memory_type=MemoryType.IDENTITY,
            slot_key="identity:global:name",
            cardinality=Cardinality.EXCLUSIVE,
        )
        found = repo.list_recall_eligible_for_slots(
            ("identity:global:name", "identity:global:name"), now=FROZEN_NOW
        )
        assert len(found) == 1

    def test_too_many_trusted_slots_are_refused(self, repo: MemoryRepository) -> None:
        """REP-16d — an unbounded IN clause is a denial-of-service shape."""

        with pytest.raises(ValueError, match="trusted_slot_query_out_of_range"):
            repo.list_recall_eligible_for_slots(
                tuple(f"slot:{index}" for index in range(51)), now=FROZEN_NOW
            )


class TestSources:
    def test_only_active_sources_are_returned(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-17 — a detached source no longer vouches for the fact."""

        operation_id = factories.insert_operation(engine)
        record_id = factories.insert_record(engine, operation_id=operation_id)
        for index, active in enumerate((True, False)):
            repo.add_source(
                MemorySource(
                    id=f"{UUID_A[:-1]}{index}",
                    owner_id=OWNER_ID,
                    memory_id=record_id,
                    source_kind="chat_message",
                    source_content_hash=f"{index}" * 64,
                    observed_at=FROZEN_NOW,
                    assertion_role="supports",
                    is_active=active,
                    operation_id=operation_id,
                )
            )
        session.commit()
        grouped = repo.active_source_ids_for_records((record_id,))
        assert len(grouped[record_id]) == 1

    def test_a_record_with_no_sources_maps_to_an_empty_tuple(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-17b — the key is present, so callers need no special case."""

        record_id = factories.insert_record(engine)
        assert repo.active_source_ids_for_records((record_id,)) == {record_id: ()}

    def test_no_ids_returns_an_empty_mapping(self, repo: MemoryRepository) -> None:
        """REP-17c"""

        assert repo.active_source_ids_for_records(()) == {}

    def test_a_source_for_a_missing_record_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-24a"""

        operation_id = factories.insert_operation(engine)
        with pytest.raises(MemoryNotFoundError, match="source_reference_not_found"):
            repo.add_source(
                MemorySource(
                    id=UUID_A,
                    owner_id=OWNER_ID,
                    memory_id=UUID_B,
                    source_kind="chat_message",
                    source_content_hash="a" * 64,
                    observed_at=FROZEN_NOW,
                    assertion_role="supports",
                    is_active=True,
                    operation_id=operation_id,
                )
            )

    def test_a_repeated_source_is_returned_rather_than_duplicated(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-24b — a reconfirmation may cite the exact same evidence.

        Provenance is deduplicated by (record, content hash), so citing it twice
        is acknowledged rather than turned into a transaction failure.
        """

        operation_id = factories.insert_operation(engine)
        record_id = factories.insert_record(engine, operation_id=operation_id)

        def _source(source_id: str) -> MemorySource:
            return MemorySource(
                id=source_id,
                owner_id=OWNER_ID,
                memory_id=record_id,
                source_kind="chat_message",
                source_content_hash="a" * 64,
                observed_at=FROZEN_NOW,
                assertion_role="supports",
                is_active=True,
                operation_id=operation_id,
            )

        first = repo.add_source(_source(UUID_A))
        second = repo.add_source(_source(UUID_B))
        assert second.id == first.id


class TestRelationsAndEvents:
    def test_a_relation_to_a_missing_record_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-25"""

        operation_id = factories.insert_operation(engine)
        record_id = factories.insert_record(engine, operation_id=operation_id)
        with pytest.raises(MemoryNotFoundError, match="relation_endpoint_not_found"):
            repo.add_relation(
                MemoryRelation(
                    id=UUID_A,
                    owner_id=OWNER_ID,
                    from_memory_id=record_id,
                    relation_type="supersedes",
                    to_memory_id=UUID_B,
                    operation_id=operation_id,
                )
            )

    def test_a_relation_with_a_missing_operation_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-25b"""

        operation_id = factories.insert_operation(engine)
        first = factories.insert_record(engine, operation_id=operation_id)
        second = factories.insert_record(
            engine, operation_id=operation_id, slot_key=f"goal:global:independent:{UUID_B}"
        )
        with pytest.raises(MemoryNotFoundError, match="relation_operation_not_found"):
            repo.add_relation(
                MemoryRelation(
                    id=UUID_A,
                    owner_id=OWNER_ID,
                    from_memory_id=first,
                    relation_type="supersedes",
                    to_memory_id=second,
                    operation_id=UUID_B,
                )
            )

    def test_an_outbox_event_for_a_missing_record_is_refused(self, repo: MemoryRepository) -> None:
        """REP-24c"""

        with pytest.raises(MemoryNotFoundError, match="outbox_memory_not_found"):
            repo.add_outbox_event(
                MemoryOutbox(
                    id=UUID_A,
                    owner_id=OWNER_ID,
                    event_kind="canonical_upsert",
                    memory_id=UUID_B,
                    event_payload_json={},
                    state="pending",
                    attempts=0,
                    event_idempotency_key="event-1",
                )
            )

    def test_a_tombstone_with_a_missing_operation_is_refused(self, repo: MemoryRepository) -> None:
        """REP-24d"""

        with pytest.raises(MemoryNotFoundError, match="tombstone_operation_not_found"):
            repo.add_tombstone(
                MemoryTombstone(
                    id=UUID_A,
                    owner_id=OWNER_ID,
                    fingerprint_digest="a" * 64,
                    fingerprint_key_version="v1",
                    memory_type="goal",
                    originating_operation_id=UUID_B,
                    created_at=FROZEN_NOW,
                    expires_at=FROZEN_NOW + timedelta(days=30),
                )
            )

    def test_deleting_an_unknown_tombstone_returns_false(self, repo: MemoryRepository) -> None:
        """REP-32"""

        assert repo.delete_tombstone(UUID_A) is False


class TestUsageRecording:
    def test_usage_increments_the_count_and_stamps_the_time(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-20"""

        record_id = factories.insert_record(engine)
        repo.record_recall_usage((record_id,), used_at=FROZEN_NOW, request_id="req-1")
        session.commit()
        record = repo.get_record(record_id, statuses=ACTIVE)
        assert record is not None
        assert record.usage_count == 1
        assert record.last_used_at is not None

    def test_usage_is_recorded_once_per_request(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-19 — replaying a request must not inflate the count of events."""

        record_id = factories.insert_record(engine)
        repo.record_recall_usage((record_id,), used_at=FROZEN_NOW, request_id="req-1")
        repo.record_recall_usage((record_id,), used_at=FROZEN_NOW, request_id="req-1")
        session.commit()
        with engine.begin() as connection:
            events = connection.execute(
                text("SELECT COUNT(*) FROM memory_usage_events")
            ).scalar_one()
        assert events == 1

    def test_usage_writes_one_event_per_memory(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-18"""

        first = factories.insert_record(engine)
        second = factories.insert_record(engine, slot_key=f"goal:global:independent:{UUID_B}")
        repo.record_recall_usage((first, second), used_at=FROZEN_NOW, request_id="req-1")
        session.commit()
        with engine.begin() as connection:
            events = connection.execute(
                text("SELECT COUNT(*) FROM memory_usage_events")
            ).scalar_one()
        assert events == 2

    def test_recording_usage_for_an_ineligible_record_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-18b — you cannot claim to have used a forgotten memory."""

        record_id = factories.insert_record(engine, status=MemoryLifecycleState.FORGOTTEN)
        with pytest.raises(MemoryNotFoundError, match="usage_selection_not_fully_eligible"):
            repo.record_recall_usage((record_id,), used_at=FROZEN_NOW, request_id="req-1")

    def test_a_partially_eligible_selection_records_nothing(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-18c — all or nothing, so the count can never drift."""

        good = factories.insert_record(engine)
        bad = factories.insert_record(
            engine,
            status=MemoryLifecycleState.ARCHIVED,
            slot_key=f"goal:global:independent:{UUID_B}",
        )
        with pytest.raises(MemoryNotFoundError):
            repo.record_recall_usage((good, bad), used_at=FROZEN_NOW, request_id="req-1")
        session.rollback()
        record = repo.get_record(good, statuses=ACTIVE)
        assert record is not None
        assert record.usage_count == 0

    def test_no_ids_records_nothing(self, repo: MemoryRepository) -> None:
        """REP-18d"""

        assert repo.record_recall_usage((), used_at=FROZEN_NOW) == ()


class TestFieldUpdates:
    def test_an_unknown_field_is_refused(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-26 — the update allowlist is what stops owner_id being rewritten."""

        record_id = factories.insert_record(engine)
        with pytest.raises(ValueError, match="record_update_fields_not_allowed"):
            repo.update_record_fields(
                record_id, expected_revision=1, values={"owner_id": OTHER_OWNER_ID}
            )

    def test_an_unknown_metadata_key_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-27"""

        record_id = factories.insert_record(engine)
        with pytest.raises(ValueError, match="record_metadata_keys_not_allowed"):
            repo.update_record_fields(
                record_id,
                expected_revision=1,
                values={"metadata_json": {"smuggled": "value"}},
            )

    def test_an_allowed_metadata_key_is_accepted(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-27b"""

        record_id = factories.insert_record(engine)
        updated = repo.update_record_fields(
            record_id, expected_revision=1, values={"metadata_json": {"tags": ["x"]}}
        )
        session.commit()
        assert updated.metadata_json == {"tags": ["x"]}

    def test_an_update_bumps_the_revision(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-28 — the revision is what makes optimistic concurrency work."""

        record_id = factories.insert_record(engine)
        updated = repo.update_record_fields(
            record_id, expected_revision=1, values={"importance": 7}
        )
        session.commit()
        assert updated.revision == 2
        assert updated.importance == 7

    def test_a_stale_revision_conflicts(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-29 — someone else wrote first; the caller must re-read."""

        record_id = factories.insert_record(engine)
        with pytest.raises(MemoryRevisionConflict):
            repo.update_record_fields(record_id, expected_revision=99, values={"importance": 7})

    def test_updating_a_missing_record_conflicts(self, repo: MemoryRepository) -> None:
        """REP-29b"""

        with pytest.raises(MemoryRevisionConflict):
            repo.update_record_fields(UUID_A, expected_revision=1, values={"importance": 7})

    def test_updating_another_owners_record_conflicts(
        self, other_engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-29c — indistinguishable from "not found", which is the point."""

        foreign_id = factories.insert_record(other_engine, owner=OTHER_OWNER_ID)
        with pytest.raises(MemoryRevisionConflict):
            repo.update_record_fields(foreign_id, expected_revision=1, values={"importance": 7})

    @pytest.mark.parametrize("revision", [0, -1])
    def test_a_non_positive_expected_revision_is_refused(
        self, engine: Engine, repo: MemoryRepository, revision: int
    ) -> None:
        """REP-29d"""

        record_id = factories.insert_record(engine)
        with pytest.raises(ValueError, match="expected_revision_must_be_positive"):
            repo.update_record_fields(
                record_id, expected_revision=revision, values={"importance": 7}
            )

    def test_an_empty_update_is_refused(self, engine: Engine, repo: MemoryRepository) -> None:
        """REP-26b"""

        record_id = factories.insert_record(engine)
        with pytest.raises(ValueError, match="record_update_requires_values"):
            repo.update_record_fields(record_id, expected_revision=1, values={})

    def test_prohibited_content_cannot_arrive_by_update(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-26c — the scanner runs on the update path too, not just insert."""

        record_id = factories.insert_record(engine)
        with pytest.raises(MemoryProhibitedContentError):
            repo.update_record_fields(
                record_id,
                expected_revision=1,
                values={"display_text": "my password is hunter2"},
            )


class TestCandidateUpdates:
    def test_an_unknown_candidate_field_is_refused(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-30"""

        candidate_id = factories.insert_candidate(engine)
        with pytest.raises(ValueError, match="candidate_update_fields_not_allowed"):
            repo.update_candidate_decision(
                candidate_id, expected_revision=1, values={"owner_id": OTHER_OWNER_ID}
            )

    def test_a_stale_candidate_revision_conflicts(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-31"""

        candidate_id = factories.insert_candidate(engine)
        with pytest.raises(MemoryRevisionConflict):
            repo.update_candidate_decision(
                candidate_id, expected_revision=99, values={"state": "rejected"}
            )

    def test_a_decision_bumps_the_candidate_revision(
        self, engine: Engine, repo: MemoryRepository, session: Session
    ) -> None:
        """REP-31b"""

        candidate_id = factories.insert_candidate(engine)
        updated = repo.update_candidate_decision(
            candidate_id, expected_revision=1, values={"state": "rejected"}
        )
        session.commit()
        assert updated.revision == 2
        assert updated.state == "rejected"

    def test_a_candidate_from_another_owner_is_invisible(
        self, other_engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-30b"""

        foreign_id = factories.insert_candidate(other_engine, owner=OTHER_OWNER_ID)
        assert repo.get_candidate(foreign_id) is None


class TestIndexCandidates:
    def test_index_candidates_are_ordered_and_resumable(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-33b — reconciliation pages through the store by id."""

        ids = sorted(
            factories.insert_record(
                engine, slot_key=f"goal:global:independent:{UUID_A[:-1]}{index}"
            )
            for index in range(4)
        )
        first_page = repo.list_index_candidates(now=FROZEN_NOW, limit=2)
        assert [item.id for item in first_page] == ids[:2]

        second_page = repo.list_index_candidates(now=FROZEN_NOW, limit=2, after_memory_id=ids[1])
        assert [item.id for item in second_page] == ids[2:]

    def test_index_candidates_exclude_ineligible_records(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-33c — a forgotten record must not be re-indexed."""

        factories.insert_record(engine, status=MemoryLifecycleState.FORGOTTEN)
        assert repo.list_index_candidates(now=FROZEN_NOW) == []


class TestOperationLookup:
    def test_an_operation_is_found_by_idempotency_key(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-22"""

        factories.insert_operation(engine, idempotency_key="known-key")
        assert repo.get_operation_by_idempotency_key("known-key") is not None

    def test_an_unknown_key_returns_none(self, repo: MemoryRepository) -> None:
        """REP-22b"""

        assert repo.get_operation_by_idempotency_key("never-used") is None

    def test_another_owners_key_is_invisible(
        self, other_engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-22c — two profiles may reuse the same request id safely."""

        factories.insert_operation(other_engine, owner=OTHER_OWNER_ID, idempotency_key="shared-key")
        assert repo.get_operation_by_idempotency_key("shared-key") is None


class TestActiveLookups:
    def test_find_active_slot_ignores_inactive_records(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-21a"""

        factories.insert_record(engine, status=MemoryLifecycleState.SUPERSEDED)
        assert (
            repo.find_active_slot(
                subject_key="user",
                memory_type=MemoryType.GOAL,
                domain_key="global",
                slot_key=factories.DEFAULT_GOAL_SLOT,
            )
            == []
        )

    def test_find_active_fingerprint_ignores_inactive_records(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-21b — this is what lets a forgotten fact be learned again."""

        fingerprint = f"sha256:{'a' * 64}"
        factories.insert_record(
            engine,
            canonical_fingerprint=fingerprint,
            status=MemoryLifecycleState.FORGOTTEN,
        )
        assert repo.find_active_fingerprint(fingerprint) is None

    def test_find_active_fingerprint_finds_an_active_record(
        self, engine: Engine, repo: MemoryRepository
    ) -> None:
        """REP-21c"""

        fingerprint = f"sha256:{'a' * 64}"
        factories.insert_record(engine, canonical_fingerprint=fingerprint)
        assert repo.find_active_fingerprint(fingerprint) is not None

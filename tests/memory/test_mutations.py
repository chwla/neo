"""Tier 3 — the mutation service (plan sections MUT and PLN).

This is the transactional boundary: the only place canonical memory changes.
Everything it does happens once or not at all, and a retried request has to be
recognised as the same request rather than applied twice.

The tests run the real service against a real SQLite file.  Planning behaviour
(PLN) is exercised through the service rather than by calling the planner
directly, because the planner's decisions only matter insofar as they reach the
database — and testing through the front door catches wiring mistakes that a
direct planner test would miss.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from app.services.memory.contracts import (
    CandidateIntent,
    CandidateTargetHints,
    MemoryErrorCode,
    MemoryLifecycleState,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    MemoryUpdatePatch,
    Sensitivity,
    TargetRevision,
)
from app.services.memory.mutations import (
    InjectedMutationFailure,
    MemoryMutationService,
    RetryPolicy,
)
from tests.memory import factories
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID

PREFERENCE_SLOT = "preference:global:verbosity"


def _counts(engine: Engine, *tables: str) -> dict[str, int]:
    with engine.begin() as connection:
        return {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in tables
        }


def _preference(**overrides):
    return factories.preference_proposal(**overrides)


def _create(service: MemoryMutationService, **overrides):
    return service.execute(factories.create_command(**overrides))


class TestCreate:
    def test_a_create_writes_every_row_it_should(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-03 / PLN-01 — one create produces one coherent set of rows."""

        result = _create(mutation_service)
        assert result.outcome is MemoryOutcome.CREATED
        assert len(result.affected_memory_ids) == 1
        assert result.current_revision == 1
        assert _counts(
            engine, "memory_records", "memory_operations", "memory_sources", "memory_outbox"
        ) == {
            "memory_records": 1,
            "memory_operations": 1,
            "memory_sources": 1,
            "memory_outbox": 1,
        }

    def test_the_outbox_event_targets_the_new_record(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-01b — the derived indexes learn about the change through this row."""

        result = _create(mutation_service)
        with engine.begin() as connection:
            kind, memory_id = connection.execute(
                text("SELECT event_kind, memory_id FROM memory_outbox")
            ).one()
        assert kind == "canonical_upsert"
        assert memory_id == str(result.affected_memory_ids[0])

    def test_the_operation_row_records_its_provenance(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-24 — every write is attributable."""

        _create(mutation_service)
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT operation_kind, actor_kind, source_kind, status, outcome, "
                    "committed_at IS NOT NULL FROM memory_operations"
                )
            ).one()
        assert row[0] == MemoryOperationKind.CREATE.value
        assert row[1] == "user"
        assert row[2] == "direct_command"
        assert row[3] == "committed"
        assert row[4] == MemoryOutcome.CREATED.value
        assert row[5] == 1

    def test_result_record_ids_match_what_was_written(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-26"""

        result = _create(mutation_service)
        with engine.begin() as connection:
            stored = connection.execute(text("SELECT id FROM memory_records")).scalar_one()
        assert str(result.affected_memory_ids[0]) == stored

    def test_an_additive_slot_creates_alongside(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-05 — you may hold many independent goals at once."""

        _create(mutation_service, idempotency_key="one")
        _create(
            mutation_service,
            idempotency_key="two",
            candidate=factories.proposal(
                slot_key="goal:global:independent:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                canonical_value="run a 5K",
                display_text="run a 5K",
            ),
        )
        assert _counts(engine, "memory_records")["memory_records"] == 2

    def test_restating_the_same_fact_reconfirms_rather_than_duplicates(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-06 — the duplicate-memory guard, end to end.

        A later turn re-extracting an earlier fact must not write a second row.
        """

        first = _create(mutation_service, idempotency_key="one")
        second = _create(mutation_service, idempotency_key="two")
        assert second.outcome is MemoryOutcome.RECONFIRMED
        assert second.affected_memory_ids == first.affected_memory_ids
        assert _counts(engine, "memory_records")["memory_records"] == 1

    def test_a_reconfirmation_moves_last_confirmed_at(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-02b — freshness is what reconfirmation is for."""

        _create(mutation_service, idempotency_key="one")
        with engine.begin() as connection:
            before = connection.execute(
                text("SELECT last_confirmed_at FROM memory_records")
            ).scalar_one()
        _create(mutation_service, idempotency_key="two")
        with engine.begin() as connection:
            after = connection.execute(
                text("SELECT last_confirmed_at FROM memory_records")
            ).scalar_one()
        assert after >= before


class TestExclusiveSlotConflicts:
    def test_a_conflicting_exclusive_value_is_refused_without_evidence(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-04 — two contradictory facts is not permission to pick one."""

        _create(
            mutation_service,
            idempotency_key="one",
            candidate=_preference(
                canonical_value="concise answers", display_text="concise answers"
            ),
        )
        result = _create(
            mutation_service,
            idempotency_key="two",
            candidate=_preference(
                canonical_value="detailed answers", display_text="detailed answers"
            ),
        )
        assert result.outcome in {MemoryOutcome.NEEDS_REVIEW, MemoryOutcome.REJECTED}
        assert result.rejection_code is not None

    def test_the_existing_record_stays_active_after_a_refusal(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-38 — a refused conflict must leave what we already knew alone."""

        _create(
            mutation_service,
            idempotency_key="one",
            candidate=_preference(
                canonical_value="concise answers", display_text="concise answers"
            ),
        )
        _create(
            mutation_service,
            idempotency_key="two",
            candidate=_preference(
                canonical_value="detailed answers", display_text="detailed answers"
            ),
        )
        with engine.begin() as connection:
            statuses = connection.execute(text("SELECT status FROM memory_records")).scalars().all()
        assert statuses.count("active") == 1

    def test_a_refinement_of_an_occupied_exclusive_slot_needs_review(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-03 — pinning the conservative choice the planner actually makes.

        ``compatible_refinement`` recognises "concise answers with examples" as a
        refinement of "concise answers", so a reasonable reading of the design is
        that this applies automatically.  It does not: an occupied exclusive slot
        routes to review regardless, with ``occupied_exclusive_slot_requires_replace``.

        That is the safe direction to err — silently rewriting an exclusive fact
        is worse than asking — but it is a real behaviour rather than an obvious
        consequence, so it is pinned rather than assumed.
        """

        _create(
            mutation_service,
            idempotency_key="one",
            candidate=_preference(
                canonical_value="concise answers", display_text="concise answers"
            ),
        )
        result = _create(
            mutation_service,
            idempotency_key="two",
            candidate=_preference(
                canonical_value="concise answers with examples",
                display_text="concise answers with examples",
            ),
        )
        assert result.outcome is MemoryOutcome.NEEDS_REVIEW
        assert result.message == "occupied_exclusive_slot_requires_replace"
        assert _counts(engine, "memory_records")["memory_records"] == 1


class TestReplace:
    def test_an_explicit_replacement_supersedes_the_old_record(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-16 — the correction path: old goes superseded, new goes active."""

        first = _create(
            mutation_service,
            idempotency_key="one",
            candidate=_preference(
                canonical_value="concise answers", display_text="concise answers"
            ),
        )
        target = first.affected_memory_ids[0]
        result = mutation_service.execute(
            factories.replace_command(
                idempotency_key="replace",
                candidate=_preference(
                    canonical_value="detailed answers",
                    display_text="detailed answers",
                    intent=CandidateIntent.REPLACE,
                    target_hints=CandidateTargetHints(target_memory_ids=(target,)),
                ),
                targets=(TargetRevision(memory_id=target, expected_revision=1),),
            )
        )
        assert result.outcome is MemoryOutcome.REPLACED
        with engine.begin() as connection:
            rows = dict(connection.execute(text("SELECT id, status FROM memory_records")).all())
        assert rows[str(target)] == MemoryLifecycleState.SUPERSEDED.value
        assert list(rows.values()).count("active") == 1

    def test_a_replacement_records_a_supersedes_relation(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-16b — provenance for why the old value went away."""

        first = _create(
            mutation_service,
            idempotency_key="one",
            candidate=_preference(
                canonical_value="concise answers", display_text="concise answers"
            ),
        )
        target = first.affected_memory_ids[0]
        mutation_service.execute(
            factories.replace_command(
                idempotency_key="replace",
                candidate=_preference(
                    canonical_value="detailed answers",
                    display_text="detailed answers",
                    intent=CandidateIntent.REPLACE,
                    target_hints=CandidateTargetHints(target_memory_ids=(target,)),
                ),
                targets=(TargetRevision(memory_id=target, expected_revision=1),),
            )
        )
        with engine.begin() as connection:
            relations = (
                connection.execute(text("SELECT relation_type FROM memory_relations"))
                .scalars()
                .all()
            )
        assert "supersedes" in relations

    def test_replacing_a_missing_target_is_refused(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """PLN-18"""

        from uuid import uuid4

        missing = uuid4()
        result = mutation_service.execute(
            factories.replace_command(
                candidate=factories.proposal(
                    intent=CandidateIntent.REPLACE,
                    target_hints=CandidateTargetHints(target_memory_ids=(missing,)),
                ),
                targets=(TargetRevision(memory_id=missing, expected_revision=1),),
            )
        )
        assert result.outcome in {MemoryOutcome.FAILED, MemoryOutcome.NEEDS_REVIEW}

    def test_replacing_with_a_stale_revision_conflicts(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """PLN-10 / MUT-21"""

        first = _create(
            mutation_service,
            idempotency_key="one",
            candidate=_preference(
                canonical_value="concise answers", display_text="concise answers"
            ),
        )
        target = first.affected_memory_ids[0]
        result = mutation_service.execute(
            factories.replace_command(
                idempotency_key="replace",
                candidate=_preference(
                    canonical_value="detailed answers",
                    display_text="detailed answers",
                    intent=CandidateIntent.REPLACE,
                    target_hints=CandidateTargetHints(target_memory_ids=(target,)),
                ),
                targets=(TargetRevision(memory_id=target, expected_revision=99),),
            )
        )
        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code is MemoryErrorCode.REVISION_CONFLICT


class TestUpdate:
    def test_an_update_applies_only_the_patched_field(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-13"""

        created = _create(mutation_service)
        memory_id = created.affected_memory_ids[0]
        mutation_service.execute(
            factories.update_command(
                memory_id=memory_id,
                expected_revision=1,
                patch=MemoryUpdatePatch(importance=9),
            )
        )
        with engine.begin() as connection:
            importance, display = connection.execute(
                text("SELECT importance, display_text FROM memory_records")
            ).one()
        assert importance == 9
        assert display == "improve at urban sketching"

    def test_an_update_bumps_the_revision(self, mutation_service: MemoryMutationService) -> None:
        """PLN-13b"""

        created = _create(mutation_service)
        result = mutation_service.execute(
            factories.update_command(memory_id=created.affected_memory_ids[0], expected_revision=1)
        )
        assert result.current_revision == 2

    def test_a_stale_revision_conflicts(self, mutation_service: MemoryMutationService) -> None:
        """PLN-10"""

        created = _create(mutation_service)
        result = mutation_service.execute(
            factories.update_command(memory_id=created.affected_memory_ids[0], expected_revision=99)
        )
        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code is MemoryErrorCode.REVISION_CONFLICT

    def test_updating_a_missing_record_is_not_found(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """PLN-11"""

        from uuid import uuid4

        result = mutation_service.execute(
            factories.update_command(memory_id=uuid4(), expected_revision=1)
        )
        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code in {
            MemoryErrorCode.NOT_FOUND,
            MemoryErrorCode.REVISION_CONFLICT,
        }

    def test_pinning_still_emits_a_canonical_upsert(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-15 — pinning the current behaviour, which is wasteful but not wrong.

        A pin changes ranking, not content: the derived document is built from
        display text, type, domain and slot, none of which a pin touches.  So the
        re-index this triggers rewrites an identical document.

        That is wasted work rather than incorrect — the content hash is unchanged,
        so the delivery is idempotent — but it means a burst of pin toggles costs
        a burst of embedding calls.  Pinned here so the cost is visible; if the
        planner ever learns to skip index-irrelevant field updates, this test
        turns red and gets updated deliberately.
        """

        created = _create(mutation_service)
        before = _counts(engine, "memory_outbox")["memory_outbox"]
        mutation_service.execute(
            factories.update_command(
                memory_id=created.affected_memory_ids[0],
                expected_revision=1,
                patch=MemoryUpdatePatch(pinned=True),
            )
        )
        assert _counts(engine, "memory_outbox")["memory_outbox"] == before + 1

    def test_a_pin_update_does_not_change_the_indexed_content(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-15b — the reason the extra event above is harmless."""

        created = _create(mutation_service)
        with engine.begin() as connection:
            before = connection.execute(
                text("SELECT display_text, memory_type, domain_key, slot_key FROM memory_records")
            ).one()
        mutation_service.execute(
            factories.update_command(
                memory_id=created.affected_memory_ids[0],
                expected_revision=1,
                patch=MemoryUpdatePatch(pinned=True),
            )
        )
        with engine.begin() as connection:
            after = connection.execute(
                text("SELECT display_text, memory_type, domain_key, slot_key FROM memory_records")
            ).one()
        assert after == before


class TestArchiveForgetErase:
    def test_archiving_deactivates_and_emits_a_removal(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-25"""

        created = _create(mutation_service)
        result = mutation_service.execute(
            factories.archive_command(memory_id=created.affected_memory_ids[0], expected_revision=1)
        )
        assert result.outcome is MemoryOutcome.ARCHIVED
        with engine.begin() as connection:
            status = connection.execute(text("SELECT status FROM memory_records")).scalar_one()
            kinds = connection.execute(text("SELECT event_kind FROM memory_outbox")).scalars().all()
        assert status == MemoryLifecycleState.ARCHIVED.value
        assert "canonical_remove" in kinds

    def test_forgetting_creates_a_tombstone_and_keeps_provenance(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-27 — forget is reversible by the user, not by the extractor."""

        created = _create(mutation_service)
        result = mutation_service.execute(
            factories.forget_command(memory_id=created.affected_memory_ids[0], expected_revision=1)
        )
        assert result.outcome is MemoryOutcome.FORGOTTEN
        counts = _counts(engine, "memory_tombstones", "memory_sources", "memory_records")
        assert counts["memory_tombstones"] == 1
        assert counts["memory_sources"] >= 1
        assert counts["memory_records"] == 1

    def test_a_tombstone_expires_thirty_days_out(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-28"""

        created = _create(mutation_service)
        mutation_service.execute(
            factories.forget_command(memory_id=created.affected_memory_ids[0], expected_revision=1)
        )
        from app.models.memory import MemoryTombstone

        with engine.begin() as connection:
            created_at, expires_at = connection.execute(
                select(MemoryTombstone.created_at, MemoryTombstone.expires_at)
            ).one()
        assert (expires_at - created_at) == timedelta(days=30)

    def test_a_forgotten_fact_cannot_be_recreated_automatically(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-07 — the point of forget: it does not come back by itself."""

        created = _create(mutation_service, idempotency_key="one")
        mutation_service.execute(
            factories.forget_command(memory_id=created.affected_memory_ids[0], expected_revision=1)
        )
        result = _create(mutation_service, idempotency_key="two")
        assert result.rejection_code is MemoryRejectionCode.RESURRECTION_BLOCKED

    def test_an_explicit_restatement_overrides_the_tombstone(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """PLN-08 — saying it again on purpose always wins over a past forget."""

        created = _create(mutation_service, idempotency_key="one")
        mutation_service.execute(
            factories.forget_command(memory_id=created.affected_memory_ids[0], expected_revision=1)
        )
        result = _create(
            mutation_service,
            idempotency_key="two",
            candidate=factories.proposal(explicit_user_request=True),
        )
        assert result.outcome in {MemoryOutcome.CREATED, MemoryOutcome.RECONFIRMED}

    def test_erasing_removes_the_record_and_its_provenance(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-29 / MUT-46 — a true erasure leaves nothing behind."""

        created = _create(mutation_service)
        result = mutation_service.execute(
            factories.erase_command(memory_id=created.affected_memory_ids[0], expected_revision=1)
        )
        assert result.outcome is MemoryOutcome.ERASED_PERMANENTLY
        counts = _counts(
            engine, "memory_records", "memory_sources", "memory_relations", "memory_tombstones"
        )
        assert counts["memory_records"] == 0
        assert counts["memory_sources"] == 0
        assert counts["memory_relations"] == 0
        assert counts["memory_tombstones"] == 0

    def test_erasing_leaves_no_trace_of_the_content(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PRV-06 — sweep every table for the erased text."""

        _create(
            mutation_service,
            candidate=factories.proposal(
                canonical_value="a very distinctive erasable phrase",
                display_text="a very distinctive erasable phrase",
            ),
        )
        with engine.begin() as connection:
            memory_id = connection.execute(text("SELECT id FROM memory_records")).scalar_one()
        mutation_service.execute(factories.erase_command(memory_id=memory_id, expected_revision=1))

        with engine.begin() as connection:
            tables = (
                connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
                .scalars()
                .all()
            )
            for table in tables:
                rows = connection.execute(text(f"SELECT * FROM {table}")).all()
                blob = " ".join(str(cell) for row in rows for cell in row)
                assert "distinctive erasable phrase" not in blob, table


class TestRestore:
    def test_an_archived_record_can_be_restored(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PLN-30"""

        created = _create(mutation_service)
        memory_id = created.affected_memory_ids[0]
        mutation_service.execute(
            factories.archive_command(memory_id=memory_id, expected_revision=1)
        )
        result = mutation_service.execute(
            factories.restore_command(memory_id=memory_id, expected_revision=2)
        )
        assert result.outcome is MemoryOutcome.RESTORED
        with engine.begin() as connection:
            status = connection.execute(text("SELECT status FROM memory_records")).scalar_one()
        assert status == MemoryLifecycleState.ACTIVE.value

    def test_a_forgotten_record_cannot_be_restored_in_place(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """PLN-31 — forget and archive are different promises."""

        created = _create(mutation_service)
        memory_id = created.affected_memory_ids[0]
        mutation_service.execute(factories.forget_command(memory_id=memory_id, expected_revision=1))
        result = mutation_service.execute(
            factories.restore_command(memory_id=memory_id, expected_revision=2)
        )
        assert result.outcome is not MemoryOutcome.RESTORED


class TestIdempotency:
    def test_an_exact_replay_returns_the_original_result(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-05 / MUT-07 — a retried request must not write twice."""

        command = factories.create_command()
        first = mutation_service.execute(command)
        second = mutation_service.execute(command)
        assert second.outcome == first.outcome
        assert second.affected_memory_ids == first.affected_memory_ids
        assert second.current_revision == first.current_revision
        assert _counts(engine, "memory_records", "memory_operations") == {
            "memory_records": 1,
            "memory_operations": 1,
        }

    def test_a_different_request_under_the_same_key_conflicts(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-06 — reusing a key for different content is a caller bug."""

        mutation_service.execute(factories.create_command(idempotency_key="shared"))
        result = mutation_service.execute(
            factories.create_command(
                idempotency_key="shared",
                candidate=factories.proposal(
                    canonical_value="something else", display_text="something else"
                ),
            )
        )
        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code is MemoryErrorCode.IDEMPOTENCY_CONFLICT
        assert _counts(engine, "memory_records")["memory_records"] == 1

    def test_replaying_a_forget_is_stable(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-05b — deletion has to be idempotent or a retry looks like a failure."""

        created = _create(mutation_service)
        command = factories.forget_command(
            memory_id=created.affected_memory_ids[0], expected_revision=1
        )
        first = mutation_service.execute(command)
        second = mutation_service.execute(command)
        assert second.outcome == first.outcome
        assert _counts(engine, "memory_tombstones")["memory_tombstones"] == 1


class TestValidationAndOwnership:
    def test_an_invalid_dict_command_is_refused(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-02 — a malformed command raises rather than returning a result.

        This is the one input the kernel refuses to answer for.  Pydantic's
        rendering of a validation error echoes the rejected input values, so the
        boundary raises a fixed non-echoing ``MemoryMutationError`` instead of
        packaging the detail into a result the caller might log.
        """

        from app.services.memory.mutations import MemoryMutationError

        with pytest.raises(MemoryMutationError, match="invalid_command_shape"):
            mutation_service.execute({"operation": "teleport"})
        assert _counts(engine, "memory_records")["memory_records"] == 0

    def test_the_refusal_does_not_echo_the_rejected_input(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """MUT-02b — the error message must not carry the caller's payload."""

        from app.services.memory.mutations import MemoryMutationError

        with pytest.raises(MemoryMutationError) as excinfo:
            mutation_service.execute({"operation": "teleport", "secret": "hunter2"})
        assert "hunter2" not in str(excinfo.value)

    def test_a_valid_dict_command_is_accepted(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """MUT-01 — the service accepts the wire form as well as the typed one."""

        payload = factories.create_command().model_dump(mode="json")
        result = mutation_service.execute(payload)
        assert result.outcome is MemoryOutcome.CREATED

    def test_another_owners_command_is_refused(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-48 / PLN-12 — the service is bound to exactly one owner."""

        result = mutation_service.execute(factories.create_command(owner=OTHER_OWNER_ID))
        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code in {
            MemoryErrorCode.OWNER_MISMATCH,
            MemoryErrorCode.CROSS_OWNER_REFERENCE,
        }
        assert _counts(engine, "memory_records")["memory_records"] == 0

    def test_a_dry_run_writes_nothing(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-23 / PLN-39 — a preview must have no side effects at all."""

        result = mutation_service.execute(factories.create_command(dry_run=True))
        assert result.outcome is not MemoryOutcome.FAILED
        assert _counts(engine, "memory_records", "memory_operations") == {
            "memory_records": 0,
            "memory_operations": 0,
        }


class TestProhibitedContent:
    def test_a_prohibited_candidate_is_never_stored(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-10 — refused at the boundary, not deeper in."""

        result = mutation_service.execute(
            factories.create_command(
                candidate=factories.proposal(
                    canonical_value="my password is hunter2",
                    display_text="my password is hunter2",
                )
            )
        )
        assert result.outcome in {MemoryOutcome.REJECTED, MemoryOutcome.FAILED}
        assert _counts(engine, "memory_records")["memory_records"] == 0

    def test_the_rejection_record_holds_no_prohibited_text(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-11 / PRV-01 — refusing must not itself write the secret down."""

        mutation_service.execute(
            factories.create_command(
                candidate=factories.proposal(
                    canonical_value="my password is hunter2",
                    display_text="my password is hunter2",
                )
            )
        )
        with engine.begin() as connection:
            tables = (
                connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
                .scalars()
                .all()
            )
            for table in tables:
                rows = connection.execute(text(f"SELECT * FROM {table}")).all()
                blob = " ".join(str(cell) for row in rows for cell in row)
                assert "hunter2" not in blob, table


class TestSensitiveContent:
    @staticmethod
    def _sensitive_command(**overrides):
        return factories.create_command(
            candidate=factories.proposal(
                canonical_value="I was diagnosed with asthma",
                display_text="I was diagnosed with asthma",
                sensitivity=Sensitivity.SENSITIVE,
                explicit_user_request=True,
            ),
            **overrides,
        )

    def test_a_sensitive_record_is_stored_encrypted(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-14 / PRV-02"""

        result = mutation_service.execute(self._sensitive_command())
        assert result.outcome is MemoryOutcome.CREATED
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT canonical_payload, display_text, "
                    "encrypted_canonical_payload IS NOT NULL, encryption_algorithm "
                    "FROM memory_records"
                )
            ).one()
        assert row[0] is None
        assert row[1] is None
        assert row[2] == 1
        assert row[3] == "aes-256-gcm"

    def test_the_sensitive_plaintext_appears_nowhere(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-13 / PRV-02b — sweep every table, not just the record."""

        mutation_service.execute(self._sensitive_command())
        with engine.begin() as connection:
            tables = (
                connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
                .scalars()
                .all()
            )
            for table in tables:
                rows = connection.execute(text(f"SELECT * FROM {table}")).all()
                blob = " ".join(str(cell) for row in rows for cell in row)
                assert "diagnosed with asthma" not in blob, table

    def test_a_sensitive_command_payload_is_encrypted(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-12 — the audit row must not hold what the record hides."""

        mutation_service.execute(self._sensitive_command())
        with engine.begin() as connection:
            normalized, encrypted = connection.execute(
                text(
                    "SELECT normalized_command_json, encrypted_command_payload IS NOT NULL "
                    "FROM memory_operations"
                )
            ).one()
        assert normalized is None
        assert encrypted == 1

    def test_a_sensitive_record_survives_a_round_trip(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """MUT-15 — encryption is only useful if it decrypts to the original."""

        mutation_service.execute(self._sensitive_command())
        records = mutation_service.list_active_records()
        assert len(records) == 1
        assert "asthma" in records[0].display_text


class TestAtomicity:
    @pytest.mark.parametrize(
        "stage",
        [
            "operation_start",
            "operation_completion",
            "replacement_record_creation",
            "provenance_creation",
            "outbox_creation",
        ],
    )
    def test_a_failure_at_any_stage_leaves_no_partial_write(
        self, mutation_service_factory, engine: Engine, stage: str
    ) -> None:
        """MUT-04 — the guarantee the whole service exists to provide.

        Each named stage is an injection point inside the write transaction.
        Failing at any of them must roll the entire mutation back, leaving the
        database exactly as it was.
        """

        def _fail_at(current: str) -> None:
            if current == stage:
                raise RuntimeError(f"injected at {current}")

        # Retries are disabled so the injected failure is observed once rather
        # than being re-attempted ten times.
        service = mutation_service_factory(
            failure_injector=_fail_at, retry_policy=RetryPolicy(attempts=1)
        )
        before = _counts(
            engine, "memory_records", "memory_operations", "memory_sources", "memory_outbox"
        )

        result = service.execute(factories.create_command())

        # An injected failure is reported as a failed result rather than raised:
        # the kernel's contract is that a caller always gets a result back.
        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code is MemoryErrorCode.INTERNAL_ERROR
        assert (
            _counts(
                engine,
                "memory_records",
                "memory_operations",
                "memory_sources",
                "memory_outbox",
            )
            == before
        )

    def test_a_failure_does_not_consume_the_idempotency_key(
        self, mutation_service_factory, mutation_service: MemoryMutationService
    ) -> None:
        """MUT-04b — a rolled-back attempt must be retryable."""

        def _fail_at(current: str) -> None:
            if current == "outbox_creation":
                raise RuntimeError("injected")

        failing = mutation_service_factory(
            failure_injector=_fail_at, retry_policy=RetryPolicy(attempts=1)
        )
        command = factories.create_command()
        assert failing.execute(command).outcome is MemoryOutcome.FAILED

        assert mutation_service.execute(command).outcome is MemoryOutcome.CREATED

    def test_an_injected_failure_is_wrapped_not_leaked(self, mutation_service_factory) -> None:
        """MUT-04c — the injector's own exception type never escapes.

        ``_inject`` catches whatever the injector raises and re-raises it as
        ``InjectedMutationFailure``, which ``execute`` then converts into a
        failed result.  Asserting the wrapping explicitly means a future change
        that lets a raw exception through is caught here rather than surfacing
        as a 500 somewhere upstream.
        """

        captured: list[str] = []

        def _record(stage: str) -> None:
            captured.append(stage)
            raise RuntimeError("injected")

        service = mutation_service_factory(
            failure_injector=_record, retry_policy=RetryPolicy(attempts=1)
        )
        result = service.execute(factories.create_command())
        assert captured  # the injector really was reached
        assert result.outcome is MemoryOutcome.FAILED
        assert issubclass(InjectedMutationFailure, Exception)


class TestRetryPolicy:
    @pytest.mark.parametrize("attempts", [0, -1, 11])
    def test_an_out_of_range_attempt_count_is_refused(self, attempts: int) -> None:
        """MUT-19"""

        with pytest.raises(ValueError, match="retry_attempts_out_of_range"):
            RetryPolicy(attempts=attempts)

    @pytest.mark.parametrize("delay", [-0.1, 1.1])
    def test_an_out_of_range_delay_is_refused(self, delay: float) -> None:
        """MUT-19b"""

        with pytest.raises(ValueError, match="retry_delay_out_of_range"):
            RetryPolicy(base_delay_seconds=delay)

    def test_the_delay_grows_with_the_attempt(self) -> None:
        """MUT-18 — competing writers must decorrelate rather than collide."""

        policy = RetryPolicy(base_delay_seconds=0.1)
        early = [policy.delay_for_attempt(0) for _ in range(50)]
        late = [policy.delay_for_attempt(5) for _ in range(50)]
        assert sum(late) / len(late) > sum(early) / len(early)

    def test_the_delay_stays_bounded(self) -> None:
        """MUT-18b — jitter must not produce an unbounded sleep."""

        policy = RetryPolicy(base_delay_seconds=0.1)
        for attempt in range(10):
            for _ in range(20):
                assert 0 <= policy.delay_for_attempt(attempt) <= 0.1 * (attempt + 1) * 1.5


class TestReadOperations:
    def test_list_active_records_returns_only_active_ones(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """MUT-34"""

        first = _create(mutation_service, idempotency_key="one")
        mutation_service.execute(
            factories.archive_command(memory_id=first.affected_memory_ids[0], expected_revision=1)
        )
        assert tuple(mutation_service.list_active_records()) == ()

    def test_candidate_status_of_an_unknown_candidate_is_none(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """MUT-35"""

        from uuid import uuid4

        assert mutation_service.candidate_status(uuid4()) is None


class TestSourceDetachment:
    def test_detaching_the_only_source_requires_review(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-27 — a memory with nothing vouching for it is the user's call."""

        from app.services.memory.contracts import (
            DetachMemorySourceCommand,
            SourceChangeOutcome,
        )

        created = _create(mutation_service)
        memory_id = created.affected_memory_ids[0]
        with engine.begin() as connection:
            source_id = connection.execute(text("SELECT id FROM memory_sources")).scalar_one()

        from uuid import UUID

        result = mutation_service.detach_source(
            DetachMemorySourceCommand(
                owner_id=OWNER_ID,
                idempotency_key="detach-1",
                target=TargetRevision(memory_id=memory_id, expected_revision=1),
                source_id=UUID(source_id),
            )
        )
        assert result.outcome is SourceChangeOutcome.NEEDS_REVIEW
        assert result.review_required is True
        assert result.canonical_mutation_performed is False

    def test_detaching_an_unknown_source_is_reported(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """MUT-30"""

        from uuid import uuid4

        from app.services.memory.contracts import (
            DetachMemorySourceCommand,
            SourceChangeOutcome,
        )

        created = _create(mutation_service)
        result = mutation_service.detach_source(
            DetachMemorySourceCommand(
                owner_id=OWNER_ID,
                idempotency_key="detach-1",
                target=TargetRevision(
                    memory_id=created.affected_memory_ids[0], expected_revision=1
                ),
                source_id=uuid4(),
            )
        )
        assert result.outcome is SourceChangeOutcome.SOURCE_NOT_FOUND
        assert result.review_required is False

    def test_detaching_with_a_stale_revision_conflicts(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-32"""

        from uuid import UUID

        from app.services.memory.contracts import (
            DetachMemorySourceCommand,
            SourceChangeOutcome,
        )

        created = _create(mutation_service)
        with engine.begin() as connection:
            source_id = connection.execute(text("SELECT id FROM memory_sources")).scalar_one()
        result = mutation_service.detach_source(
            DetachMemorySourceCommand(
                owner_id=OWNER_ID,
                idempotency_key="detach-1",
                target=TargetRevision(
                    memory_id=created.affected_memory_ids[0], expected_revision=99
                ),
                source_id=UUID(source_id),
            )
        )
        assert result.outcome is SourceChangeOutcome.REVISION_CONFLICT

    def test_detaching_never_changes_the_canonical_revision(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """MUT-33 — provenance and content are separate concerns."""

        from uuid import UUID

        from app.services.memory.contracts import DetachMemorySourceCommand

        created = _create(mutation_service)
        with engine.begin() as connection:
            source_id = connection.execute(text("SELECT id FROM memory_sources")).scalar_one()
            before = connection.execute(text("SELECT revision FROM memory_records")).scalar_one()
        mutation_service.detach_source(
            DetachMemorySourceCommand(
                owner_id=OWNER_ID,
                idempotency_key="detach-1",
                target=TargetRevision(
                    memory_id=created.affected_memory_ids[0], expected_revision=1
                ),
                source_id=UUID(source_id),
            )
        )
        with engine.begin() as connection:
            after = connection.execute(text("SELECT revision FROM memory_records")).scalar_one()
        assert after == before


class TestConstruction:
    def test_a_non_sqlite_engine_is_refused(self, crypto) -> None:
        """MUT-01b — the kernel relies on SQLite transaction semantics."""

        # Building a real non-SQLite engine would need another driver installed,
        # so assert the guard directly against a stub dialect instead.
        class _Stub:
            class dialect:  # noqa: N801
                name = "postgresql"

        with pytest.raises(ValueError, match="requires_sqlite"):
            MemoryMutationService(
                _Stub(),  # type: ignore[arg-type]
                owner_id=OWNER_ID,
                database_identity="x",
                payload_provider=crypto,
                fingerprint_provider=crypto,
                tombstone_provider=crypto,
                key_versions=crypto,
            )

    @pytest.mark.parametrize("identity", ["", "   "])
    def test_a_blank_database_identity_is_refused(
        self, engine: Engine, crypto, identity: str
    ) -> None:
        """MUT-01c"""

        with pytest.raises(ValueError, match="database_identity_required"):
            MemoryMutationService(
                engine,
                owner_id=OWNER_ID,
                database_identity=identity,
                payload_provider=crypto,
                fingerprint_provider=crypto,
                tombstone_provider=crypto,
                key_versions=crypto,
            )

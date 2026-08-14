"""Tier 7 — privacy properties that must hold across every table (plan section PRV).

Individual layers already have their own privacy tests: SCH-12 pins the encrypted
column shape, MUT-13 checks one operation row, IDX-01 stops a sensitive record
reaching the index. Those all ask "did *this* component behave?".

These ask the different question: after the whole pipeline has run, is the string
anywhere at all? Every test here sweeps every managed table and names the one it
found the value in. That is the only form of the question that survives someone
adding a table — a per-component test passes unchanged when a new table starts
storing the same content.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.memory_migrations import MEMORY_TABLES
from app.services.memory.contracts import (
    MemoryLifecycleState,
    MemoryOutcome,
    Sensitivity,
)
from app.services.memory.mutations import MemoryMutationService
from tests.memory import factories

DIAGNOSIS = "I was diagnosed with a rare autoimmune condition"
SECRET = "sk-live-abcdef0123456789abcdef0123456789"


def _sweep(engine: Engine) -> dict[str, str]:
    """Every cell of every managed table, flattened to text, keyed by table.

    Enumerated from `MEMORY_TABLES` rather than a hand-written list, so a table
    added by a future migration is swept automatically. A hand-written list is
    exactly the thing that stays green while a new table quietly stores the
    value.
    """

    contents: dict[str, str] = {}
    with engine.begin() as connection:
        for table in MEMORY_TABLES:
            rows = connection.execute(text(f"SELECT * FROM {table}")).all()
            contents[table] = " ".join(str(cell) for row in rows for cell in row)
    return contents


def _assert_absent(engine: Engine, needle: str) -> None:
    for table, blob in _sweep(engine).items():
        assert needle not in blob, f"{needle!r} found in {table}"


def _assert_present(engine: Engine, needle: str) -> list[str]:
    """The control for every sweep: prove the needle *would* have been found."""

    found = [table for table, blob in _sweep(engine).items() if needle in blob]
    assert found, f"{needle!r} was not stored anywhere — the sweep proves nothing"
    return found


def _sensitive_command(**overrides):
    return factories.create_command(
        candidate=factories.proposal(
            canonical_value=DIAGNOSIS,
            display_text=DIAGNOSIS,
            sensitivity=Sensitivity.SENSITIVE,
            explicit_user_request=True,
        ),
        **overrides,
    )


class TestTheSweepItself:
    def test_the_sweep_covers_every_managed_table(self, engine: Engine) -> None:
        """Guard against the sweep silently covering nothing.

        Every other test in this file is a negative assertion over `_sweep`. If
        it returned an empty mapping — a renamed table list, a changed import —
        all of them would pass while checking nothing at all.
        """

        swept = _sweep(engine)

        assert len(swept) == len(MEMORY_TABLES)
        assert "memory_records" in swept
        assert "memory_operations" in swept

    def test_ordinary_content_is_found_by_the_sweep(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """The positive control. A sweep that cannot find anything proves nothing."""

        mutation_service.execute(factories.create_command())

        assert _assert_present(engine, "improve at urban sketching")


class TestProhibitedContent:
    def test_prohibited_content_reaches_no_table(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PRV-01 — refused content must not be retained as evidence of refusal.

        The rejection is still recorded, because the user asked for something and
        deserves to know it was declined. What must not survive is the value.
        """

        result = mutation_service.execute(
            factories.create_command(
                candidate=factories.proposal(
                    canonical_value=f"my api key is {SECRET}",
                    display_text=f"my api key is {SECRET}",
                )
            )
        )

        assert result.outcome is not MemoryOutcome.CREATED
        _assert_absent(engine, SECRET)

    def test_the_rejection_is_still_recorded(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """Refusing silently would be indistinguishable from losing the request."""

        mutation_service.execute(
            factories.create_command(
                candidate=factories.proposal(
                    canonical_value=f"my api key is {SECRET}",
                    display_text=f"my api key is {SECRET}",
                )
            )
        )

        with engine.begin() as connection:
            operations = connection.execute(
                text("SELECT COUNT(*) FROM memory_operations")
            ).scalar()

        assert operations >= 1


class TestSensitiveContent:
    def test_sensitive_plaintext_reaches_no_table(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PRV-02 — encrypted at rest means *nowhere* in plaintext, not just the record.

        The same value passes through the record, the operation payload, the
        source excerpt and the outbox event. Each is encrypted or omitted
        separately, so each is a separate chance to leak.
        """

        result = mutation_service.execute(_sensitive_command())

        assert result.outcome is MemoryOutcome.CREATED
        _assert_absent(engine, DIAGNOSIS)

    def test_the_record_is_stored_with_ciphertext(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """The other half: absent as plaintext, but genuinely stored."""

        mutation_service.execute(_sensitive_command())

        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT canonical_payload, display_text, encrypted_canonical_payload, "
                    "encryption_algorithm FROM memory_records"
                )
            ).one()

        assert row[0] is None
        assert row[1] is None
        assert row[2] is not None
        assert row[3] == "aes-256-gcm"

    def test_sensitive_content_never_reaches_the_outbox_payload(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PRV-04 — the outbox is a queue an index worker reads without decrypting."""

        mutation_service.execute(_sensitive_command())

        with engine.begin() as connection:
            payloads = " ".join(
                str(row[0])
                for row in connection.execute(
                    text("SELECT event_payload_json FROM memory_outbox")
                ).all()
            )

        assert DIAGNOSIS not in payloads

    def test_sensitive_content_never_reaches_the_logs(
        self,
        mutation_service: MemoryMutationService,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """PRV-03 — a log file is the one store with no encryption at all.

        Captured at DEBUG across every logger, because the failure mode is an
        incidental `logger.debug(command)` somewhere in the write path.
        """

        with caplog.at_level(logging.DEBUG):
            mutation_service.execute(_sensitive_command())

        assert DIAGNOSIS not in caplog.text

    def test_prohibited_content_never_reaches_the_logs(
        self,
        mutation_service: MemoryMutationService,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The refusal path logs more than the success path, so it is worth its own case."""

        with caplog.at_level(logging.DEBUG):
            mutation_service.execute(
                factories.create_command(
                    candidate=factories.proposal(
                        canonical_value=f"my api key is {SECRET}",
                        display_text=f"my api key is {SECRET}",
                    )
                )
            )

        assert SECRET not in caplog.text


class TestForget:
    def _stored(self, mutation_service: MemoryMutationService) -> str:
        result = mutation_service.execute(factories.create_command())
        assert result.outcome is MemoryOutcome.CREATED
        return result.affected_memory_ids[0]

    def test_forget_keeps_provenance_but_removes_the_record_from_recall(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PRV-07 — forget is reversible by design, so it deliberately keeps history.

        The distinction from erase: forget must stop the fact being *used* while
        leaving enough behind to explain what happened and to block resurrection.
        """

        memory_id = self._stored(mutation_service)

        mutation_service.execute(factories.forget_command(memory_id=memory_id))

        with engine.begin() as connection:
            status = connection.execute(
                text("SELECT status FROM memory_records WHERE id = :i"), {"i": str(memory_id)}
            ).scalar()
            sources = connection.execute(
                text("SELECT COUNT(*) FROM memory_sources WHERE memory_id = :i"),
                {"i": str(memory_id)},
            ).scalar()
            tombstones = connection.execute(
                text("SELECT COUNT(*) FROM memory_tombstones")
            ).scalar()

        assert status == MemoryLifecycleState.FORGOTTEN.value
        assert sources >= 1, "provenance must survive a forget"
        assert tombstones >= 1, "a forget leaves a tombstone that blocks resurrection"

    def test_a_forgotten_record_is_not_listed_as_active(
        self, mutation_service: MemoryMutationService
    ) -> None:
        """PRV-08 — the user-visible half of forget."""

        memory_id = self._stored(mutation_service)
        mutation_service.execute(factories.forget_command(memory_id=memory_id))

        active = mutation_service.list_active_records()

        assert str(memory_id) not in {str(item.memory_id) for item in active}


class TestErasePermanently:
    def test_erase_leaves_no_trace_of_the_value(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """PRV-06 — the stronger promise, and the one a user invokes deliberately.

        Swept rather than checked per-table: "permanently" is a claim about the
        whole database, and the value passed through several tables on the way
        in. The positive control runs first so the sweep is known to work.
        """

        value = "a fact the user regrets telling neo"
        result = mutation_service.execute(
            factories.create_command(
                candidate=factories.proposal(canonical_value=value, display_text=value)
            )
        )
        memory_id = result.affected_memory_ids[0]
        assert _assert_present(engine, value)

        erased = mutation_service.execute(factories.erase_command(memory_id=memory_id))

        assert erased.outcome is not MemoryOutcome.FAILED
        _assert_absent(engine, value)

    def test_erase_leaves_no_tombstone(
        self, mutation_service: MemoryMutationService, engine: Engine
    ) -> None:
        """A tombstone is derived from the value, so keeping one would defeat the erase.

        This is the difference from forget in a single assertion: forget stores a
        digest so the fact cannot silently return, erase keeps nothing at all —
        which also means an erased fact *can* be re-stated later.
        """

        value = "another regrettable fact"
        result = mutation_service.execute(
            factories.create_command(
                candidate=factories.proposal(canonical_value=value, display_text=value)
            )
        )
        mutation_service.execute(
            factories.erase_command(memory_id=result.affected_memory_ids[0])
        )

        with engine.begin() as connection:
            tombstones = connection.execute(
                text("SELECT COUNT(*) FROM memory_tombstones")
            ).scalar()

        assert tombstones == 0

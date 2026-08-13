"""Tier 3 — database constraints (plan section SCH).

These run against a real migrated SQLite file, not a mock, because the point is
to prove the database itself refuses bad rows.  A `CheckConstraint` with a typo
in its SQL still builds the table and still passes every mock-based test — it
just never matches anything, and bad data flows in for months before anyone
notices.  The only way to know a constraint fires is to make it fire.

Each test writes a row the mutation layer would never write.  That is
deliberate: this is the last line of defence, the one that holds when a future
refactor forgets a validation somewhere above it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.models.memory import (
    DERIVED_METRIC_CODES,
    DERIVED_TARGET_STATES,
    DERIVED_TARGETS,
    OUTBOX_EVENT_KINDS,
    OUTBOX_STATES,
    RELATION_TYPES,
    SOURCE_ASSERTION_ROLES,
)
from app.models.memory import (
    MemoryDerivedMetric as DerivedMetricRow,
)
from app.models.memory import (
    MemoryDerivedState as DerivedStateRow,
)
from app.models.memory import (
    MemoryFtsDocument as FtsDocumentRow,
)
from app.models.memory import (
    MemoryOutbox as OutboxRow,
)
from app.models.memory import (
    MemoryOutboxDelivery as OutboxDeliveryRow,
)
from app.models.memory import (
    MemoryOwnerBinding as OwnerBindingRow,
)
from app.models.memory import (
    MemoryRecord as RecordRow,
)
from app.models.memory import (
    MemoryRelation as RelationRow,
)
from app.models.memory import (
    MemorySource as SourceRow,
)
from app.models.memory import (
    MemoryTombstone as TombstoneRow,
)
from app.models.memory import (
    MemoryUsageEvent as UsageEventRow,
)
from app.models.memory import (
    MemoryVectorPoint as VectorPointRow,
)
from app.services.memory.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory import factories
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID

VALID_SHA = "a" * 64
UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UUID_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _write(engine: Engine, table, **values) -> None:
    with engine.begin() as connection:
        connection.execute(insert(table).values(**values))


def _expect_rejected(engine: Engine, table, **values) -> None:
    with pytest.raises(IntegrityError):
        _write(engine, table, **values)


# ---------------------------------------------------------------------------
# Identifier shape
# ---------------------------------------------------------------------------


class TestUuidShape:
    """SCH-01 — every identifier column insists on a canonical lowercase UUID."""

    @pytest.mark.parametrize(
        "bad_id",
        [
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",  # upper case
            "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa",  # no dashes
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa",  # too short
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaaa",  # too long
            "gggggggg-gggg-4ggg-8ggg-gggggggggggg",  # non-hex
            "aaaaaaaa_aaaa_4aaa_8aaa_aaaaaaaaaaaa",  # wrong separators
            "",
        ],
    )
    def test_a_malformed_record_id_is_rejected(self, engine: Engine, bad_id: str) -> None:
        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(record_id=bad_id, operation_id=operation_id),
        )

    def test_a_malformed_owner_id_is_rejected(self, engine: Engine) -> None:
        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(owner="not-a-uuid", operation_id=operation_id),
        )

    def test_a_valid_uuid_is_accepted(self, engine: Engine) -> None:
        assert factories.insert_record(engine)


# ---------------------------------------------------------------------------
# memory_records
# ---------------------------------------------------------------------------


class TestRecordEnums:
    def test_an_unknown_memory_type_is_rejected(self, engine: Engine) -> None:
        """SCH-02"""

        operation_id = factories.insert_operation(engine)
        values = factories.record_values(operation_id=operation_id)
        values["memory_type"] = "telepathy"
        _expect_rejected(engine, RecordRow, **values)

    @pytest.mark.parametrize(
        ("column", "bad_value"),
        [
            ("cardinality", "occasional"),
            ("sensitivity", "top_secret"),
            ("status", "haunted"),
        ],
    )
    def test_each_enum_column_rejects_an_unknown_value(
        self, engine: Engine, column: str, bad_value: str
    ) -> None:
        """SCH-03"""

        operation_id = factories.insert_operation(engine)
        values = factories.record_values(operation_id=operation_id)
        values[column] = bad_value
        _expect_rejected(engine, RecordRow, **values)

    @pytest.mark.parametrize("memory_type", list(MemoryType))
    def test_every_valid_memory_type_is_accepted(
        self, engine: Engine, memory_type: MemoryType
    ) -> None:
        """SCH-02b — the check must not be narrower than the enum."""

        assert factories.insert_record(
            engine,
            memory_type=memory_type,
            slot_key=f"{memory_type.value}:global:item:{UUID_A}",
            cardinality=Cardinality.ADDITIVE,
        )

    @pytest.mark.parametrize("status", list(MemoryLifecycleState))
    def test_every_lifecycle_state_is_accepted(
        self, engine: Engine, status: MemoryLifecycleState
    ) -> None:
        """SCH-03b"""

        assert factories.insert_record(engine, status=status)


class TestRecordRanges:
    @pytest.mark.parametrize("confidence", [-0.01, 1.01, -1.0, 2.0])
    def test_confidence_outside_zero_to_one_is_rejected(
        self, engine: Engine, confidence: float
    ) -> None:
        """SCH-04"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, confidence=confidence),
        )

    @pytest.mark.parametrize("confidence", [0.0, 1.0, 0.5])
    def test_confidence_at_the_boundaries_is_accepted(
        self, engine: Engine, confidence: float
    ) -> None:
        """SCH-04b"""

        assert factories.insert_record(engine, confidence=confidence)

    @pytest.mark.parametrize("importance", [0, 11, -1, 100])
    def test_importance_outside_one_to_ten_is_rejected(
        self, engine: Engine, importance: int
    ) -> None:
        """SCH-05"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, importance=importance),
        )

    @pytest.mark.parametrize("importance", [1, 10])
    def test_importance_at_the_boundaries_is_accepted(
        self, engine: Engine, importance: int
    ) -> None:
        """SCH-05b"""

        assert factories.insert_record(engine, importance=importance)

    def test_a_negative_usage_count_is_rejected(self, engine: Engine) -> None:
        """SCH-06"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, usage_count=-1),
        )

    @pytest.mark.parametrize("revision", [0, -1])
    def test_a_non_positive_revision_is_rejected(self, engine: Engine, revision: int) -> None:
        """SCH-07"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, revision=revision),
        )

    @pytest.mark.parametrize("column", ["value_schema_version", "record_schema_version"])
    def test_a_zero_schema_version_is_rejected(self, engine: Engine, column: str) -> None:
        """SCH-08"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, **{column: 0}),
        )


class TestRecordPayloadShape:
    """The constraint that keeps sensitive facts from being stored in the clear."""

    def test_a_normal_record_needs_plaintext(self, engine: Engine) -> None:
        """SCH-09a"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, canonical_payload=None),
        )

    def test_a_normal_record_needs_display_text(self, engine: Engine) -> None:
        """SCH-09b"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, display_text=None),
        )

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_normal_record_rejects_blank_display_text(self, engine: Engine, blank: str) -> None:
        """SCH-11"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, display_text=blank),
        )

    @pytest.mark.parametrize("whitespace", ["\t", "\n", "\t\n "])
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known gap: the non-blank display-text check uses SQLite trim(), "
            "which strips spaces only. Tab- and newline-only display text "
            "therefore passes. Remove this xfail when the check also trims "
            "other whitespace."
        ),
    )
    def test_whitespace_only_display_text_is_currently_accepted(
        self, engine: Engine, whitespace: str
    ) -> None:
        """SCH-11b — a gap found while writing SCH-11, recorded not patched.

        ``length(trim(display_text)) > 0`` reads as "must not be blank", but
        SQLite's one-argument ``trim()`` removes spaces and nothing else.  A
        display text of a single tab has trimmed length 1 and is stored, so a
        memory can exist with nothing renderable to show the user.

        Cosmetic rather than dangerous — but the constraint does not mean what
        it looks like it means, which is worth knowing.
        """

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, display_text=whitespace),
        )

    @pytest.mark.parametrize(
        "column",
        [
            "encrypted_canonical_payload",
            "encrypted_display_payload",
            "encryption_algorithm",
            "encryption_key_version",
            "canonical_nonce",
            "display_nonce",
            "encryption_aad",
        ],
    )
    def test_a_normal_record_rejects_any_crypto_column(self, engine: Engine, column: str) -> None:
        """SCH-10 — a half-encrypted row is a row nobody can safely read."""

        operation_id = factories.insert_operation(engine)
        filler = b"x" if "payload" in column or "nonce" in column or "aad" in column else "x"
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, **{column: filler}),
        )

    @staticmethod
    def _sensitive_values(operation_id: str) -> dict:
        values = factories.record_values(
            operation_id=operation_id,
            sensitivity=Sensitivity.SENSITIVE,
            canonical_payload=None,
            display_text=None,
        )
        values.update(
            encrypted_canonical_payload=b"ciphertext",
            encrypted_display_payload=b"ciphertext",
            encryption_algorithm="aes-256-gcm",
            encryption_key_version="local-memory-v1",
            canonical_nonce=b"nonce12bytes",
            display_nonce=b"nonce12bytes",
            encryption_aad=b"aad",
        )
        return values

    def test_a_complete_sensitive_record_is_accepted(self, engine: Engine) -> None:
        """SCH-12"""

        operation_id = factories.insert_operation(engine)
        _write(engine, RecordRow, **self._sensitive_values(operation_id))

    @pytest.mark.parametrize(
        "missing",
        [
            "encrypted_canonical_payload",
            "encrypted_display_payload",
            "encryption_algorithm",
            "encryption_key_version",
            "canonical_nonce",
            "display_nonce",
            "encryption_aad",
        ],
    )
    def test_a_sensitive_record_missing_any_crypto_column_is_rejected(
        self, engine: Engine, missing: str
    ) -> None:
        """SCH-13 — every piece is needed to decrypt, so all-or-nothing."""

        operation_id = factories.insert_operation(engine)
        values = self._sensitive_values(operation_id)
        values[missing] = None
        _expect_rejected(engine, RecordRow, **values)

    def test_a_sensitive_record_cannot_also_hold_plaintext(self, engine: Engine) -> None:
        """SCH-12b — the whole point is that the plaintext is not there."""

        operation_id = factories.insert_operation(engine)
        values = self._sensitive_values(operation_id)
        values["canonical_payload"] = "the secret in the clear"
        _expect_rejected(engine, RecordRow, **values)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_sensitive_record_rejects_a_blank_algorithm(self, engine: Engine, blank: str) -> None:
        """SCH-13b"""

        operation_id = factories.insert_operation(engine)
        values = self._sensitive_values(operation_id)
        values["encryption_algorithm"] = blank
        _expect_rejected(engine, RecordRow, **values)


class TestExclusiveSlotUniqueness:
    """SCH-14 … SCH-17 — the index that stops two answers to one question."""

    @staticmethod
    def _exclusive(**overrides) -> dict:
        base = {
            "memory_type": MemoryType.PREFERENCE,
            "domain_key": "global",
            "slot_key": "preference:global:verbosity",
            "cardinality": Cardinality.EXCLUSIVE,
        }
        base.update(overrides)
        return base

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "REAL BUG: the unique index includes scope_project_id, which is NULL "
            "for every global-scoped record. SQL treats NULLs as distinct in a "
            "unique index, so the index never fires for global scope — where "
            "almost every memory lives. Project-scoped records are protected; "
            "global ones are not. See the docstring for the full explanation."
        ),
    )
    def test_two_active_records_cannot_share_an_exclusive_slot(self, engine: Engine) -> None:
        """SCH-14 — you cannot prefer two different verbosities at once.

        Except that at global scope, you currently can.

        ``uq_memory_records_active_exclusive_slot`` covers
        ``(owner_id, scope_type, scope_project_id, subject_key, memory_type,
        domain_key, slot_key)`` with a partial predicate of
        ``status = 'active' AND cardinality = 'exclusive'``.

        Every globally-scoped record has ``scope_project_id IS NULL``, and SQL
        unique indexes treat NULLs as distinct from each other.  Two rows that
        agree on all six other columns and are both NULL in the seventh are
        therefore *not* duplicates as far as the index is concerned, and both
        inserts succeed.

        The blast radius is the exclusive slots: your name, every preference,
        the current primary goal per domain, current employment, current
        education.  Two contradictory active records can coexist, and recall
        will surface whichever the ranking happens to prefer.

        ``TestExclusiveSlotUniqueness.test_the_slot_is_project_scoped`` shows
        the index working correctly when ``scope_project_id`` is non-NULL, which
        is what makes the NULL semantics the clear cause.

        The usual fix is a second partial index for the global case, or
        COALESCE-ing the scope column to a sentinel string inside the index.
        """

        factories.insert_record(engine, **self._exclusive())
        operation_id = factories.insert_operation(engine, idempotency_key="second")
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, **self._exclusive()),
        )

    def test_the_exclusive_slot_index_does_hold_within_a_project(self, engine: Engine) -> None:
        """SCH-14d — the control case that isolates the NULL cause above.

        Identical to the failing test in every respect except that
        ``scope_project_id`` is a real string rather than NULL.  This one passes,
        which rules out the partial predicate, the column list, and the
        migration having failed to create the index at all.
        """

        scoped = {"scope_type": "project", "scope_project_id": "alpha"}
        factories.insert_record(engine, **scoped, **self._exclusive())
        operation_id = factories.insert_operation(engine, idempotency_key="second")
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=operation_id, **scoped, **self._exclusive()),
        )

    @pytest.mark.parametrize(
        "status",
        [
            MemoryLifecycleState.SUPERSEDED,
            MemoryLifecycleState.ARCHIVED,
            MemoryLifecycleState.FORGOTTEN,
        ],
    )
    def test_an_inactive_record_frees_the_slot(
        self, engine: Engine, status: MemoryLifecycleState
    ) -> None:
        """SCH-15 — replacement works by making the old one inactive first."""

        factories.insert_record(engine, status=status, **self._exclusive())
        assert factories.insert_record(engine, **self._exclusive())

    def test_the_slot_is_owner_scoped(self, engine: Engine, other_engine: Engine) -> None:
        """SCH-16 — two people may each have their own answer."""

        factories.insert_record(engine, **self._exclusive())
        assert factories.insert_record(other_engine, owner=OTHER_OWNER_ID, **self._exclusive())

    def test_the_slot_is_project_scoped(self, engine: Engine) -> None:
        """SCH-17 — a project may hold a different preference from the global one."""

        factories.insert_record(engine, **self._exclusive())
        assert factories.insert_record(
            engine,
            scope_type="project",
            scope_project_id="alpha",
            **self._exclusive(),
        )

    def test_additive_records_may_share_a_slot_shape(self, engine: Engine) -> None:
        """SCH-14b — the index only applies to exclusive cardinality."""

        factories.insert_record(engine)
        assert factories.insert_record(engine)


class TestFingerprintUniqueness:
    def test_two_active_records_cannot_share_a_fingerprint(self, engine: Engine) -> None:
        """SCH-18 — this is the duplicate-memory guard at the storage layer."""

        fingerprint = f"sha256:{VALID_SHA}"
        factories.insert_record(engine, canonical_fingerprint=fingerprint)
        operation_id = factories.insert_operation(engine, idempotency_key="second")
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(
                operation_id=operation_id,
                canonical_fingerprint=fingerprint,
                slot_key=f"goal:global:independent:{UUID_B}",
            ),
        )

    def test_an_inactive_record_frees_the_fingerprint(self, engine: Engine) -> None:
        """SCH-19 — a forgotten fact may be learned again later."""

        fingerprint = f"sha256:{VALID_SHA}"
        factories.insert_record(
            engine,
            canonical_fingerprint=fingerprint,
            status=MemoryLifecycleState.FORGOTTEN,
        )
        assert factories.insert_record(engine, canonical_fingerprint=fingerprint)


class TestRecordForeignKeys:
    def test_a_record_cannot_reference_a_missing_operation(self, engine: Engine) -> None:
        """SCH-20a — every record traces to the write that created it."""

        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(operation_id=UUID_C),
        )

    def test_a_record_cannot_reference_another_owners_operation(self, engine: Engine) -> None:
        """SCH-20b — the composite key is what makes this impossible."""

        foreign_operation = factories.insert_operation(
            engine, owner=OTHER_OWNER_ID, idempotency_key="theirs"
        )
        _expect_rejected(
            engine,
            RecordRow,
            **factories.record_values(owner=OWNER_ID, operation_id=foreign_operation),
        )


# ---------------------------------------------------------------------------
# memory_candidates / memory_operations
# ---------------------------------------------------------------------------


class TestCandidateConstraints:
    def test_a_sensitive_candidate_requires_an_explicit_request(self, engine: Engine) -> None:
        """SCH-21 — enforced in the database, not only in the contract."""

        with pytest.raises(IntegrityError):
            factories.insert_candidate(
                engine,
                sensitivity=Sensitivity.SENSITIVE,
                explicit_user_request=False,
                canonical_payload=None,
                display_text=None,
                encrypted_canonical_payload=b"c",
                encrypted_display_payload=b"c",
                encryption_algorithm="aes-256-gcm",
                encryption_key_version="v1",
                canonical_nonce=b"n",
                display_nonce=b"n",
                encryption_aad=b"a",
            )

    def test_a_normal_candidate_is_accepted(self, engine: Engine) -> None:
        """SCH-21b"""

        assert factories.insert_candidate(engine)


class TestOperationConstraints:
    def test_an_idempotency_key_is_unique_per_owner(self, engine: Engine) -> None:
        """SCH-22 — this is what makes a retry safe."""

        factories.insert_operation(engine, idempotency_key="same-key")
        with pytest.raises(IntegrityError):
            factories.insert_operation(engine, idempotency_key="same-key")

    def test_a_normal_operation_cannot_hold_encrypted_fields(self, engine: Engine) -> None:
        """SCH-23a"""

        from app.models.memory import MemoryOperation as OperationRow

        _expect_rejected(
            engine,
            OperationRow,
            **factories.operation_values(encrypted_command_payload=b"c"),
        )

    def test_a_sensitive_operation_needs_every_crypto_field(self, engine: Engine) -> None:
        """SCH-23b"""

        from app.models.memory import MemoryOperation as OperationRow

        _expect_rejected(
            engine,
            OperationRow,
            **factories.operation_values(
                sensitivity=Sensitivity.SENSITIVE.value,
                normalized_command_json=None,
                encrypted_command_payload=b"c",
            ),
        )

    @pytest.mark.parametrize(
        ("column", "bad_value"),
        [
            ("status", "maybe"),
            ("outcome", "sort_of_created"),
            ("rejection_code", "just_because"),
            ("error_code", "oops"),
            ("operation_kind", "teleport"),
            ("actor_kind", "ghost"),
            ("source_kind", "telepathy"),
        ],
    )
    def test_each_operation_enum_rejects_an_unknown_value(
        self, engine: Engine, column: str, bad_value: str
    ) -> None:
        """SCH-24"""

        from app.models.memory import MemoryOperation as OperationRow

        _expect_rejected(engine, OperationRow, **factories.operation_values(**{column: bad_value}))


# ---------------------------------------------------------------------------
# memory_sources / memory_relations / memory_usage_events
# ---------------------------------------------------------------------------


def _source_values(engine: Engine, record_id: str, operation_id: str, **overrides) -> dict:
    values = {
        "id": overrides.pop("id", UUID_A),
        "owner_id": overrides.pop("owner", OWNER_ID),
        "memory_id": record_id,
        "source_kind": "chat_message",
        "source_content_hash": VALID_SHA,
        "observed_at": FROZEN_NOW,
        "assertion_role": "supports",
        "is_active": True,
        "operation_id": operation_id,
        "created_at": FROZEN_NOW,
        "updated_at": FROZEN_NOW,
    }
    values.update(overrides)
    return values


class TestSourceConstraints:
    @pytest.fixture
    def seeded(self, engine: Engine) -> tuple[str, str]:
        operation_id = factories.insert_operation(engine)
        record_id = factories.insert_record(engine, operation_id=operation_id)
        return record_id, operation_id

    @pytest.mark.parametrize("role", list(SOURCE_ASSERTION_ROLES))
    def test_every_assertion_role_is_accepted(
        self, engine: Engine, seeded: tuple[str, str], role: str
    ) -> None:
        """SCH-47a"""

        record_id, operation_id = seeded
        _write(
            engine,
            SourceRow,
            **_source_values(engine, record_id, operation_id, assertion_role=role),
        )

    def test_an_unknown_assertion_role_is_rejected(
        self, engine: Engine, seeded: tuple[str, str]
    ) -> None:
        """SCH-47b"""

        record_id, operation_id = seeded
        _expect_rejected(
            engine,
            SourceRow,
            **_source_values(engine, record_id, operation_id, assertion_role="vibes"),
        )

    def test_a_plaintext_only_excerpt_is_accepted(
        self, engine: Engine, seeded: tuple[str, str]
    ) -> None:
        """SCH-25a"""

        record_id, operation_id = seeded
        _write(
            engine,
            SourceRow,
            **_source_values(
                engine, record_id, operation_id, redacted_excerpt="the user said this"
            ),
        )

    def test_a_mixed_excerpt_shape_is_rejected(
        self, engine: Engine, seeded: tuple[str, str]
    ) -> None:
        """SCH-25b — plaintext and ciphertext together means one is a leak."""

        record_id, operation_id = seeded
        _expect_rejected(
            engine,
            SourceRow,
            **_source_values(
                engine,
                record_id,
                operation_id,
                redacted_excerpt="the user said this",
                encrypted_excerpt=b"ciphertext",
                excerpt_encryption_algorithm="aes-256-gcm",
                excerpt_key_version="v1",
                excerpt_nonce=b"n",
                excerpt_aad=b"a",
            ),
        )

    def test_an_incomplete_encrypted_excerpt_is_rejected(
        self, engine: Engine, seeded: tuple[str, str]
    ) -> None:
        """SCH-25c"""

        record_id, operation_id = seeded
        _expect_rejected(
            engine,
            SourceRow,
            **_source_values(engine, record_id, operation_id, encrypted_excerpt=b"ciphertext"),
        )

    def test_one_source_hash_per_record(self, engine: Engine, seeded: tuple[str, str]) -> None:
        """SCH-26 — the same message cannot vouch for one fact twice."""

        record_id, operation_id = seeded
        _write(engine, SourceRow, **_source_values(engine, record_id, operation_id))
        _expect_rejected(
            engine,
            SourceRow,
            **_source_values(engine, record_id, operation_id, id=UUID_B),
        )

    def test_sources_cascade_when_the_record_is_deleted(
        self, engine: Engine, seeded: tuple[str, str]
    ) -> None:
        """SCH-27 — an erase must not leave orphan provenance behind."""

        record_id, operation_id = seeded
        _write(engine, SourceRow, **_source_values(engine, record_id, operation_id))
        with engine.begin() as connection:
            connection.execute(delete(RecordRow).where(RecordRow.id == record_id))
            remaining = connection.execute(text("SELECT COUNT(*) FROM memory_sources")).scalar_one()
        assert remaining == 0


class TestRelationConstraints:
    @pytest.fixture
    def two_records(self, engine: Engine) -> tuple[str, str, str]:
        operation_id = factories.insert_operation(engine)
        first = factories.insert_record(engine, operation_id=operation_id)
        second = factories.insert_record(
            engine, operation_id=operation_id, slot_key=f"goal:global:independent:{UUID_B}"
        )
        return first, second, operation_id

    def test_a_relation_cannot_point_at_itself(
        self, engine: Engine, two_records: tuple[str, str, str]
    ) -> None:
        """SCH-28 — a fact cannot supersede itself."""

        first, _, operation_id = two_records
        _expect_rejected(
            engine,
            RelationRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            from_memory_id=first,
            relation_type="supersedes",
            to_memory_id=first,
            operation_id=operation_id,
            created_at=FROZEN_NOW,
        )

    @pytest.mark.parametrize("relation_type", list(RELATION_TYPES))
    def test_every_relation_type_is_accepted(
        self, engine: Engine, two_records: tuple[str, str, str], relation_type: str
    ) -> None:
        """SCH-47c"""

        first, second, operation_id = two_records
        _write(
            engine,
            RelationRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            from_memory_id=first,
            relation_type=relation_type,
            to_memory_id=second,
            operation_id=operation_id,
            created_at=FROZEN_NOW,
        )

    def test_an_unknown_relation_type_is_rejected(
        self, engine: Engine, two_records: tuple[str, str, str]
    ) -> None:
        """SCH-47d"""

        first, second, operation_id = two_records
        _expect_rejected(
            engine,
            RelationRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            from_memory_id=first,
            relation_type="reminds_me_of",
            to_memory_id=second,
            operation_id=operation_id,
            created_at=FROZEN_NOW,
        )

    def test_a_relation_is_unique_on_its_identity(
        self, engine: Engine, two_records: tuple[str, str, str]
    ) -> None:
        """SCH-29"""

        first, second, operation_id = two_records
        common = {
            "owner_id": OWNER_ID,
            "from_memory_id": first,
            "relation_type": "supersedes",
            "to_memory_id": second,
            "operation_id": operation_id,
            "created_at": FROZEN_NOW,
        }
        _write(engine, RelationRow, id=UUID_A, **common)
        _expect_rejected(engine, RelationRow, id=UUID_C, **common)

    @pytest.mark.parametrize("side", ["from_memory_id", "to_memory_id"])
    def test_relations_cascade_from_both_sides(
        self, engine: Engine, two_records: tuple[str, str, str], side: str
    ) -> None:
        """SCH-30 — deleting either end must not leave a dangling relation."""

        first, second, operation_id = two_records
        _write(
            engine,
            RelationRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            from_memory_id=first,
            relation_type="supersedes",
            to_memory_id=second,
            operation_id=operation_id,
            created_at=FROZEN_NOW,
        )
        doomed = first if side == "from_memory_id" else second
        with engine.begin() as connection:
            connection.execute(delete(RecordRow).where(RecordRow.id == doomed))
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM memory_relations")
            ).scalar_one()
        assert remaining == 0


class TestUsageEventConstraints:
    def test_a_selection_is_recorded_once(self, engine: Engine) -> None:
        """SCH-31 — replaying a request must not inflate usage counts."""

        record_id = factories.insert_record(engine)
        common = {
            "owner_id": OWNER_ID,
            "memory_id": record_id,
            "request_id": "req-1",
            "session_id": "sess-1",
            "purpose": "chat_context",
            "used_at": FROZEN_NOW,
            "created_at": FROZEN_NOW,
        }
        _write(engine, UsageEventRow, id=UUID_A, **common)
        _expect_rejected(engine, UsageEventRow, id=UUID_B, **common)

    def test_usage_events_cascade_with_the_record(self, engine: Engine) -> None:
        """SCH-31b"""

        record_id = factories.insert_record(engine)
        _write(
            engine,
            UsageEventRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            memory_id=record_id,
            request_id="req-1",
            session_id="sess-1",
            purpose="chat_context",
            used_at=FROZEN_NOW,
            created_at=FROZEN_NOW,
        )
        with engine.begin() as connection:
            connection.execute(delete(RecordRow).where(RecordRow.id == record_id))
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM memory_usage_events")
            ).scalar_one()
        assert remaining == 0


# ---------------------------------------------------------------------------
# outbox and derived state
# ---------------------------------------------------------------------------


def _outbox_values(record_id: str | None, **overrides) -> dict:
    values = {
        "id": UUID_A,
        "owner_id": OWNER_ID,
        "event_kind": "canonical_upsert",
        "memory_id": record_id,
        "event_payload_json": {},
        "state": "pending",
        "attempts": 0,
        "event_idempotency_key": "event-1",
        "created_at": FROZEN_NOW,
        "updated_at": FROZEN_NOW,
    }
    values.update(overrides)
    return values


class TestOutboxConstraints:
    @pytest.mark.parametrize("event_kind", ["canonical_upsert", "canonical_remove", "usage"])
    def test_a_canonical_event_requires_a_memory_id(self, engine: Engine, event_kind: str) -> None:
        """SCH-32 — an upsert with nothing to upsert is unprocessable."""

        _expect_rejected(engine, OutboxRow, **_outbox_values(None, event_kind=event_kind))

    @pytest.mark.parametrize("event_kind", ["tombstone_expiry", "reconciliation_request"])
    def test_a_housekeeping_event_may_omit_the_memory_id(
        self, engine: Engine, event_kind: str
    ) -> None:
        """SCH-33"""

        _write(engine, OutboxRow, **_outbox_values(None, event_kind=event_kind))

    @pytest.mark.parametrize("event_kind", list(OUTBOX_EVENT_KINDS))
    def test_every_event_kind_is_accepted(self, engine: Engine, event_kind: str) -> None:
        """SCH-47e"""

        record_id = factories.insert_record(engine)
        _write(engine, OutboxRow, **_outbox_values(record_id, event_kind=event_kind))

    def test_an_unknown_event_kind_is_rejected(self, engine: Engine) -> None:
        """SCH-47f"""

        record_id = factories.insert_record(engine)
        _expect_rejected(
            engine, OutboxRow, **_outbox_values(record_id, event_kind="please_reindex")
        )

    @pytest.mark.parametrize("state", list(OUTBOX_STATES))
    def test_every_outbox_state_is_accepted(self, engine: Engine, state: str) -> None:
        """SCH-47g"""

        record_id = factories.insert_record(engine)
        _write(engine, OutboxRow, **_outbox_values(record_id, state=state))

    def test_an_unknown_outbox_state_is_rejected(self, engine: Engine) -> None:
        """SCH-47h"""

        record_id = factories.insert_record(engine)
        _expect_rejected(engine, OutboxRow, **_outbox_values(record_id, state="thinking"))

    def test_the_idempotency_key_is_unique_per_owner(self, engine: Engine) -> None:
        """SCH-34 — the same change must not be dispatched twice."""

        record_id = factories.insert_record(engine)
        _write(engine, OutboxRow, **_outbox_values(record_id))
        _expect_rejected(engine, OutboxRow, **_outbox_values(record_id, id=UUID_B))

    def test_negative_attempts_are_rejected(self, engine: Engine) -> None:
        """SCH-35a"""

        record_id = factories.insert_record(engine)
        _expect_rejected(engine, OutboxRow, **_outbox_values(record_id, attempts=-1))

    @pytest.mark.parametrize("revision", [0, -1])
    def test_a_non_positive_canonical_revision_is_rejected(
        self, engine: Engine, revision: int
    ) -> None:
        """SCH-35b"""

        record_id = factories.insert_record(engine)
        _expect_rejected(
            engine, OutboxRow, **_outbox_values(record_id, canonical_revision=revision)
        )


class TestOutboxDeliveryConstraints:
    @pytest.fixture
    def event_id(self, engine: Engine) -> str:
        record_id = factories.insert_record(engine)
        _write(engine, OutboxRow, **_outbox_values(record_id))
        return UUID_A

    @staticmethod
    def _delivery(event_id: str, **overrides) -> dict:
        values = {
            "id": UUID_B,
            "owner_id": OWNER_ID,
            "event_id": event_id,
            "target": "fts",
            "state": "pending",
            "attempts": 0,
            "created_at": FROZEN_NOW,
            "updated_at": FROZEN_NOW,
        }
        values.update(overrides)
        return values

    def test_one_delivery_per_event_and_target(self, engine: Engine, event_id: str) -> None:
        """SCH-36"""

        _write(engine, OutboxDeliveryRow, **self._delivery(event_id))
        _expect_rejected(engine, OutboxDeliveryRow, **self._delivery(event_id, id=UUID_C))

    def test_two_targets_may_share_an_event(self, engine: Engine, event_id: str) -> None:
        """SCH-36b — one change fans out to both indexes."""

        _write(engine, OutboxDeliveryRow, **self._delivery(event_id, target="fts"))
        _write(
            engine,
            OutboxDeliveryRow,
            **self._delivery(event_id, id=UUID_C, target="vector"),
        )

    def test_deliveries_cascade_when_the_event_is_deleted(
        self, engine: Engine, event_id: str
    ) -> None:
        """SCH-37"""

        _write(engine, OutboxDeliveryRow, **self._delivery(event_id))
        with engine.begin() as connection:
            connection.execute(delete(OutboxRow).where(OutboxRow.id == event_id))
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM memory_outbox_deliveries")
            ).scalar_one()
        assert remaining == 0

    @pytest.mark.parametrize("state", list(DERIVED_TARGET_STATES))
    def test_every_derived_state_is_accepted(
        self, engine: Engine, event_id: str, state: str
    ) -> None:
        """SCH-46a"""

        _write(engine, OutboxDeliveryRow, **self._delivery(event_id, state=state))

    @pytest.mark.parametrize("target", list(DERIVED_TARGETS))
    def test_every_derived_target_is_accepted(
        self, engine: Engine, event_id: str, target: str
    ) -> None:
        """SCH-46b"""

        _write(engine, OutboxDeliveryRow, **self._delivery(event_id, target=target))

    def test_an_unknown_target_is_rejected(self, engine: Engine, event_id: str) -> None:
        """SCH-46c"""

        _expect_rejected(engine, OutboxDeliveryRow, **self._delivery(event_id, target="telepathy"))


class TestDerivedStateConstraints:
    @staticmethod
    def _state(record_id: str, **overrides) -> dict:
        values = {
            "id": UUID_A,
            "owner_id": OWNER_ID,
            "memory_id": record_id,
            "target": "fts",
            "state": "current",
            "content_hash": VALID_SHA,
            "canonical_revision": 1,
            "updated_at": FROZEN_NOW,
        }
        values.update(overrides)
        return values

    def test_one_state_row_per_memory_and_target(self, engine: Engine) -> None:
        """SCH-38"""

        record_id = factories.insert_record(engine)
        _write(engine, DerivedStateRow, **self._state(record_id))
        _expect_rejected(engine, DerivedStateRow, **self._state(record_id, id=UUID_B))

    @pytest.mark.parametrize(
        "bad_hash",
        ["abc", "A" * 64, "z" * 64, "a" * 63, "a" * 65],
    )
    def test_a_malformed_content_hash_is_rejected(self, engine: Engine, bad_hash: str) -> None:
        """SCH-39"""

        record_id = factories.insert_record(engine)
        _expect_rejected(engine, DerivedStateRow, **self._state(record_id, content_hash=bad_hash))

    def test_a_null_content_hash_is_allowed(self, engine: Engine) -> None:
        """SCH-39b — a target that has never been built has no hash yet."""

        record_id = factories.insert_record(engine)
        _write(engine, DerivedStateRow, **self._state(record_id, content_hash=None))


class TestDerivedMetricConstraints:
    @pytest.mark.parametrize("metric_code", list(DERIVED_METRIC_CODES))
    def test_every_metric_code_is_accepted(self, engine: Engine, metric_code: str) -> None:
        """SCH-40a"""

        _write(
            engine,
            DerivedMetricRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            metric_code=metric_code,
            count=0,
            updated_at=FROZEN_NOW,
        )

    def test_an_unknown_metric_code_is_rejected(self, engine: Engine) -> None:
        """SCH-40b"""

        _expect_rejected(
            engine,
            DerivedMetricRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            metric_code="vibes_dropped",
            count=0,
            updated_at=FROZEN_NOW,
        )

    def test_a_negative_count_is_rejected(self, engine: Engine) -> None:
        """SCH-40c"""

        _expect_rejected(
            engine,
            DerivedMetricRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            metric_code=DERIVED_METRIC_CODES[0],
            count=-1,
            updated_at=FROZEN_NOW,
        )

    def test_one_row_per_owner_and_code(self, engine: Engine) -> None:
        """SCH-40d"""

        common = {
            "owner_id": OWNER_ID,
            "metric_code": DERIVED_METRIC_CODES[0],
            "count": 1,
            "updated_at": FROZEN_NOW,
        }
        _write(engine, DerivedMetricRow, id=UUID_A, **common)
        _expect_rejected(engine, DerivedMetricRow, id=UUID_B, **common)


class TestDerivedDocumentConstraints:
    def test_one_fts_document_per_memory(self, engine: Engine) -> None:
        """SCH-41a"""

        record_id = factories.insert_record(engine)
        common = {
            "owner_id": OWNER_ID,
            "memory_id": record_id,
            "content_hash": VALID_SHA,
            "canonical_revision": 1,
            "memory_type": "goal",
            "domain_key": "global",
            "slot_key": factories.DEFAULT_GOAL_SLOT,
            "display_text": "improve at urban sketching",
            "derived_schema_version": "v1",
            "updated_at": FROZEN_NOW,
        }
        _write(engine, FtsDocumentRow, id=UUID_A, **common)
        _expect_rejected(engine, FtsDocumentRow, id=UUID_B, **common)

    def test_an_fts_content_hash_must_be_sha256(self, engine: Engine) -> None:
        """SCH-41b"""

        record_id = factories.insert_record(engine)
        _expect_rejected(
            engine,
            FtsDocumentRow,
            id=UUID_A,
            owner_id=OWNER_ID,
            memory_id=record_id,
            content_hash="not-a-hash",
            canonical_revision=1,
            memory_type="goal",
            domain_key="global",
            slot_key=factories.DEFAULT_GOAL_SLOT,
            display_text="x",
            derived_schema_version="v1",
            updated_at=FROZEN_NOW,
        )

    @staticmethod
    def _vector(record_id: str, **overrides) -> dict:
        values = {
            "id": UUID_A,
            "owner_id": OWNER_ID,
            "memory_id": record_id,
            "content_hash": VALID_SHA,
            "canonical_revision": 1,
            "provider": "local",
            "model": "test-embed",
            "provider_version": "v1",
            "dimension": 8,
            "vector_json": "[0,0,0,0,0,0,0,1]",
            "metadata_version": "v1",
            "derived_schema_version": "v1",
            "embedding_document_version": "v1",
            "embedding_content_hash": VALID_SHA,
            "embedding_identity_version": "v1",
            "updated_at": FROZEN_NOW,
        }
        values.update(overrides)
        return values

    def test_one_vector_point_per_memory(self, engine: Engine) -> None:
        """SCH-42a"""

        record_id = factories.insert_record(engine)
        _write(engine, VectorPointRow, **self._vector(record_id))
        _expect_rejected(engine, VectorPointRow, **self._vector(record_id, id=UUID_B))

    @pytest.mark.parametrize("dimension", [0, -1])
    def test_a_non_positive_dimension_is_rejected(self, engine: Engine, dimension: int) -> None:
        """SCH-42b"""

        record_id = factories.insert_record(engine)
        _expect_rejected(engine, VectorPointRow, **self._vector(record_id, dimension=dimension))

    @pytest.mark.parametrize("column", ["content_hash", "embedding_content_hash"])
    def test_both_vector_hashes_must_be_sha256(self, engine: Engine, column: str) -> None:
        """SCH-42c"""

        record_id = factories.insert_record(engine)
        _expect_rejected(engine, VectorPointRow, **self._vector(record_id, **{column: "nope"}))


class TestTombstoneConstraints:
    @staticmethod
    def _tombstone(operation_id: str, **overrides) -> dict:
        values = {
            "id": UUID_A,
            "owner_id": OWNER_ID,
            "fingerprint_digest": VALID_SHA,
            "fingerprint_key_version": "local-memory-v1",
            "memory_type": "goal",
            "domain_key": "global",
            "slot_key": None,
            "originating_operation_id": operation_id,
            "created_at": FROZEN_NOW,
            "expires_at": FROZEN_NOW.replace(year=FROZEN_NOW.year + 1),
            "explicitly_reconfirmed": False,
        }
        values.update(overrides)
        return values

    def test_expiry_must_follow_creation(self, engine: Engine) -> None:
        """SCH-43 — a tombstone that expired before it existed blocks nothing."""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine,
            TombstoneRow,
            **self._tombstone(operation_id, expires_at=FROZEN_NOW),
        )

    def test_one_tombstone_per_fingerprint_and_key_version(self, engine: Engine) -> None:
        """SCH-44"""

        operation_id = factories.insert_operation(engine)
        _write(engine, TombstoneRow, **self._tombstone(operation_id))
        _expect_rejected(engine, TombstoneRow, **self._tombstone(operation_id, id=UUID_B))

    def test_a_rotated_key_version_gets_its_own_row(self, engine: Engine) -> None:
        """SCH-44b — rotation must not collide with the pre-rotation tombstone."""

        operation_id = factories.insert_operation(engine)
        _write(engine, TombstoneRow, **self._tombstone(operation_id))
        _write(
            engine,
            TombstoneRow,
            **self._tombstone(operation_id, id=UUID_B, fingerprint_key_version="local-memory-v2"),
        )

    def test_an_unknown_memory_type_is_rejected(self, engine: Engine) -> None:
        """SCH-43b"""

        operation_id = factories.insert_operation(engine)
        _expect_rejected(
            engine, TombstoneRow, **self._tombstone(operation_id, memory_type="telepathy")
        )


class TestOwnerBinding:
    def test_the_database_identity_is_unique(self, engine: Engine) -> None:
        """SCH-45a — one database belongs to exactly one owner."""

        with engine.begin() as connection:
            identity = connection.execute(
                text("SELECT database_identity FROM memory_owner_bindings")
            ).scalar_one()
        _expect_rejected(
            engine,
            OwnerBindingRow,
            owner_id=OTHER_OWNER_ID,
            database_identity=identity,
            schema_version=1,
            bound_at=FROZEN_NOW,
        )

    @pytest.mark.parametrize("identity", ["", "   "])
    def test_a_blank_database_identity_is_rejected(self, engine: Engine, identity: str) -> None:
        """SCH-45b"""

        _expect_rejected(
            engine,
            OwnerBindingRow,
            owner_id=OTHER_OWNER_ID,
            database_identity=identity,
            schema_version=1,
            bound_at=FROZEN_NOW,
        )

    def test_the_migrated_database_is_bound_to_its_owner(self, engine: Engine) -> None:
        """SCH-45c — the fixture's own precondition, worth asserting once."""

        with engine.begin() as connection:
            owner = connection.execute(
                text("SELECT owner_id FROM memory_owner_bindings")
            ).scalar_one()
        assert owner == OWNER_ID


class TestConstraintsSurviveUpdates:
    """A check constraint that only fires on INSERT is half a constraint."""

    def test_an_update_cannot_push_importance_out_of_range(self, engine: Engine) -> None:
        """SCH-05c"""

        record_id = factories.insert_record(engine)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    update(RecordRow).where(RecordRow.id == record_id).values(importance=99)
                )

    def test_an_update_cannot_break_the_payload_shape(self, engine: Engine) -> None:
        """SCH-09c — clearing the plaintext of a normal record must fail."""

        record_id = factories.insert_record(engine)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    update(RecordRow)
                    .where(RecordRow.id == record_id)
                    .values(canonical_payload=None)
                )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Same root cause as SCH-14: the exclusive-slot unique index does not "
            "fire at global scope because scope_project_id is NULL."
        ),
    )
    def test_an_update_cannot_create_a_second_active_exclusive_record(self, engine: Engine) -> None:
        """SCH-14c — reactivating a superseded record must hit the index.

        The second half of the SCH-14 bug: not only can a duplicate be inserted,
        an already-superseded record can be flipped back to active alongside the
        live one.  A restore that should have been refused would go through.
        """

        exclusive = {
            "memory_type": MemoryType.PREFERENCE,
            "domain_key": "global",
            "slot_key": "preference:global:verbosity",
            "cardinality": Cardinality.EXCLUSIVE,
        }
        factories.insert_record(engine, **exclusive)
        superseded = factories.insert_record(
            engine, status=MemoryLifecycleState.SUPERSEDED, **exclusive
        )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    update(RecordRow)
                    .where(RecordRow.id == superseded)
                    .values(status=MemoryLifecycleState.ACTIVE.value)
                )

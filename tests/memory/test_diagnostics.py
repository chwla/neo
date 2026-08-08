from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from app.db.session import build_engine
from app.models.memory import MemoryOutbox, MemorySource
from app.services.memory.contracts import SourceKind
from app.services.memory.diagnostics import (
    canonical_data_checksum,
    create_sqlite_backup,
    identify_database_owner,
    inspect_memory_invariants,
    run_sqlite_integrity_check,
    schema_checksum,
)
from tests.memory.factories import DATABASE_IDENTITY, OWNER_A, operation, record, uuid_string


def _seed_healthy(engine) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                operation(number=100),
                record(number=200),
            ]
        )
        session.flush()
        session.add_all(
            [
                MemorySource(
                    id=uuid_string(300),
                    owner_id=OWNER_A,
                    memory_id=uuid_string(200),
                    source_kind=SourceKind.CHAT_MESSAGE.value,
                    source_content_hash="source-hash",
                    observed_at=datetime.now(UTC),
                    assertion_role="supports",
                    is_active=True,
                    operation_id=uuid_string(100),
                    schema_version=1,
                ),
                MemoryOutbox(
                    id=uuid_string(600),
                    owner_id=OWNER_A,
                    event_kind="canonical_upsert",
                    memory_id=uuid_string(200),
                    canonical_revision=1,
                    content_hash="content-hash",
                    event_payload_json={},
                    state="pending",
                    attempts=0,
                    event_idempotency_key="upsert-200-1",
                    schema_version=1,
                ),
            ]
        )


def test_healthy_fixture_checksums_and_invariant_report_are_deterministic(
    memory_engine,
) -> None:
    _seed_healthy(memory_engine)

    identity = identify_database_owner(memory_engine)
    assert identity.owner_id == OWNER_A
    assert identity.database_identity == DATABASE_IDENTITY
    assert run_sqlite_integrity_check(memory_engine) == ("ok",)
    assert schema_checksum(memory_engine) == schema_checksum(memory_engine)
    assert canonical_data_checksum(memory_engine, owner_id=OWNER_A) == canonical_data_checksum(
        memory_engine, owner_id=OWNER_A
    )

    report = inspect_memory_invariants(memory_engine, owner_id=OWNER_A)
    assert report.healthy
    assert report.violations == ()
    assert report.record_counts == {"active:goal": 1}
    assert report.pending_outbox == 1
    assert report.failed_outbox == 0


def test_backup_restores_to_equivalent_readable_database(memory_engine, tmp_path) -> None:
    _seed_healthy(memory_engine)
    expected_schema = schema_checksum(memory_engine)
    expected_data = canonical_data_checksum(memory_engine, owner_id=OWNER_A)
    destination = tmp_path / "backups" / "memory-backup.db"

    manifest = create_sqlite_backup(memory_engine, destination)

    assert manifest.owner_id == OWNER_A
    assert manifest.database_identity == DATABASE_IDENTITY
    assert manifest.integrity_result == ("ok",)
    assert len(manifest.sha256) == 64
    restored = build_engine(f"sqlite:///{destination}")
    try:
        assert identify_database_owner(restored).owner_id == OWNER_A
        assert schema_checksum(restored) == expected_schema
        assert canonical_data_checksum(restored, owner_id=OWNER_A) == expected_data
        assert inspect_memory_invariants(restored, owner_id=OWNER_A).healthy
    finally:
        restored.dispose()


def test_invariant_report_detects_deliberately_corrupted_rows(memory_engine) -> None:
    _seed_healthy(memory_engine)
    database_path = memory_engine.url.database
    memory_engine.dispose()

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("DROP INDEX uq_memory_records_active_exclusive_slot")
        connection.execute("DROP INDEX uq_memory_records_active_fingerprint")
        connection.execute(
            "INSERT INTO memory_records "
            "SELECT ?, owner_id, subject_key, memory_type, domain_key, slot_key, cardinality, "
            "sensitivity, canonical_payload, display_text, encrypted_canonical_payload, "
            "encrypted_display_payload, encryption_algorithm, encryption_key_version, "
            "canonical_nonce, display_nonce, encryption_aad, canonical_fingerprint, confidence, "
            "importance, status, created_at, updated_at, last_confirmed_at, expires_at, "
            "last_used_at, usage_count, pinned, created_by_operation_id, revision, metadata_json, "
            "contract_version, taxonomy_version, policy_version, value_schema_version, "
            "record_schema_version FROM memory_records WHERE id = ?",
            (uuid_string(201), uuid_string(200)),
        )
        connection.execute(
            "UPDATE memory_records SET display_text = NULL, created_by_operation_id = ? "
            "WHERE id = ?",
            (uuid_string(999), uuid_string(200)),
        )
        connection.execute(
            "UPDATE memory_sources SET memory_id = ? WHERE id = ?",
            (uuid_string(998), uuid_string(300)),
        )
        connection.commit()
    finally:
        connection.close()

    report = inspect_memory_invariants(memory_engine, owner_id=OWNER_A)
    codes = {violation.code for violation in report.violations}
    assert not report.healthy
    assert {
        "duplicate_active_exclusive_slot",
        "duplicate_active_fingerprint",
        "invalid_record_payload_shape",
        "record_missing_creating_operation",
        "orphan_or_cross_owner_source",
    } <= codes

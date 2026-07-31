"""Read-only backup and invariant primitives for memory v2 Phase 1."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine

from app.core.identifiers import canonical_uuid
from app.models.memory_v2 import (
    MemoryCandidateV2,
    MemoryLegacyMapV2,
    MemoryMigrationRunV2,
    MemoryOperationV2,
    MemoryOutboxV2,
    MemoryOwnerBindingV2,
    MemoryRecordV2,
    MemoryRelationV2,
    MemorySourceV2,
    MemoryTombstoneV2,
)

_CANONICAL_CHECKSUM_TABLES = (
    MemoryOperationV2.__table__,
    MemoryMigrationRunV2.__table__,
    MemoryRecordV2.__table__,
    MemoryCandidateV2.__table__,
    MemorySourceV2.__table__,
    MemoryRelationV2.__table__,
    MemoryTombstoneV2.__table__,
    MemoryLegacyMapV2.__table__,
)


@dataclass(frozen=True)
class DatabaseOwnerIdentity:
    owner_id: str
    database_identity: str
    database_path: str


@dataclass(frozen=True)
class SQLiteBackupManifest:
    owner_id: str
    database_identity: str
    source_path: str
    destination_path: str
    sha256: str
    integrity_result: tuple[str, ...]


@dataclass(frozen=True)
class InvariantViolation:
    code: str
    table: str
    row_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class MemoryV2InvariantReport:
    owner_id: str
    database_identity: str
    integrity_result: tuple[str, ...]
    schema_checksum: str
    canonical_data_checksum: str
    record_counts: dict[str, int]
    pending_outbox: int
    failed_outbox: int
    violations: tuple[InvariantViolation, ...]

    @property
    def healthy(self) -> bool:
        return self.integrity_result == ("ok",) and not self.violations


def _require_sqlite(engine: Engine) -> str:
    if engine.dialect.name != "sqlite":
        raise ValueError("sqlite_engine_required")
    database = engine.url.database
    if not database or database == ":memory:":
        raise ValueError("file_backed_sqlite_database_required")
    return str(Path(database).expanduser().resolve())


def identify_database_owner(engine: Engine) -> DatabaseOwnerIdentity:
    path = _require_sqlite(engine)
    with engine.connect() as connection:
        rows = connection.execute(select(MemoryOwnerBindingV2.__table__)).mappings().all()
    if len(rows) != 1:
        raise ValueError("database_requires_one_owner_binding")
    return DatabaseOwnerIdentity(
        owner_id=canonical_uuid(rows[0]["owner_id"]),
        database_identity=str(rows[0]["database_identity"]),
        database_path=path,
    )


def run_sqlite_integrity_check(engine: Engine) -> tuple[str, ...]:
    _require_sqlite(engine)
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("PRAGMA integrity_check").all()
    return tuple(str(row[0]) for row in rows)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_sqlite_backup(engine: Engine, destination: Path) -> SQLiteBackupManifest:
    identity = identify_database_owner(engine)
    source_path = Path(identity.database_path)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination == source_path:
        raise ValueError("backup_destination_must_differ_from_source")
    if destination.exists():
        raise FileExistsError(destination)

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
        integrity = tuple(str(row[0]) for row in target.execute("PRAGMA integrity_check"))
    finally:
        target.close()
        source.close()
    if integrity != ("ok",):
        raise RuntimeError("backup_integrity_check_failed")
    return SQLiteBackupManifest(
        owner_id=identity.owner_id,
        database_identity=identity.database_identity,
        source_path=str(source_path),
        destination_path=str(destination),
        sha256=_file_sha256(destination),
        integrity_result=integrity,
    )


def schema_checksum(engine: Engine) -> str:
    _require_sqlite(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT type, name, tbl_name, COALESCE(sql, '') AS sql "
                "FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name, tbl_name, sql"
            )
        ).mappings()
        material = [dict(row) for row in rows]
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_b64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def canonical_data_checksum(engine: Engine, *, owner_id: str) -> str:
    owner = canonical_uuid(owner_id)
    material: list[dict[str, Any]] = []
    with engine.connect() as connection:
        existing = set(inspect(connection).get_table_names())
        for table in _CANONICAL_CHECKSUM_TABLES:
            if table.name not in existing:
                continue
            statement = select(table).where(table.c.owner_id == owner)
            rows = [_json_safe(dict(row)) for row in connection.execute(statement).mappings()]
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
            material.append({"table": table.name, "rows": rows})
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _grouped_record_counts(connection, owner_id: str) -> dict[str, int]:
    rows = connection.execute(
        select(
            MemoryRecordV2.status,
            MemoryRecordV2.memory_type,
            func.count(MemoryRecordV2.id),
        )
        .where(MemoryRecordV2.owner_id == owner_id)
        .group_by(MemoryRecordV2.status, MemoryRecordV2.memory_type)
    ).all()
    return {f"{row.status}:{row.memory_type}": int(row[2]) for row in rows}


def _ids(rows) -> tuple[str, ...]:
    return tuple(str(row[0]) for row in rows)


def inspect_memory_v2_invariants(engine: Engine, *, owner_id: str) -> MemoryV2InvariantReport:
    owner = canonical_uuid(owner_id)
    identity = identify_database_owner(engine)
    violations: list[InvariantViolation] = []
    if identity.owner_id != owner:
        violations.append(
            InvariantViolation(
                code="owner_binding_mismatch",
                table=MemoryOwnerBindingV2.__tablename__,
                detail="Requested owner does not match the database binding.",
            )
        )

    with engine.connect() as connection:
        duplicate_slots = connection.execute(
            text(
                "SELECT group_concat(id) FROM memory_records_v2 "
                "WHERE owner_id = :owner AND status = 'active' AND cardinality = 'exclusive' "
                "GROUP BY subject_key, memory_type, domain_key, slot_key HAVING count(*) > 1"
            ),
            {"owner": owner},
        ).all()
        for row in duplicate_slots:
            violations.append(
                InvariantViolation(
                    code="duplicate_active_exclusive_slot",
                    table=MemoryRecordV2.__tablename__,
                    row_ids=tuple(str(row[0]).split(",")),
                )
            )

        duplicate_fingerprints = connection.execute(
            text(
                "SELECT group_concat(id) FROM memory_records_v2 "
                "WHERE owner_id = :owner AND status = 'active' "
                "GROUP BY canonical_fingerprint HAVING count(*) > 1"
            ),
            {"owner": owner},
        ).all()
        for row in duplicate_fingerprints:
            violations.append(
                InvariantViolation(
                    code="duplicate_active_fingerprint",
                    table=MemoryRecordV2.__tablename__,
                    row_ids=tuple(str(row[0]).split(",")),
                )
            )

        invalid_payloads = connection.execute(
            text(
                "SELECT id FROM memory_records_v2 WHERE owner_id = :owner AND NOT ("
                "(sensitivity = 'normal' AND canonical_payload IS NOT NULL "
                "AND display_text IS NOT NULL AND length(trim(display_text)) > 0 "
                "AND encrypted_canonical_payload IS NULL AND encrypted_display_payload IS NULL "
                "AND encryption_algorithm IS NULL AND encryption_key_version IS NULL "
                "AND canonical_nonce IS NULL AND display_nonce IS NULL AND encryption_aad IS NULL) "
                "OR (sensitivity = 'sensitive' AND canonical_payload IS NULL "
                "AND display_text IS NULL AND encrypted_canonical_payload IS NOT NULL "
                "AND encrypted_display_payload IS NOT NULL AND encryption_algorithm IS NOT NULL "
                "AND encryption_key_version IS NOT NULL AND canonical_nonce IS NOT NULL "
                "AND display_nonce IS NOT NULL AND encryption_aad IS NOT NULL))"
            ),
            {"owner": owner},
        ).all()
        if invalid_payloads:
            violations.append(
                InvariantViolation(
                    code="invalid_record_payload_shape",
                    table=MemoryRecordV2.__tablename__,
                    row_ids=_ids(invalid_payloads),
                )
            )

        invalid_candidates = connection.execute(
            text(
                "SELECT id FROM memory_candidates_v2 WHERE owner_id = :owner AND NOT ("
                "(sensitivity = 'normal' AND canonical_payload IS NOT NULL "
                "AND display_text IS NOT NULL AND length(trim(display_text)) > 0 "
                "AND encrypted_canonical_payload IS NULL AND encrypted_display_payload IS NULL "
                "AND encryption_algorithm IS NULL AND encryption_key_version IS NULL "
                "AND canonical_nonce IS NULL AND display_nonce IS NULL AND encryption_aad IS NULL) "
                "OR (sensitivity = 'sensitive' AND canonical_payload IS NULL "
                "AND display_text IS NULL AND encrypted_canonical_payload IS NOT NULL "
                "AND encrypted_display_payload IS NOT NULL AND encryption_algorithm IS NOT NULL "
                "AND encryption_key_version IS NOT NULL AND canonical_nonce IS NOT NULL "
                "AND display_nonce IS NOT NULL AND encryption_aad IS NOT NULL))"
            ),
            {"owner": owner},
        ).all()
        if invalid_candidates:
            violations.append(
                InvariantViolation(
                    code="invalid_candidate_payload_shape",
                    table=MemoryCandidateV2.__tablename__,
                    row_ids=_ids(invalid_candidates),
                )
            )

        missing_operations = connection.execute(
            text(
                "SELECT r.id FROM memory_records_v2 r "
                "LEFT JOIN memory_operations_v2 o ON o.id = r.created_by_operation_id "
                "AND o.owner_id = r.owner_id "
                "WHERE r.owner_id = :owner AND o.id IS NULL"
            ),
            {"owner": owner},
        ).all()
        if missing_operations:
            violations.append(
                InvariantViolation(
                    code="record_missing_creating_operation",
                    table=MemoryRecordV2.__tablename__,
                    row_ids=_ids(missing_operations),
                )
            )

        orphan_sources = connection.execute(
            text(
                "SELECT s.id FROM memory_sources_v2 s "
                "LEFT JOIN memory_records_v2 r ON r.id = s.memory_id AND r.owner_id = s.owner_id "
                "LEFT JOIN memory_operations_v2 o ON o.id = s.operation_id "
                "AND o.owner_id = s.owner_id "
                "WHERE s.owner_id = :owner AND (r.id IS NULL OR o.id IS NULL)"
            ),
            {"owner": owner},
        ).all()
        if orphan_sources:
            violations.append(
                InvariantViolation(
                    code="orphan_or_cross_owner_source",
                    table=MemorySourceV2.__tablename__,
                    row_ids=_ids(orphan_sources),
                )
            )

        invalid_relations = connection.execute(
            text(
                "SELECT rel.id FROM memory_relations_v2 rel "
                "LEFT JOIN memory_records_v2 f ON f.id = rel.from_memory_id "
                "AND f.owner_id = rel.owner_id "
                "LEFT JOIN memory_records_v2 t ON t.id = rel.to_memory_id "
                "AND t.owner_id = rel.owner_id "
                "LEFT JOIN memory_operations_v2 o ON o.id = rel.operation_id "
                "AND o.owner_id = rel.owner_id "
                "WHERE rel.owner_id = :owner AND (f.id IS NULL OR t.id IS NULL "
                "OR o.id IS NULL OR rel.from_memory_id = rel.to_memory_id)"
            ),
            {"owner": owner},
        ).all()
        if invalid_relations:
            violations.append(
                InvariantViolation(
                    code="orphan_cross_owner_or_self_relation",
                    table=MemoryRelationV2.__tablename__,
                    row_ids=_ids(invalid_relations),
                )
            )

        invalid_candidates_references = connection.execute(
            text(
                "SELECT c.id FROM memory_candidates_v2 c "
                "LEFT JOIN memory_operations_v2 o ON o.id = c.applied_operation_id "
                "AND o.owner_id = c.owner_id "
                "WHERE c.owner_id = :owner AND c.applied_operation_id IS NOT NULL AND o.id IS NULL"
            ),
            {"owner": owner},
        ).all()
        if invalid_candidates_references:
            violations.append(
                InvariantViolation(
                    code="orphan_or_cross_owner_candidate_operation",
                    table=MemoryCandidateV2.__tablename__,
                    row_ids=_ids(invalid_candidates_references),
                )
            )

        invalid_outbox_references = connection.execute(
            text(
                "SELECT e.id FROM memory_outbox_v2 e "
                "LEFT JOIN memory_records_v2 r ON r.id = e.memory_id AND r.owner_id = e.owner_id "
                "WHERE e.owner_id = :owner AND e.memory_id IS NOT NULL AND r.id IS NULL"
            ),
            {"owner": owner},
        ).all()
        if invalid_outbox_references:
            violations.append(
                InvariantViolation(
                    code="orphan_or_cross_owner_outbox_record",
                    table=MemoryOutboxV2.__tablename__,
                    row_ids=_ids(invalid_outbox_references),
                )
            )

        invalid_tombstone_references = connection.execute(
            text(
                "SELECT t.id FROM memory_tombstones_v2 t "
                "LEFT JOIN memory_operations_v2 o ON o.id = t.originating_operation_id "
                "AND o.owner_id = t.owner_id "
                "WHERE t.owner_id = :owner AND o.id IS NULL"
            ),
            {"owner": owner},
        ).all()
        if invalid_tombstone_references:
            violations.append(
                InvariantViolation(
                    code="orphan_or_cross_owner_tombstone_operation",
                    table=MemoryTombstoneV2.__tablename__,
                    row_ids=_ids(invalid_tombstone_references),
                )
            )

        invalid_legacy_references = connection.execute(
            text(
                "SELECT m.id FROM memory_legacy_map_v2 m "
                "LEFT JOIN memory_records_v2 r ON r.id = m.memory_id AND r.owner_id = m.owner_id "
                "LEFT JOIN memory_migration_runs_v2 run ON run.id = m.migration_run_id "
                "AND run.owner_id = m.owner_id WHERE m.owner_id = :owner AND "
                "((m.memory_id IS NOT NULL AND r.id IS NULL) OR "
                "(m.migration_run_id IS NOT NULL AND run.id IS NULL))"
            ),
            {"owner": owner},
        ).all()
        if invalid_legacy_references:
            violations.append(
                InvariantViolation(
                    code="orphan_or_cross_owner_legacy_map",
                    table=MemoryLegacyMapV2.__tablename__,
                    row_ids=_ids(invalid_legacy_references),
                )
            )

        pending_outbox = int(
            connection.scalar(
                select(func.count(MemoryOutboxV2.id)).where(
                    MemoryOutboxV2.owner_id == owner,
                    MemoryOutboxV2.state.in_(("pending", "processing")),
                )
            )
            or 0
        )
        failed_outbox = int(
            connection.scalar(
                select(func.count(MemoryOutboxV2.id)).where(
                    MemoryOutboxV2.owner_id == owner,
                    MemoryOutboxV2.state == "failed",
                )
            )
            or 0
        )
        counts = _grouped_record_counts(connection, owner)

    return MemoryV2InvariantReport(
        owner_id=owner,
        database_identity=identity.database_identity,
        integrity_result=run_sqlite_integrity_check(engine),
        schema_checksum=schema_checksum(engine),
        canonical_data_checksum=canonical_data_checksum(engine, owner_id=owner),
        record_counts=counts,
        pending_outbox=pending_outbox,
        failed_outbox=failed_outbox,
        violations=tuple(violations),
    )

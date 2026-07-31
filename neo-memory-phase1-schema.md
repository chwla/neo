# Neo memory redesign — Phase 1 schema decisions

Status: complete for Phase 1. This document describes persistence infrastructure only. Nothing
in Phase 1 is connected to chat, extraction, retrieval, routes, agents, background jobs, legacy
memory mutation, embeddings, FTS, or vector storage.

## Version and migration decisions

Neo did not have Alembic installed, so memory v2 uses a small explicit migration ledger with the
same Phase 1 safety properties: ordered revision IDs, a schema-DDL checksum, transactional upgrade
and downgrade entry points, idempotent re-entry, rejection of unknown or altered revisions, and
rejection of unmanaged partial v2 schemas. `MemoryV2Base` has dedicated metadata; legacy
`Base.metadata.create_all()` cannot create v2 tables. The v2 production upgrade entry point is
`upgrade_memory_v2`, not `create_all` or a conditional `ALTER TABLE` helper.

| Ledger | Revision | Effect |
|---|---|---|
| `profile_registry_migrations` | `0001_profile_owner_uuid` | Rebuilds the legacy profile registry once with a required unique stable owner UUID. |
| `memory_schema_migrations_v2` | `0001_memory_v2_phase1` | Creates the owner binding and all Phase 1 v2 tables, constraints, foreign keys, and indexes. |

The memory revision checksum is calculated from the complete SQLite `CREATE TABLE` and
`CREATE INDEX` DDL, not merely the table names. A checksum mismatch fails closed. Upgrade refuses
pre-existing unmanaged v2 tables and an applied revision with missing managed tables. Downgrade
first validates the owner/database pair, then drops only the tables owned by the Phase 1 revision
and its ledger. It does not alter or drop legacy memory data.

Future revisions must append immutable ordered migrations rather than changing revision 0001.

## Owner identity

Every v2 entity is owner-bound with a non-null canonical lowercase UUID string. UUID strings keep
SQLite storage simple and remain logically compatible with PostgreSQL native UUID columns.
Contracts and repositories normalize and validate UUIDs. Profile names, usernames, email,
database paths, and process context are never owner identity.

New registered profiles currently have an immutable UUID profile ID, so that UUID is also stored
as their owner UUID. Existing profiles with a valid immutable UUID ID retain it deterministically;
non-UUID legacy profile IDs receive a generated UUID exactly once. Renaming a username does not
change the owner. Guest stores persist a generated owner UUID inside the guest directory for the
guest store's lifetime.

`memory_owner_bindings_v2` binds each profile database to exactly one `(owner_id,
database_identity)` pair. Both the migration entry point and repository constructor validate that
pair. A missing schema, empty database identity, multiple binding rows, wrong owner, or wrong
database identity fails closed. The repository has no default-database or optional-owner path.
Database-per-profile isolation remains an additional boundary; it does not replace row ownership.

## Complete Phase 1 table set

All IDs in the following v2 domain tables are UUID strings. Every domain row has a required
`owner_id`; composite foreign keys include that owner so references cannot cross owners.

### `memory_owner_bindings_v2`

Stores the single authoritative owner/database binding: owner UUID, non-empty database identity,
schema version, and binding time. Owner is the primary key and database identity is unique.

### `memory_operations_v2`

The operation and idempotency ledger stores operation UUID, owner, owner-scoped idempotency key,
operation/actor/source kinds, actor ID, request hash, status, outcome, bounded rejection/error
codes and detail, result record IDs, contract/policy/taxonomy/schema versions, and timestamps.

Normal commands use structured JSON and have no encrypted fields. Sensitive commands use only
opaque encrypted bytes plus algorithm, key-version, nonce, and authenticated metadata; normal JSON
is SQL NULL. Prohibited sensitivity is not a legal database value. The repository also rejects
prohibited material detected in normal command JSON or bounded error detail without echoing it.

### `memory_records_v2`

The canonical record stores owner and record UUID; subject, memory type (including `employment`),
domain, slot and cardinality; sensitivity and one payload form; positive display form; canonical
fingerprint; confidence and importance; lifecycle status; creation, update, confirmation, expiry
and use timestamps; usage count; pin flag; creating operation; optimistic revision; allow-listed
metadata; contract/taxonomy/policy/value/record schema versions; and optional legacy ID.

Constraints enforce known types, cardinalities, sensitivities and lifecycle states; confidence
`0..1`; importance `1..10`; non-negative usage; positive revision/schema versions; and a valid
same-owner creating operation. Partial unique indexes enforce one active exclusive row per
`(owner_id, subject_key, memory_type, domain_key, slot_key)` and one active row per
`(owner_id, canonical_fingerprint)`. Additive rows are not subject to the exclusive-slot index.

The repository metadata allow-list is `tags`, `user_label`, and `review_note`. Metadata cannot
carry owner, sensitivity, lifecycle, version, canonical value, or lineage truth.

### `memory_candidates_v2`

Stores the typed Phase 0 proposal rather than a reasoning blob: candidate/owner identity,
subject/type/domain/slot/cardinality, sensitivity and payload form, positive display form, intent,
target hints, trusted target UUIDs, predecessor evidence, source spans, grounding evidence,
confidence, importance, explicit-request flag, extractor name/version, raw-output hash, candidate
state, bounded decision outcome/codes/reason, applied operation, timestamps, revision, and all
contract/policy/taxonomy/value/candidate schema versions.

Known intent and candidate-state constraints apply. Sensitive candidates require an explicit user
request. Prohibited candidates cannot be stored. The repository verifies each trusted target
exists for the bound owner. Candidates are not canonical facts and have no recall/index path.

### `memory_sources_v2`

Stores multiple provenance rows per canonical record: source kind and optional source,
conversation, session and message references; a bounded source span; either a permitted redacted
excerpt or opaque encrypted excerpt with algorithm/key-version/nonce/AAD; source hash; source and
observation times; extractor version; assertion role; active/detached state and reason; operation;
schema version; and timestamps.

Composite foreign keys enforce a same-owner record and operation. Excerpt checks prevent plaintext
and encrypted forms from coexisting and prevent orphan encryption metadata.

### `memory_relations_v2`

Stores owner-bound directed edges for `supersedes`, `refines`, `merged_from`, and `duplicate_of`.
Each row has its own UUID, from/to record UUIDs, creating operation, schema version, and timestamp.
Both endpoints and the operation use same-owner composite foreign keys. Self edges and duplicate
`(owner, from, relation type, to)` edges are rejected. Lineage is not represented by a single
predecessor/successor column, so one replacement can supersede multiple predecessors.

### `memory_outbox_v2`

Stores durable representations only—no worker—for canonical upsert, canonical removal, usage,
tombstone expiry, and reconciliation request events. Rows include owner, optional same-owner memory
reference, canonical revision/content hash, bounded event payload, state, attempts, retry time,
bounded last error, unique owner-scoped event idempotency key, schema version, and timestamps.

Canonical upsert/removal and usage events require a memory ID. Attempts are non-negative and a
canonical revision, when supplied, is positive. Record deletion does not cascade-delete outbox
events; Phase 2 must resolve or deliberately remove them within its transaction.

### `memory_tombstones_v2`

Stores no forgotten plaintext. A tombstone contains UUID, owner, keyed HMAC fingerprint digest,
fingerprint key version, original type, policy-permitted domain/slot, originating same-owner
operation, creation and expiration times, explicit-reconfirmation state/time, and schema version.
Expiration must follow creation, and the owner/digest/key-version tuple is unique. The repository
exposes tombstone deletion so `erase_permanently` can remove the tombstone itself. HMAC key
generation/storage is intentionally deferred to Phase 2.

### `memory_legacy_map_v2`

Maps `(owner_id, legacy table, legacy ID)` to an optional v2 record and migration run plus a known
migration outcome, bounded reason, version, and timestamps. Same-owner composite foreign keys are
used. Legacy integer IDs remain scoped source identifiers and never become globally trusted IDs.
No legacy rows are migrated in Phase 1.

### `memory_migration_runs_v2`

Tracks owner-bound future data-migration runs: run UUID, source schema fingerprint and database
identity, migration policy/taxonomy/schema versions, phase/checkpoint, status, source/result counts,
checksums, timestamps, and bounded error/report location. Counts are non-negative; phase, status,
UUIDs, and schema version are constrained. Phase 1 creates the ledger only and performs no data
migration.

### `memory_schema_migrations_v2`

The infrastructure ledger stores revision ID, immutable DDL checksum, and application timestamp.
It is not a user-memory entity and therefore is not an owner-scoped domain row.

No typed truth tables were created. No derived-index, embedding, FTS, or vector table was created.

## Payload representation

Normal records/candidates require typed canonical JSON and non-empty positive display text. Every
encrypted column must be SQL NULL. Sensitive records/candidates require SQL NULL canonical JSON
and display text and require opaque encrypted canonical/display bytes, algorithm/version,
key-version, separate nonce/IV values, and authenticated-encryption metadata. Database checks make
the forms mutually exclusive. SQLAlchemy JSON columns use `none_as_null=True` so sensitive values
cannot accidentally become a JSON `null` token in a non-null plaintext column.

The schema never accepts `sensitivity=prohibited`. The repository performs an additional Phase 0
classifier check before persisting normal command, record, candidate, source, outbox, or update
material. It raises a fixed error that contains no rejected content. Production encryption,
decryption and key providers do not exist in Phase 1. Sensitive fingerprints must be produced as
non-reversible/keyed material by Phase 2 and sensitive records remain excluded from future FTS and
vector indexing by default.

## Repository boundary

`MemoryV2Repository(session, *, owner_id, database_identity)` validates the stable UUID and the
database binding immediately. It never commits; flushes participate in the caller-owned
transaction.

Read primitives are `get_record` (explicit status set), `list_records` (explicit status set and
optional type), `find_active_slot`, `find_active_fingerprint`, and
`get_operation_by_idempotency_key`. Mutation primitives add operations, records, candidates,
sources, relations, outbox events, tombstones, migration runs, and legacy maps; update a record by
owner/ID/expected revision; and delete a tombstone by owner/ID. Cross-owner entities and references
fail as not found. Expected-revision update increments the revision atomically.

The repository does not choose duplicate, refinement, replacement, restoration, resurrection,
merge, correction, or review outcomes. It has no model, embedding, FTS, vector, network,
background-job, route, or runtime-service dependency.

## Backup and invariant diagnostics

The read-only diagnostics can identify the absolute SQLite file and its owner/database binding,
run `PRAGMA integrity_check`, create a consistent non-overwriting backup with SQLite's backup API,
and return backup integrity plus SHA-256. Schema checksums are deterministic over ordered
`sqlite_master` definitions. Canonical-data checksums are deterministic over owner-filtered,
ordered canonical/operation/provenance/migration rows with binary payloads encoded safely for the
checksum. Outbox rows are intentionally excluded from the canonical checksum.

The invariant report provides counts by lifecycle status/type, pending/processing and failed
outbox counts, and correctness/security violations for owner-binding mismatch, duplicate active
exclusive slots, duplicate active fingerprints, invalid payload/sensitivity shapes, records with
missing creating operations, orphan/cross-owner sources, and orphan/cross-owner/self relations.
It performs no repair.

## Deliberate deviations and refinements

- The finalized Phase 1 owner decision narrows Phase 0's earlier “stable nonblank string” owner
  placeholder to a canonical UUID. Phase 0 semantics are otherwise unchanged, and the Phase 0
  decision document now records this finalized type.
- `memory_owner_bindings_v2` is an additional invariant table beyond the minimum list. It is needed
  to prove the profile-database/owner pair without relying on context variables or paths.
- The repository uses a focused explicit migration ledger rather than Alembic because Neo has no
  Alembic dependency or migration environment. The ledger does not perform opportunistic repair
  and supplies the required revision, checksum, upgrade, downgrade, and corruption guarantees.
- Source excerpt encryption metadata is modeled separately from canonical payload encryption so a
  future provider can rotate or redact provenance independently without duplicating semantic
  truth.

No contradiction made the approved Phase 0 lifecycle, policy, taxonomy, or command semantics
impossible.

## Phase 2 prerequisites

Before runtime use, Phase 2 must provide transactional mutation/lifecycle validation and
idempotency replay semantics; encryption and keyed-HMAC providers with rotation and secure key
storage; sensitive fingerprint rules; tombstone creation/expiry/reconfirmation behavior; outbox
production and worker/reconciliation behavior; safe derived-index projection; and explicit
profile-owner binding at every foreground/background call site. It must also implement preflight,
backup, legacy normalization/mapping, checksums, quarantine, rollback, and invariant gates for the
actual legacy data migration. PostgreSQL-native migration execution should be added when a
PostgreSQL deployment becomes active.

Phase 2 may begin against this schema, but it must not cut over runtime traffic until those
prerequisites and their failure-path tests are complete.

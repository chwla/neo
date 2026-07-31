# Neo memory redesign: migration and rollout plan

## Goals

Migrate every profile database from the current dual representation to the canonical schema without making ambiguous or contradictory facts active, without depending on embeddings, and with a recoverable cutover. Migration is an owner-scoped use of the same deterministic mutation rules as runtime writes.

No existing table is dropped during the initial rollout. No legacy row is edited in place merely to make the migration easier.

## 1. Preconditions

Before any data migration:

1. Freeze a named legacy schema/behavior version and inventory every production database.
2. Introduce a real migration ledger and verify that every current schema variant can be read. Do not rely on `Base.metadata.create_all` or the conditional helpers in `app/db/session.py` as upgrade history.
3. Implement and pass the canonical schema, operation service, invariant checker, and migration dry-run tests before enabling canonical writes.
4. Assign every profile a stable `owner_id`. The authenticated profile record—not a value inside exported memory—is authoritative.
5. Define the initial type/domain/slot/value ontology and version it. Do not migrate while the identity contract is still changing.
6. Decide retention and deletion/tombstone policy, especially whether deleted text or only an irreversible fingerprint may be retained.

## 2. Backup and preflight

For each profile database:

1. Stop or lease-block writes for the short snapshot window.
2. Run SQLite `PRAGMA quick_check`, then use SQLite's online backup API (or `VACUUM INTO` where operationally appropriate) to produce a consistent database copy. Copying a live WAL database as a single file is not sufficient.
3. Record source path, owner ID, byte size, SHA-256 checksum, SQLite version, application version, source schema fingerprint, table row counts, maximum IDs/timestamps, and backup timestamp in a migration manifest.
4. Preserve the backup outside the directory the migration process mutates, under the product's encrypted backup/retention policy.
5. Run a read-only analyzer and write a per-owner report containing:
   - `Memory` counts by type/status/active combination;
   - typed-table counts by active/status;
   - candidates by status;
   - rows missing expected columns/values;
   - duplicate fingerprints and duplicate active slots;
   - contradictory active `Memory`/typed pairs;
   - orphan sources, embeddings, audits, and lifecycle links;
   - invalid successor/predecessor chains and cycles;
   - malformed JSON `reasoning`/embedding values;
   - FTS and embedding coverage/hash mismatch;
   - rows whose owner cannot be established from the containing profile.
6. Fail preflight on database corruption, owner ambiguity, unsupported schema, or inability to write/verify a backup. Data ambiguity is quarantinable and does not require aborting the whole owner.

## 3. Side-by-side schema

Add versioned tables under unambiguous names (`memory_records_v2`, `memory_candidates_v2`, `memory_sources_v2`, `memory_relations_v2`, `memory_operations_v2`, `memory_outbox_v2`, and derived tables). Legacy tables remain read-only during shadow migration.

Record:

- database schema version;
- migration run ID and owner;
- source schema fingerprint;
- migration policy/ontology version;
- phase/checkpoint;
- source row → new UUID mappings;
- per-row outcome/reason;
- report/checksum locations.

Generate deterministic UUIDv5 IDs for migrated legacy records from a Neo migration namespace plus `(owner_id, source_table, legacy_id, source_database_id)`. New runtime records use UUIDv7. Deterministic migration IDs make reruns idempotent. Preserve the legacy ID in `legacy_id` and a separate mapping table; do not expose it as globally unique.

## 4. Source-of-truth reconciliation

Current `Memory` is the primary seed because it contains generic lifecycle, provenance links, and recall state, but it is not blindly trusted. Typed tables are corroborating or missing-fact sources because direct answers and CRUD have treated them as independent truth.

### 4.1 Build normalized source facts

Read all legacy state into immutable intermediate records:

```text
owner
source table and ID
legacy type/status/active flags
raw and normalized value
proposed subject/type/domain/slot/cardinality/value
created/updated/source timestamps
fingerprints and links
source/provenance evidence
quality flags and parse confidence
```

The migration parser must reuse the versioned deterministic normalizers but must not call an LLM to decide active truth. An LLM may produce an offline suggestion for an operator report; it cannot alter the automated outcome.

### 4.2 Map legacy categories

| Legacy source | Canonical mapping |
|---|---|
| `memories` | Map type, positive text/value, lifecycle, confidence/importance, expiry, timestamps, provenance, and legacy ID. Derive slot only when the type policy is deterministic. |
| `profile_facts` | Map each active key to an exclusive `identity:global:<key>` slot. Inactive rows are history only when a trustworthy `Memory`/audit link exists; otherwise report/quarantine. |
| `preferences` | Map category/value to a typed preference dimension and domain. Never map a topic preference to `response_style:global` solely because the value describes answer format. |
| `goals` | Map active/current goals to versioned goal roles. Preserve abandoned/completed status as historical metadata or archived state, not active personal truth. |
| `projects` | Map independently identifiable projects as additive records; preserve project status and typed value. Chat organization projects that are not user-memory facts remain outside personal memory. |
| `education` | Map structured fields and active/current semantics. |
| `activities` | Map recurrence/time/category, archive expired records according to explicit policy. |
| `events` | Map structured time/location; expired events are archived or excluded according to event policy, never recalled as current by default. |
| `memory_candidates` | Preserve pending candidates only if their JSON and source are valid; convert to v2 `needs_review`. Accepted candidates are represented through their accepted record/operation, not reapplied. |
| `memory_sources` | Map to owner-bound v2 provenance and detach state. Orphans are quarantined. |
| lifecycle audits/links | Reconstruct operations and supersedes relations when the link is acyclic and same owner. Invalid history is reported without changing active selection. |
| embeddings/FTS | Do not migrate as truth. Discard and rebuild from validated canonical active rows. |

### 4.3 Canonical positive-value validation

Any row whose text contains a correction sentence combining a new assertion and a negated old assertion is not migrated verbatim. Deterministic clause parsing may extract a positive value only when the source span and type are unambiguous. Otherwise the row becomes a `needs_review` migration candidate and is excluded from recall.

Examples of quarantine reasons:

- no stable type/domain/slot;
- mixed positive/negative canonical text;
- invalid or ungrounded structured attributes;
- category likely wrong (topic preference stored as global response style);
- malformed candidate JSON;
- lifecycle flags disagree (`is_active=true`, status deleted/superseded);
- successor missing, cross-owner, cyclic, or older without evidence;
- typed and generic values conflict with no trustworthy chronology;
- source belongs to an unresolvable profile.

Quarantine is loss-avoiding: retain the raw legacy reference and reason in the migration report/review candidate, but do not put ambiguous text into active recall.

## 5. Contradiction resolution

Group normalized facts by `(owner, subject, type, domain, slot)`.

Automated active selection is allowed only in this order:

1. A valid explicit legacy supersession chain with exactly one active terminal successor wins.
2. A valid explicit user correction source linked to the newer positive record wins and supersedes matched predecessors.
3. Exact/paraphrased typed and generic duplicates merge provenance into one active record.
4. A current typed record and `Memory` with identical canonical value merge, taking the best provenance and latest confirmation—not two records.
5. For additive types, independent entity-stable facts may all remain active.
6. If two different values occupy an exclusive slot and no explicit lifecycle/correction evidence selects one, activate neither automatically. Create `needs_review` candidates and a conflict group report. Do **not** choose merely by latest timestamp, highest confidence, vector similarity, or typed-table precedence.

When a valid replacement supersedes several legacy actives, create one new active record plus one `supersedes` relation per predecessor. This preserves history that the current single `supersedes_id` field cannot express.

Deleted legacy facts become tombstones/history under the chosen privacy policy and enqueue derived deletions. They are never reintroduced from a matching typed row or import without explicit reconfirmation.

## 6. Migration execution

For each owner, in resumable bounded batches:

1. Acquire an owner/database migration lease.
2. Re-read and compare the source schema fingerprint/checkpoint to preflight. Abort if legacy data changed unexpectedly.
3. Insert a `migration` operation with deterministic idempotency key for each normalized record or conflict group.
4. Execute the canonical mutation planner in migration mode. Migration mode may preserve historical timestamps and legacy IDs, but may not bypass owner, positive-value, lifecycle, exclusivity, or lineage constraints.
5. Write canonical records, sources, relations, operation outcomes, and outbox rows in the same transaction.
6. Commit a bounded batch and checkpoint source IDs plus cumulative counts/checksum. A rerun must return the same results.
7. Run owner-level invariant validation before marking canonical data ready.

Do not make network embedding calls during migration transactions. Indexing begins only after canonical validation.

## 7. Derived-index rebuild

1. Drop/recreate or logically version the v2 FTS index.
2. Enumerate only authorized, active, unexpired canonical records.
3. Generate the canonical content hash from the versioned serialization used by recall.
4. Upsert FTS and vector entries with `(owner_id, memory_id, content_hash, model/version)`.
5. Record success/failure in outbox/rebuild checkpoints. Vector outages do not fail canonical migration.
6. Reconcile counts and hashes. Any index entry lacking an active matching canonical row is deleted; any active row lacking the current hash remains pending/degraded.
7. Run cross-owner collision tests against the built index before enabling semantic recall.

## 8. Validation report and release gates

Each owner and the fleet summary must report:

- source rows by table and disposition: migrated active, migrated history, merged duplicate, quarantined, intentionally excluded, failed;
- v2 records by type/status;
- exact mappings from legacy IDs to UUIDs;
- every contradiction group and chosen/non-chosen reason;
- every category/domain/slot rewrite;
- all records normalized from mixed negation and all records quarantined for it;
- active exclusive-slot uniqueness violations (must be zero);
- active duplicate fingerprints (must be zero);
- invalid/cross-owner/orphan relations and sources (must be zero);
- active records with missing create operation/provenance (must be zero unless an explicitly documented synthetic migration source);
- inactive records returned by v2 recall probes (must be zero);
- owner A records returned/suppressing owner B probes (must be zero);
- FTS/vector current, pending, failed, stale, and ghost counts;
- deterministic direct lookup parity and intentionally changed legacy behavior;
- broad-recall and critical correction fixtures;
- database integrity and before/after canonical dataset checksums.

Release cannot proceed while a correctness/security invariant is non-zero. Vector coverage may be below 100% only when degraded lexical operation is verified and the shortfall is visible/retrying.

## 9. Compatibility and rollout stages

### Stage 0 — Read-only analyzer

Ship schema inventory, backup, normalization preview, and reports. Change no runtime behavior. Sample real anonymized reports and finalize ontology/policy.

### Stage 1 — V2 shadow schema and mutation kernel

Create v2 tables and operation service behind feature flags. Run unit/contract/concurrency tests. Legacy remains authoritative. Do not dual-write from production paths yet; an incomplete dual-write would create another divergence source.

### Stage 2 — Offline/shadow migration

Populate v2 side by side from a consistent legacy snapshot. Build derived indexes. Keep v2 non-serving. Fix migration software and rerun idempotently; do not hand-edit migrated rows.

### Stage 3 — Shadow reads

For selected profiles, execute v2 recall after the production legacy response and compare IDs/types/domains/status, limits, and intended answer facts. Do not inject shadow results or expose raw content in logs. Categorize differences as intended correction, migration ambiguity, or defect.

### Stage 4 — Single canonical write cutover

During a short owner-scoped maintenance window:

1. block/queue chat and memory mutations;
2. take and verify a final backup;
3. migrate the delta since the shadow checkpoint;
4. validate invariants;
5. flip all mutation entry points to v2 adapters together;
6. make legacy tables read-only at the repository boundary;
7. resume writes.

There must not be a period in which some runtime surfaces write legacy and others write v2. Compatibility endpoints must already call `MemoryMutationService`.

### Stage 5 — Read cutover canary

Enable deterministic direct lookups and lexical v2 recall first, then semantic ranking after derived validation. Canary by profile with automatic rollback triggers for owner mismatch, invariant violation, error-rate/latency thresholds, or inactive recall. Compare normal plan-generation fixtures and broad recall.

### Stage 6 — Full rollout and observation

Expand by cohorts. Keep legacy tables and backups read-only for at least one release/retention window. Run continuous invariant/index reconciliation and review quarantine UX/metrics.

### Stage 7 — Legacy retirement

After the rollback window and explicit approval:

- export final mapping/reports;
- remove legacy read adapters and direct store methods;
- remove typed writable tables or convert them to SQL views;
- delete obsolete FTS/embedding data under recoverable/retention procedures;
- update the schema compatibility floor.

This is a separate destructive change, not part of initial migration.

## 10. Rollback

### Before write cutover

Disable v2 flags and discard/rebuild side-by-side v2 tables. Legacy has not changed, so rollback is immediate. Preserve failed migration reports for diagnosis.

### During the cutover window

If final validation fails, keep writes blocked, restore the verified SQLite backup atomically at the profile-database level, verify its checksum/integrity, reset the feature flag to legacy, and then resume. Do not partially reverse individual v2 rows.

### After v2 writes have been accepted

A blind switch back to the old database would lose new user mutations. Choose one of:

1. **Preferred:** fix-forward v2 while semantic/vector features are disabled and deterministic/lexical service continues.
2. **Full rollback:** stop writes, export committed v2 operations after the cutover watermark, restore the legacy backup, replay those operations through a separately tested reverse compatibility adapter, validate, then reopen.

The reverse adapter must be implemented and rehearsal-tested before production cutover if full rollback is a release requirement. Never use unverified dual-write as rollback insurance.

Rollback triggers include owner leakage/mismatch, two active records in an exclusive slot, recall of inactive data, unrecoverable canonical write errors, migration checksum mismatch, and unexplained data-loss counts. Vector outage alone is not a rollback trigger because lexical degraded mode is required.

## 11. Handling ongoing edits and background jobs

- Pause legacy aging/compression/repair jobs before the final delta and leave them disabled after v2 cutover.
- Drain or invalidate legacy embedding work; v2 rebuilds from canonical hashes.
- Chat/message edits submitted during a maintenance window are queued with idempotency keys and replayed through v2 after the cutover.
- Long-running chat generations capture a memory schema/generation watermark. Their eventual extraction command is applied to the current v2 slot state, not to their stale pre-cutover snapshot.
- Imports started before cutover either finish entirely in legacy before the final delta or are canceled and resubmitted to v2; never split a batch across authorities.

## 12. Migration acceptance criteria

Migration is complete only when:

1. Every production profile has a verified backup and final report.
2. Every source row has a recorded disposition; there are no silent drops.
3. All v2 canonical invariants pass with zero owner/lifecycle/slot violations.
4. Ambiguous/conflicting/malformed items are quarantined and absent from recall.
5. The exact long-form-video → short-reels correction has one active new goal and complete supersession history.
6. All runtime write surfaces call the v2 mutation service; legacy tables are read-only.
7. Direct answers, broad recall, and plan generation read only v2 canonical records.
8. Lexical degraded operation passes with vectors unavailable.
9. Derived coverage is measured and retrying; stale/ghost entries do not affect results.
10. Canary and rollback rehearsals pass, including operations committed after the backup watermark.

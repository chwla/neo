# Neo memory redesign: implementation plan

## Delivery rule

Build correctness in dependency order: contracts and schema, deterministic mutation, path convergence, extraction, recall/indexing, migration, then rollout and legacy removal. Do not begin a later phase to patch around a missing invariant from an earlier one. Existing tests stay intact; new v2 tests are added alongside them.

This document is a plan only. No production implementation is included in this design task.

## Proposed component layout

Names can follow repository conventions, but responsibilities must remain separated:

```text
app/models/memory_v2.py                 canonical ORM records/relations/operations/outbox
app/models/memory_candidate_v2.py       typed candidate persistence if kept separate
app/services/memory_v2/contracts.py     command/result/value schemas and error codes
app/services/memory_v2/taxonomy.py      versioned type/domain/slot/cardinality policies
app/services/memory_v2/normalization.py positive typed values and fingerprints
app/services/memory_v2/planner.py       duplicate/refinement/conflict/target plan
app/services/memory_v2/mutations.py     sole canonical transaction service
app/services/memory_v2/queries.py       owner/status-bound canonical queries
app/services/memory_v2/extraction.py    model proposal + deterministic admission
app/services/memory_v2/recall.py        lookup, hybrid scoring, limits, result contract
app/services/memory_v2/prompt.py        untrusted memory serialization
app/services/memory_v2/outbox.py        derived-index event processor/retry
app/services/memory_v2/indexes.py       FTS/vector adapters and reconciliation
app/services/memory_v2/maintenance.py   dry-run invariant checks and command proposals
app/services/memory_v2/migration.py     read-only analysis and idempotent migration runner
app/repositories/memory_v2.py           narrow owner-bound SQL repository used by services
migrations/...                          versioned schema migrations
tests/memory_v2/...                     unit/contract/integration/security/migration/e2e
```

`app/services/memory_retrieval/` and `app/services/context_memory/` remain separate. Rename route prefixes or service labels only through a dedicated compatibility change; do not import their stores into personal-memory v2.

## Phase 0 — Freeze contracts, evidence, and safety rails

### Work

- Record the current green test baseline and preserve the exact critical correction as a new expected-v2 fixture.
- Add architecture tests/instrumentation capable of detecting direct writes to legacy personal-memory tables after cutover.
- Inventory current schema variants and all API/agent consumers.
- Resolve the blocking product policies listed in `neo-memory-redesign-spec.md:16`: sensitive data, delete retention, ambiguous conflicts, pins, incognito/guest, source retention, and initial ontology.
- Define stable operation outcomes/error codes, policy versions, and observability fields.
- Create feature flags for v2 schema, shadow migration, shadow read, canonical write, lexical recall, vector recall, and legacy compatibility. Flags must be owner/cohort scoped and fail closed on owner mismatch.

### Files/components

- Add `tests/memory_v2/fixtures/`, contract fixtures, and architecture checks.
- Add versioned config/contracts under `app/services/memory_v2/` without connecting runtime paths.
- Document API response compatibility in the API schema layer used by `app/api/routes/memory.py`.

### Dependencies

None. Product decisions must finish before taxonomy/schema freeze.

### Acceptance criteria

- Existing 51 tests remain unchanged and green.
- The exact implicit correction is captured as a v2 failing contract test, not “fixed” in legacy code.
- Every current mutation/retrieval surface from `neo-memory-current-state.md` has a named owner and migration adapter.
- Operation/error/policy versions and feature-flag rollback behavior are approved.

## Phase 1 — Versioned schema and owner-bound repository

### Work

- Add Alembic or an equivalent explicit migration ledger; stop using create-all/conditional ALTER as an upgrade mechanism for v2.
- Create canonical records, candidates, sources, relations, operations, outbox, owner-safe legacy mapping, and derived metadata tables.
- Add enums/check constraints, owner-aware foreign keys, active fingerprint uniqueness, and partial active exclusive-slot uniqueness.
- Assign and persist stable owner IDs to profile accounts. Require the authenticated owner on repository construction and every method.
- Implement backup/integrity utilities and an invariant checker before importing data.

### Files/components

- New model/migration files above.
- `app/services/profile_accounts.py`: stable owner ID and owner/database binding.
- `app/main.py`: personal-memory requests must have valid profile context; no default-database fallthrough for these routes.
- `app/db/session.py`: run versioned migrations; retain legacy helpers only for supported pre-v2 upgrade ingestion.
- New `app/repositories/memory_v2.py`.

### Dependencies

Phase 0 contracts and product policy.

### Acceptance criteria

- Empty and every known legacy-schema fixture upgrade reproducibly to the v2 schema.
- Database rejects two active exclusive records and every cross-owner relation/source.
- Repository has no owner-optional runtime query.
- Backup/checksum/restore rehearsal succeeds.
- Schema and repository tests pass on SQLite; PostgreSQL compatibility tests pass if CI provides it.

## Phase 2 — Deterministic normalization, planning, and mutation kernel

### Work

- Implement versioned subject/type/domain/slot taxonomy and typed values.
- Implement positive-canonical-value validation, fingerprints, exact/typed paraphrase comparison, correction target matching, and tombstone/lineage checks.
- Implement `MemoryCommand`, idempotency ledger, dry-run planner, and the eight operation semantics.
- Implement short transaction/lock/retry behavior, optimistic revisions, multi-predecessor supersession, provenance, audit, and outbox creation.
- Implement restore-as-replacement and explicit rejection of unsafe restore.
- Make index adapters unavailable to the mutation transaction so network calls cannot accidentally occur before commit.

### Files/components

- New `contracts.py`, `taxonomy.py`, `normalization.py`, `planner.py`, `mutations.py`.
- New command contract/concurrency tests from matrix sections A-C and F.
- Do not modify legacy `MemoryReviewService` to emulate v2; it remains isolated until adapters switch.

### Dependencies

Phase 1 schema/repository.

### Acceptance criteria

- A01-C11 and F01, F05-F10 pass against canonical SQL without embeddings.
- The critical correction creates one clean new active goal and supersedes every predecessor atomically.
- Canonical values never contain the negated old clause.
- Exact duplicate, refinement, conflict, merge, delete, archive, restore, and resurrection rules return stable outcomes.
- Concurrent writes cannot create two active values in an exclusive slot.
- No canonical table is written outside the mutation repository in v2 tests.

## Phase 3 — Converge every mutation surface

### Work

Build adapters that translate existing requests to `MemoryCommand`; keep response shapes where an actual consumer requires them.

1. Candidate review and explicit direct memory commands.
2. Generic `/memories` create/update/delete/lifecycle.
3. Typed profile/preference/goal/project/education/activity/event routes.
4. Chat sync and stream extraction orchestration.
5. Message edit/rerun/chat deletion source changes.
6. `/conversation` and `/extract-memory`.
7. Imports, agent tools, audits, aging, and consolidation.

All adapters must pass an authenticated owner and idempotency key. Remove internal commits from `NeoChatService.persist_user_memory`; mutation service owns its transaction. Sync and stream orchestration must call the same shared method rather than duplicate sequences.

### Files/components

- `app/api/routes/memory.py`: adapt all routes listed in the current-state audit.
- `app/services/chat.py`: shared sync/stream memory orchestration, source-change commands, no opportunistic legacy repair.
- `app/services/review.py`: candidate decisions become adapter input; category-specific SQL writes retire.
- `app/repositories/memory_store.py`: legacy path read-only behind flag; typed CRUD no longer runtime writer after cutover.
- `app/services/lifecycle.py` and `lifecycle_maintenance.py`: emit commands/dry-run plans; no direct lifecycle edits.
- Agent/import tool locations found during Phase 0: construct the same contracts.

### Dependencies

Phase 2 complete and stable. This phase initially runs v2 in non-serving/shadow mode.

### Acceptance criteria

- G13-G16 and H09-H10 pass.
- Contract tests submit equivalent operations through every surface and observe the same canonical result.
- Static/architecture tests find no direct runtime INSERT/UPDATE/DELETE to v2 tables outside `MemoryMutationService`/repository.
- Sync and stream chat have identical memory behavior.
- Manual edits recompute canonical value/fingerprint and enqueue derived updates.
- Legacy tables can be switched read-only without breaking enabled v2 mutation paths.

## Phase 4 — Structured extraction and correction planner

### Work

- Replace free-form candidate attributes in `reasoning` with versioned typed proposal fields.
- Implement speaker/durability/negation/correction/source-span pre-parsing.
- Define the strict model JSON schema and model-version telemetry.
- Deterministically validate grounding, positive values, sensitive policy, type/domain/slot, target hints, and automatic candidate cap.
- Make malformed/ambiguous output a no-op or `needs_review`, never a generic auto-accepted fallback.
- Ensure current-turn corrections suppress contradictory recalled facts even if durable application is asynchronous.
- Select pre-response or post-turn extraction behavior per product decision and expose operation outcome to UI where appropriate.

### Files/components

- New `app/services/memory_v2/extraction.py`.
- `app/services/extraction.py`: compatibility wrapper only once enabled; retire giant category-specific mutation behavior rather than keep both.
- `app/services/memory_intent.py`: produce typed intent/retraction signals or be replaced by v2 pre-parser.
- `app/services/memory_scope.py`: replace last-token/domain heuristics with versioned taxonomy; remove global/topic duplication.
- `app/services/chat.py`: call one extraction coordinator.

### Dependencies

Phase 2 mutation semantics; Phase 3 adapter contract. Can be developed in parallel with later parts of Phase 3 only while not serving writes.

### Acceptance criteria

- B01-B13 and E01-E11 pass across deterministic and model-fixture extractors.
- Exact brief correction passes without the words “Correction,” “replace,” or “I now prefer.”
- “Clearly” cannot become a domain or response-style preference in that case.
- Malformed/model-invented/assistant/temporary/sensitive facts never become active.
- Model outage does not produce unexpected regex-created active facts.

## Phase 5 — Canonical queries, bounded recall, and secure prompt integration

### Work

- Implement owner/status/expiry-bound deterministic typed lookup and a single recall result model.
- Replace typed-table direct answers with canonical query adapters.
- Implement FTS/BM25 scoring, domain/type filters, diversity, thresholds, bounded pin/importance/confidence/recency/usage contributions, five-record and token budgets.
- Serialize a separate delimited untrusted memory-context message. Remove recalled text from stable system-policy instructions.
- Emit usage events only for final injected IDs.
- Apply memory-enabled/incognito gates before direct answer, retrieval, extraction, usage, or background work.

### Files/components

- New `queries.py`, `recall.py`, and `prompt.py`.
- `app/services/retrieval.py`: becomes compatibility adapter to one recall service; remove parallel typed/generic result logic.
- `app/services/direct_answer.py`: query canonical typed values; remove hard-coded domain answer paths where canonical lookup suffices.
- `app/services/context.py`: consume v2 recall result.
- `app/services/chat.py`: safe prompt message and current-turn precedence.
- `app/services/research/memory_scope.py`, `app/services/research/jobs.py`, and `app/services/research/planner.py`: require the job's authenticated owner, call the canonical recall service, and serialize memory as untrusted context; remove global `SessionLocal` personal-memory reads.
- `app/services/archives.py` and `POST /conversation`: keep archive recall disabled unless an owner-bound canonical archive manifest, tenant-filtered Qdrant query, and deletion/retention contract are implemented; never mix ownerless archive hits into personal context.
- `app/repositories/memory_store.py`: legacy FTS/search no longer serving when v2 flag is active.

### Dependencies

Phases 2-4. Lexical recall can ship before semantic indexing.

### Acceptance criteria

- D01-D03, D08-D16, G01, G03-G08, and G17-G18 pass with vectors disabled.
- Normal recall returns only active, owner-bound, unexpired records and at most configured budgets.
- Direct saved-memory answer and plan generation use the same canonical IDs.
- Prompt-injection fixture cannot alter system/tool policy.
- Incognito produces zero memory repository/vector/outbox calls.

## Phase 6 — Derived-index outbox, semantic recall, and reconciliation

### Work

- Implement post-commit outbox leasing, retry/backoff, idempotent FTS/vector upsert/delete, dead-letter visibility, and content-hash reconciliation.
- Keep provider calls outside canonical transactions.
- Join semantic candidates back to canonical owner/status/hash before scoring.
- Implement complete rebuild and per-owner/global coverage reports.
- Make vector failures degrade to lexical/deterministic recall without changing mutation outcome.

### Files/components

- New `outbox.py`, `indexes.py`, provider adapter(s), worker entry point.
- Replace `MemoryStore._sync_memory_embedding`, `_mark_embedding_stale`, lazy FTS backfill, and direct embedding calls for v2.
- `app/services/embeddings.py`: provider only; no canonical lifecycle responsibilities.
- Operational health/maintenance routes expose state but mutate through commands/outbox controls.

### Dependencies

Phase 5 canonical recall and Phase 2 outbox events.

### Acceptance criteria

- D04-D07, F02-F04, F11-F12, G02, and H08 pass.
- Canonical write latency/transaction does not include embedding network time.
- Stale, ghost, wrong-owner, inactive, and hash-mismatched hits cannot be returned or suppress valid results.
- Full index deletion/rebuild yields equivalent authorized recall within scoring tolerances.
- Old outbox age and failure metrics trigger operational alerts.

## Phase 7 — Migration tooling and rehearsal

### Work

- Implement the read-only analyzer, immutable normalized intermediate representation, deterministic ID map, conflict grouping, quarantine, batch checkpoints, reports, backup/restore, and index rebuild described in `neo-memory-migration-plan.md`.
- Create fixture databases for every schema variant and corruption class.
- Rehearse pre-cutover and post-cutover rollback including operation replay after watermark.
- Run shadow migration and shadow-read comparison on representative profiles.

### Files/components

- New `migration.py`, CLI/admin entry points, migration manifests/reports.
- Versioned database migrations.
- `tests/memory_v2/migration/` and anonymized fixture generators.
- Feature-flag/shadow comparison telemetry; never log raw memory text.

### Dependencies

Phases 1-6 stable. Migration must use the production mutation/normalization contracts, not copies.

### Acceptance criteria

- H01-H12 pass.
- Every legacy row receives a disposition and mapping; ambiguous contradictions are absent from recall.
- Repeated migration produces identical UUIDs, checksums, and relations.
- Critical correction, broad recall, plan generation, owner collision, and vector outage pass on migrated data.
- Backup and both rollback modes are successfully rehearsed.

## Phase 8 — Canary cutover and operational validation

### Work

- Pause legacy maintenance, take final verified backup, migrate delta, validate, and flip all write paths together by owner.
- Make legacy tables repository-read-only.
- Enable v2 deterministic/lexical reads first; shadow-compare and then enable semantic ranking.
- Canary with automated stop/rollback conditions, expand cohorts, and monitor invariant/index/security metrics.
- Provide review UI/export for quarantined records without exposing them to normal recall.

### Files/components

- Deployment/configuration, feature-flag, admin reporting, and monitoring definitions.
- No emergency path may write legacy and v2 independently.

### Dependencies

Phase 7 release report and rollback approval.

### Acceptance criteria

- Zero owner mismatch/leak, inactive recall, and active exclusive-slot violations.
- Error and latency budgets pass with vector healthy and unavailable.
- Mutation outcomes and source/index lag are observable.
- Product signs off intended behavior changes and quarantine rate.
- Rollout can be stopped per owner without losing post-backup operations.

## Phase 9 — Legacy removal (separate reviewed change)

### Work

- After at least one successful rollback/retention window, remove direct legacy write/read code and hard-coded repair logic.
- Replace typed tables with canonical query views or remove them after external-dependency verification.
- Remove duplicate sync/stream orchestration, old candidate JSON reasoning protocol, direct FTS/vector side effects, and legacy maintenance mutations.
- Rename/document the unrelated workspace/context memory routes to make subsystem boundaries explicit.
- Drop old tables/indexes only with a fresh backup, report, and explicit destructive-change approval.

### Files/components to retire or substantially reduce

- `app/repositories/memory_store.py` personal-memory typed/generic mutation and legacy search methods.
- Category mutation branches in `app/services/review.py`.
- Conflict/tombstone/domain heuristics in `app/services/conflicts.py`, `memory_scope.py`, and legacy parts of `lifecycle.py`.
- Duplicate retrieval/direct-answer paths in `app/services/retrieval.py` and `direct_answer.py`.
- Opportunistic repair and post-turn duplicate extraction in `app/services/chat.py`.
- Hand-written v2 upgrade logic in `app/db/session.py` (legacy ingestion support may remain offline only).

### Dependencies

Successful Phase 8 observation window, confirmed external API consumers, and destructive-operation approval.

### Acceptance criteria

- Static search and architecture tests prove one personal-memory writer and one recall service.
- No runtime dependency reads legacy personal-memory tables.
- Full v2 matrix and retained legacy API compatibility tests pass.
- Final backup/mapping report is verified before any drop.

## Exact build and validation order

1. Approve policy/ontology and freeze command/error contracts.
2. Add versioned schema, stable owners, constraints, backup, and invariant checker.
3. Build owner-bound repository.
4. Build typed normalizers and stable slot identity.
5. Build mutation planner and transaction/idempotency kernel.
6. Validate duplicate/refinement/conflict/lifecycle/concurrency without embeddings.
7. Adapt every write surface to the kernel and prove path parity.
8. Build structured extraction and pass the exact implicit correction/negation/category suite.
9. Build canonical deterministic/lexical recall and secure prompt serialization.
10. Replace typed-table direct answers and plan context with canonical queries.
11. Add incognito/memory-disabled gates across the whole flow.
12. Build derived outbox/vector integration, stale-hit joins, degraded mode, and reconciliation.
13. Build/rehearse migration, reports, backup, and rollback.
14. Shadow migrate and shadow read.
15. Cut over all writes for a canary owner; then lexical reads; then semantic ranking.
16. Expand cohorts only while invariants/security/latency remain green.
17. Retain legacy data read-only through the rollback window.
18. Remove old paths/tables in a separately reviewed destructive change.

## Principal implementation risks

- **Ontology instability:** a changing domain/slot contract invalidates identity and migration. Mitigate with a small versioned taxonomy and `needs_review` fallback.
- **Legacy ambiguity/data loss:** typed and generic state may conflict without evidence. Mitigate with quarantine and per-row disposition, never latest-row guessing.
- **SQLite contention:** broad/slow transactions would harm chat. Mitigate with short mutation transactions, no embeddings inside them, bounded batches, busy retry, and concurrency tests.
- **Partial path cutover:** one forgotten route recreates dual truth. Mitigate with adapters switched together, repository read-only guards, static checks, and parity tests.
- **Prompt behavior regressions:** bounded canonical recall may change answers users relied on. Mitigate with shadow comparisons and explicit intended-difference review, not by restoring stale data.
- **Sensitive history retention:** supersession/audit and deletion policy may conflict with privacy expectations. Resolve before schema/data migration and minimize raw source content.
- **Derived backlog:** vector outage during migration/rollout can create large queues. Make lexical mode sufficient, rate-limit workers, expose age/coverage, and rehearse rebuild.
- **Long-running generation races:** extraction from stale chat state can overwrite a newer fact. Use current-slot resolution, expected revisions, idempotency, and queue watermarks.
- **Rollback after new writes:** restoring a snapshot alone loses operations. Build and rehearse the operation-watermark replay before cutover.

# Neo memory v2 Phase 6 derived indexing and semantic recall

Status: implemented behind disabled-by-default, owner-cohort flags; pending independent validation.
This checkpoint does not authorize rollout.

## Authority and transaction boundary

Canonical SQL remains the only authority. Phase 2 mutation planning and transactions still commit
canonical rows and the existing `memory_outbox_v2` events atomically. They do not import or call an
embedding provider, FTS adapter, vector adapter, or Phase 6 worker. A post-commit worker first leases
per-target delivery rows in a short SQL transaction; only after that transaction closes does it
reload owner-bound canonical state, build an approved derived document, and call an index/provider.
The worker and maintenance services have no canonical lifecycle update capability.

Migration `0002_memory_v2_phase6_derived_indexes` extends the explicit ledger with:

- `memory_outbox_deliveries_v2`, independently tracking FTS/vector attempts and leases;
- `memory_derived_state_v2`, independently tracking current/stale/failed/deleted policy state;
- `memory_derived_metrics_v2`, storing owner-bound enum counters without candidate content;
- `memory_fts_documents_v2`, a separate personal-memory-v2 lexical namespace; and
- `memory_vector_points_v2`, an owner/memory-keyed SQLite vector namespace.

The original outbox event kinds, event table, canonical revision, canonical fingerprint, and
idempotency protocol are retained. Derived tables are reconstructible and intentionally do not
become authorization or canonical truth.

## Derived documents and indexes

`DerivedDocumentBuilder` accepts only active, unexpired, normal-sensitivity records with an
approved display value. Its SHA-256 content hash covers the exact stable JSON representation of
owner ID, memory ID, canonical revision, memory type, domain, slot, approved display text, and
derived schema version. It excludes sources, provenance, operation payloads, reasoning, and raw
canonical payloads. Sensitive records are `not_applicable`; prohibited inputs produce no canonical
record or derived event.

The FTS adapter is isolated from legacy FTS and exposes owner-scoped idempotent upsert/delete,
bounded search, metadata enumeration, clear, and health. Its candidates remain untrusted and are
rejoined through canonical SQL; adapter failure falls back to Phase 5 canonical lexical recall.
The selected vector backend is the existing profile SQLite database, avoiding new infrastructure
while retaining deterministic, owner-keyed uniqueness. Exact cosine search streams owner rows and
retains only a bounded top-k heap with stable score/ID ordering. A point carries owner/memory IDs,
derived hash, canonical revision, provider/model/version/dimension, vector-metadata and
derived-schema versions, the embedding-document version/hash, embedding-identity version, and
vector. Frozen hash fixtures cover both the derived document and the exact embedding document. An
expected-hash delete cannot remove a newer point, including when the old delete reaches the worker
after replacement.

The Phase 6 embedding wrapper is provider-only. It accepts bounded approved text, validates numeric
finite output and exact dimension, exposes provider/model/version/health, and maps failures to stable
codes. It cannot access canonical rows, indexes, lifecycle methods, or outbox state.

## Delivery, retry, and recovery

Eligible per-target states are leased using an atomic conditional update with worker ID, lease
timestamps, expiry, and incremented attempt. Active leases are exclusive; expired leases are
recoverable. Retry uses versioned deterministic exponential backoff bounded by configured base and
maximum delays. Failed targets retry independently, so `fts=current, vector=failed` is valid and a
vector retry does not repeat the completed FTS delivery. The configured dead-letter threshold is
independently bounded by the maximum-attempt ceiling and moves a target to visible `dead_letter`;
only the explicit idempotent requeue control resets it.

Every upsert reloads the event's owner-bound canonical row and validates lifecycle, expiry,
revision, canonical fingerprint, and target eligibility. Advanced canonical state makes the old
event obsolete and schedules an idempotent current repair. Removal is owner/memory keyed and
expected-hash guarded. A removal older than current derived revision is completed as not applicable,
so it cannot erase a replacement. Errors store bounded codes only—never text, vectors, payloads,
provider bodies, or secrets.

## Recall and degraded operation

Deterministic and broad Phase 5 paths never generate query embeddings. Scoped lexical recall may
use the derived lexical adapter but always retains bounded canonical lexical fallback. Optional
semantic recall requires all owner-scoped flags and a sufficiently tokenized query. Each vector hit
is treated as untrusted: owner is checked, canonical state is fetched through the authenticated
repository, lifecycle/expiry/type/domain/sensitivity are enforced, and derived hash, revision, and
provider identity must match. Missing, inactive, wrong-owner, and stale hits are dropped before
budgeting or usage and queue target-specific delete/upsert repairs. A sensitive or otherwise
policy-ineligible canonical hit also queues immediate expected-hash deletion of the derived point
while leaving canonical state and usage untouched.

Scoring policy `neo.memory.semantic-hybrid.v1` normalizes cosine similarity to `[0, 1]`, applies a
configured cap and threshold, and blends it with the complete Phase 5 score using a bounded default
weight of `0.35`. Domain gates, current-turn suppression, canonical-ID/slot deduplication, stable
tie-breaking, the five-record maximum, and the character budget remain authoritative. Usage is
recorded only for final serialized canonical IDs.

Embedding health/query failures or vector search failures set a structured degraded reason and
return lexical Phase 5 results. FTS failures set lexical degradation while canonical lexical
fallback and validated semantic retrieval continue. With both derived systems unavailable,
deterministic and bounded canonical behavior remains. Provider availability never affects canonical
write commit or foreground latency.

## Reconciliation, rebuild, and health

Owner-scoped reconciliation compares canonical eligible documents with both derived metadata sets.
It reports and target-queues repairs for missing/stale rows, ghost/inactive/expired rows, embedding
identity/dimension drift, owner metadata mismatch, pending-already-current work, and done events
whose derived state is missing. Queue-repair mode is the default; callers must explicitly request
`dry_run=True` for a report-only pass. Canonical, FTS-metadata, and vector-metadata enumeration each
use the same bounded page limit and advance independently through an opaque versioned checkpoint.
The report exposes all three per-page counts; a checkpoint is complete only after every lane is
exhausted. This allows derived-only ghosts beyond the current canonical page to be found without an
unbounded metadata scan. A second run after processing is clean.

Owner rebuild captures a canonical checksum, clears only that owner's two derived namespaces,
marks eligible per-target state pending, queues deterministic upserts, and returns the unchanged
checksum, cleared counts, expected derived checksum, and pending-target count. Post-drain
verification compares canonical, FTS, and vector counts and hashes. Global rebuild accepts only
explicitly authorized owner-scoped maintainers; global reconciliation accepts bounded limits and
owner-keyed opaque checkpoints so every owner can resume independently. Unknown checkpoint owners
fail closed. Neither interface adds an owner-optional runtime repository method.

Coverage reports contain counts and stable codes only: canonical eligible, FTS/vector
current/missing/stale/not-applicable, outbox pending/processing/failed/dead-letter, oldest age,
maximum attempts, expired leases, ghost/security/stale-hit counters, provider health and
provider/model/version coverage, degraded status, rollout readiness, and alert codes. Current alert
thresholds cover oldest pending age, dead-letter count, coverage ratio, provider health, stale/ghost
rate configuration, and lease expiration rate configuration. Current/missing/stale coverage is
computed from persisted index metadata rather than trusting status rows alone, while semantic
wrong-owner/stale/ghost/inactive counters persist as bounded owner-scoped enum metrics.

The default-off `/api/memory-v2/derived/health`, `/reconcile`, and `/rebuild` routes require a
server-side authenticated profile session and an enabled-owner cohort match. They never accept an
owner ID from the request body or path. Reconcile and rebuild can mutate only reconstructible
derived state and existing outbox repair work. No global maintenance route is registered; the
privileged global service remains an explicit administrative interface.

## Acceptance evidence

| Row | Executable evidence |
| --- | --- |
| D04 | `test_stale_superseded_predecessor_is_dropped_for_active_successor` |
| D05, G02 | `test_ghost_and_wrong_owner_hits_do_not_consume_or_suppress_local_result` |
| D06 | `test_hash_mismatch_is_rejected_and_current_upsert_repair_is_scheduled` |
| D07 | `test_embedding_and_vector_outages_degrade_to_lexical_and_deterministic` |
| F02 | `test_all_derived_failures_leave_canonical_committed_and_outbox_retryable` |
| F03 | `test_pending_survives_restart_and_active_lease_is_exclusive` |
| F04 | `test_vector_upsert_crash_retry_is_idempotent` |
| F11 | `test_fts_success_vector_failure_is_independent_and_retryable` |
| F12 | `test_missing_vector_reconciliation_repairs_once_and_second_run_is_clean` |
| H08 | `test_provider_outage_keeps_canonical_and_fts_correct_with_pending_vector` |

The same focused suite directly covers lease expiry, active-lease exclusion, dead-letter/requeue,
expected-hash deletion, deterministic hashes, finite/dimension validation, structured provider
health, bounded top-k vector search, exact-model Ollama health, ineligible semantic cleanup,
domain/duplicate hybrid behavior, current-turn suppression, usage/budget parity, bounded composite
reconciliation, derived-only ghosts, privileged global checkpoints, rebuild equivalence, privacy
gates, default-off flags, operational authorization, and the Phase 7 boundary.

## Flags and phase boundary

Outbox worker, FTS, vector, semantic recall, reconciliation, and derived-health flags all default to
`False`. Semantic requires vector; FTS/vector/reconciliation require the worker; every Phase 6 flag
requires owner-bound canonical query enablement, and reconciliation/health require both derived
indexes. Runtime dependencies fail closed outside the enabled owner cohort. Legacy behavior remains
unchanged while these flags are disabled. When v2 indexing owns derived work for the bound owner, a
database-binding-aware legacy `MemoryStore` guard prevents lazy legacy FTS/embedding synchronization
across chat, API, review, and import paths without deleting legacy code.

No legacy-data migration, manifest, shadow reads, canary, production cutover, taxonomy change,
legacy removal, archive redesign, or other Phase 7 behavior is included.

## Local validation checkpoint

The final local audit passed 70 focused Phase 6 tests, 389 complete memory-v2 tests, and 461 full
repository tests with the seven pre-existing deprecation warnings. Ruff, format verification, and
`git diff --check` passed, and the disposable manual validator printed
`phase6_fixture_validation=PASS`. Phase 6 remains pending independent validation; these results do
not enable rollout or declare the phase complete.

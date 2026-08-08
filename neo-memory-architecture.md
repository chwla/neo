# Neo Memory Architecture

Neo has one personal-memory system. It is local, profile-scoped, owner-bound, and authoritative in SQL. Lexical and vector indexes are reconstructible acceleration layers; neither is an authority for lifecycle state.

## Request flow

The normal synchronous and streaming chat paths use the same sequence:

```text
authenticated profile
→ current user message
→ deterministic current-turn contradiction analysis
→ owner-bound canonical recall
→ bounded, injection-safe memory serialization
→ assistant generation
→ structured extraction after the response
→ canonical mutation transaction
→ post-commit outbox indexing
```

Current-turn corrections suppress contradicted older records before prompt construction. Extraction runs after generation and cannot corrupt or delay the assistant response. Direct answers, chat, and research consume canonical IDs from the same recall service. Usage is recorded only for the IDs that survived suppression and prompt budgeting.

When memory is disabled or incognito is selected, chat and research do not construct the personal-memory runtime and perform no personal-memory reads, writes, extraction, usage accounting, or indexing. These gates also apply to chat edit, rerun, and deletion source maintenance. When vector infrastructure is unavailable, recall continues through deterministic and SQLite FTS paths.

Each memory-aware user turn stores only text-free diagnostic IDs in chat-message metadata: recalled, current-turn-suppressed, and final serialized canonical IDs. The read-only inspector joins those persisted diagnostics to operations, candidates, usage events, sources, and outbox rows; it does not reconstruct suppression from current state.

## Components

- `app/services/memory/` owns contracts, normalization, policy, extraction, mutation coordination, recall, prompt serialization, source changes, outbox delivery, indexes, and reconciliation.
- `app/models/memory.py` defines the dedicated canonical schema.
- `app/repositories/memory.py` is the single owner-bound canonical repository.
- `app/db/memory_migrations.py` installs the final schema through a checksum ledger.
- `app/services/memory_chat.py` builds request-owned recall for chat and research.
- `app/api/routes/memory.py` exposes the user-facing Memory API.
- `scripts/memory_index_worker.py` processes committed outbox work.
- `scripts/inspect_memory.py` performs read-only inspection of the UI profile.
- `scripts/reset_memory.py` is the explicitly confirmed destructive replacement tool.

## Canonical tables

```text
memory_owner_bindings
memory_operations
memory_records
memory_candidates
memory_sources
memory_relations
memory_usage_events
memory_outbox
memory_tombstones
memory_outbox_deliveries
memory_health_state
memory_health_metrics
memory_fts_documents
memory_vector_points
memory_schema_migrations
memory_fts_index
```

SQLite creates internal shadow tables for the `memory_fts_index` virtual table. They are derived state and may be rebuilt.

## Lifecycle and authority

Active, superseded, archived, and forgotten states live only in `memory_records`. Exclusive slots permit one active record; an authoritative correction creates the successor and supersedes the predecessor atomically. Uncertain corrections remain candidates. Forgetting clears recoverable canonical payloads and emits canonical-removal work. Sources are independently detachable; removing one source preserves an active record when another active support remains.

Sensitive payloads use profile-derived local encryption. Prohibited material is rejected before durable persistence. Owner and database identity are checked at schema binding, repository construction, recall, mutation, worker, API, and inspection boundaries.

## API

```text
GET    /api/memory
GET    /api/memory/{memory_id}
POST   /api/memory
PATCH  /api/memory/{memory_id}
DELETE /api/memory/{memory_id}
GET    /api/memory/candidates
POST   /api/memory/candidates/{candidate_id}/accept
POST   /api/memory/candidates/{candidate_id}/reject
GET    /api/memory/health
```

The authenticated profile supplies the owner identity. Request bodies cannot select an owner. All writes use the canonical mutation coordinator.

The pre-existing workspace-artifact index is not personal memory: it stores redacted project, task, run, and research artifacts and does not participate in canonical personal-memory lifecycle or chat recall. Its preserved API was moved to `/api/workspace-memory/*`, so no non-canonical route remains under `/api/memory`.

## Destructive replacement boundary

There is no old-data migration, compatibility read, dual write, shadow read, or rollback path. `scripts/reset_memory.py` recognizes an explicit allowlist of retired and canonical memory tables, refuses unknown personal-memory table names, fingerprints every unrelated table before and after, drops only allowlisted targets, and installs an empty final schema. It creates no backup or export.

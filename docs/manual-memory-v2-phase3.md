# Manual validation: memory v2 Phase 3 write convergence

This validates flagged, owner-bound write-path parity. It does **not** validate natural-language
extraction, normal conversational recall, derived indexes, legacy migration, or production cutover.

## Command

From the repository root:

```bash
.venv/bin/python scripts/manual_memory_v2_phase3.py --keep
```

The script creates a new directory under the operating system's temporary directory. It supplies
that exact directory as `memory_v2_disposable_database_root`; no profile registry, default Neo
database, or real profile path is used.

## Expected output

The output begins with `disposable_root=` and `disposable_database=` and prints typed compatibility
results for:

- generic create;
- typed goal create;
- candidate-review replacement;
- sync and streaming structured chat candidates;
- explicit forget and rejected unsafe restore;
- accepted structured import;
- persisted source deletion while another support remains, including a typed result and SQL proof;
- cross-owner rejection;
- incognito zero-call behavior;
- disabled-feature legacy mode;
- persisted idempotent replay;
- equivalent replacement through generic, review, and chat adapters.

It then prints the owner binding, records, relations, sources, operations, tombstones, outbox, and
legacy table counts. Success ends with:

```text
phase3_manual_validation=PASS
artifacts_retained=/tmp/.../neo-memory-v2-phase3-...
cleanup_command=rm -rf -- /tmp/.../neo-memory-v2-phase3-...
```

Any failed invariant exits nonzero and retains the directory for inspection.

## Expected canonical state

For the critical replacement:

- old value: `create long-form cinematic YouTube videos`;
- current value: `create short Instagram reels clearly`;
- one active `video_creation` goal in the inherited
  `goal:video_creation:current_primary_goal` exclusive slot;
- one superseded predecessor;
- one `supersedes` relation from the new record to the predecessor;
- positive canonical/display text without a negated old clause;
- sync and stream retries share one operation ID;
- no duplicate active record or logical outbox entry;
- no rows added to legacy typed-memory tables.

The forgotten learning goal has an opaque 30-day tombstone and an unsafe direct restore is rejected.
Imported owner, status, lineage, and canonical IDs are ignored.

For `source_delete_with_other_support`, expect a non-null typed result containing
`action=detach_source`, `outcome=preserved`, `review_required=false`, the memory and detached source
IDs, `remaining_active_source_count=1`, and `canonical_mutation_performed=false`. The following
`source_delete_with_other_support_proof` line is derived from a fresh SQL read and shows the requested
source inactive, a different supporting source active, the canonical memory still active, and equal
before/after revisions. The validator also asserts that the operation count and canonical-removal
outbox count did not change.

## SQL inspection

Use the printed `disposable_database` path:

```bash
sqlite3 /printed/path/neo.db \
  "SELECT owner_id, database_identity FROM memory_owner_bindings_v2;"
sqlite3 /printed/path/neo.db \
  "SELECT id, memory_type, domain_key, slot_key, canonical_payload, status, revision FROM memory_records_v2 ORDER BY status, id;"
sqlite3 /printed/path/neo.db \
  "SELECT relation_type, from_memory_id, to_memory_id, operation_id FROM memory_relations_v2;"
sqlite3 /printed/path/neo.db \
  "SELECT id, source_kind, source_id, message_id, memory_id, assertion_role, is_active, detachment_reason FROM memory_sources_v2;"
sqlite3 /printed/path/neo.db \
  "SELECT idempotency_key, operation_kind, status, outcome, rejection_code, error_code FROM memory_operations_v2;"
sqlite3 /printed/path/neo.db \
  "SELECT memory_type, domain_key, slot_key, expires_at FROM memory_tombstones_v2;"
sqlite3 /printed/path/neo.db \
  "SELECT event_kind, memory_id, canonical_revision, state FROM memory_outbox_v2;"
sqlite3 /printed/path/neo.db \
  "SELECT (SELECT count(*) FROM memories), (SELECT count(*) FROM goals), (SELECT count(*) FROM preferences);"
```

## Cleanup

Run the exact `cleanup_command` printed by the script. Without `--keep`, cleanup occurs automatically.

Do not use ordinary conversational prompt testing as a Phase 3 gate. It becomes meaningful after
Phase 4 extraction and Phase 5 recall are complete.

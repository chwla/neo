# Manual validation: memory v2 Phase 2

This is an isolated mutation-kernel test. It is not a production chat, extraction,
retrieval, or UI test. The script uses a deterministic test-only crypto provider and
must never be pointed at a Neo profile database.

## Run

From the repository root:

```bash
.venv/bin/python scripts/manual_memory_v2_phase2.py --keep
```

The script creates a new directory named `neo-memory-v2-phase2-*` under the operating
system's temporary directory, creates `manual-phase2.sqlite3` inside it, applies the
Phase 1 migration, and prints the exact disposable path. It refuses an explicitly
supplied `--database` path if that file already exists. Omit `--keep` to clean up
automatically after the run.

A successful run exits zero and ends with:

```text
PASS: all Phase 2 manual invariants held
KEPT FOR INSPECTION: <temporary-directory>
```

Expected result summaries include:

- critical create: `created`;
- critical replace and exact replay: the same `replaced` result and active ID;
- idempotency-key reuse with changed content: `idempotency_conflict`;
- forget: `forgotten`, followed by `resurrection_blocked`;
- archived historical goal plus occupied slot: `invalid_restore`;
- two-connection exclusive-slot race: one `created` and one `needs_review`.

Thread scheduling can change which concurrency command wins, but never the outcome set
or the one-active-row invariant.

## Expected rows by scenario

After critical replacement, the video slot has two history rows: one `superseded`
predecessor at revision 2 and one `active` replacement at revision 1. The replacement
value is exactly `create short Instagram reels clearly`. One `supersedes` relation,
supporting and retraction provenance, one predecessor removal event, and one replacement
upsert event exist. Exact replay adds no rows.

After forget, the learning row is `forgotten` at revision 2, its plaintext canonical and
display columns are null, its provenance is inactive, one owner-bound HMAC tombstone has
a 30-day expiry, and `canonical_remove` plus `tombstone_expiry` events are pending. The
automatic recreation command is a durable rejection and adds no canonical row.

After unsafe restore, the finance history row is `archived`, the newer finance row is
`active`, and the restore operation is durably rejected with `invalid_restore`.

After the race, the health-and-fitness slot contains exactly one active row. Both
operation-ledger rows exist: one committed create and one deterministic review outcome.

## SQL inspection

Replace `<db>` with the printed `manual-phase2.sqlite3` path:

```bash
sqlite3 <db>
```

Then run:

```sql
.headers on
.mode column

SELECT id, memory_type, domain_key, slot_key, status, revision,
       canonical_payload, display_text
FROM memory_records_v2
ORDER BY created_at;

SELECT relation_type, from_memory_id, to_memory_id
FROM memory_relations_v2
ORDER BY created_at;

SELECT memory_id, assertion_role, is_active, detachment_reason,
       redacted_excerpt
FROM memory_sources_v2
ORDER BY created_at;

SELECT id, owner_id, substr(fingerprint_digest, 1, 16) AS digest_prefix,
       fingerprint_key_version, expires_at, explicitly_reconfirmed
FROM memory_tombstones_v2;

SELECT idempotency_key, operation_kind, status, outcome,
       rejection_code, error_code, result_record_ids
FROM memory_operations_v2
ORDER BY created_at;

SELECT event_kind, memory_id, canonical_revision, state,
       event_idempotency_key
FROM memory_outbox_v2
ORDER BY created_at;

SELECT slot_key, count(*) AS active_count
FROM memory_records_v2
WHERE status = 'active'
GROUP BY slot_key
HAVING count(*) > 1;
```

The final query must return no rows.

## Failure interpretation

- Exit 2 means the requested database path already existed or its parent did not exist;
  this is a safety refusal, not a kernel failure.
- Exit 1 means a command outcome or SQL invariant differed from the required Phase 2
  behavior. The script prints the exception type without SQL or secret payload text.
- `database_temporarily_unavailable` in the race means local SQLite locking exceeded the
  bounded retry window; rerun once and investigate filesystem/database locking if it
  repeats.
- Missing Phase 1 tables or migration checksum errors mean the schema is not the
  authoritative Phase 1 schema expected by this kernel.

## Cleanup

After inspection, remove only the exact temporary directory printed by the script:

```bash
rm -rf <printed-neo-memory-v2-phase2-temporary-directory>
```

Do not substitute a profile, repository, home, or other broad directory.

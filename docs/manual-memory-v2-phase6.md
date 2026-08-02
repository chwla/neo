# Neo memory v2 Phase 6 manual validation

Phase 6 validation uses only fresh disposable SQLite databases and dedicated
`memory_fts_documents_v2` / `memory_vector_points_v2` namespaces. It never opens the normal Neo
profile or browser.

Run the deterministic fixture battery with:

```bash
.venv/bin/python scripts/manual_memory_v2_phase6.py --keep
```

The validator covers canonical commit before indexing, independent FTS/vector delivery, retry,
lease-expiry crash recovery, idempotent vector upsert, stale/ghost/hash/wrong-owner rejection,
degraded lexical and deterministic recall, reconciliation, owner rebuild, sensitivity and request
gates, dead-letter requeue, current-turn suppression, usage parity, health alerts, disabled
production defaults, and the Phase 7 boundary. Output contains only bounded counters, IDs, and
booleans. It exits nonzero on any failed invariant and prints the retained database, derived
namespace, and exact cleanup command.

The optional live probe sends only this synthetic string to the configured local provider:
`synthetic non-personal phase six embedding probe`.

```bash
.venv/bin/python scripts/manual_memory_v2_phase6.py \
  --probe-live-embeddings \
  --provider ollama \
  --endpoint 'http://127.0.0.1:11434' \
  --model 'nomic-embed-text:latest'
```

The probe uses the repository's existing Ollama `/api/embeddings` adapter. It reports reachability,
model availability, success, dimension, finite-vector validation, provider/model, and latency. It
does not print text, vectors, provider bodies, or private data and is never part of automated tests.

The standalone worker rejects manual owner/database selection unless
`--disposable-maintenance` is present and the SQLite file resolves beneath `--disposable-root`.
Production operation derives the owner and database identity from the v2 binding and requires the
owner-scoped worker flag. Batch, lease, attempt, poll, and worker-ID arguments are bounded and
validated before leasing.

```bash
.venv/bin/python scripts/memory_v2_index_worker.py --once
```

When explicitly enabled, derived health, reconciliation, and rebuild controls are exposed only at
the authenticated owner-scoped `/api/memory-v2/derived/*` routes. They accept no owner selector and
cannot mutate canonical lifecycle state. No global maintenance HTTP route is installed.
Reconciliation returns an opaque `v1` checkpoint that independently pages canonical, FTS, and
vector metadata. Pass `next_checkpoint` back unchanged until it is `null`; each lane is bounded by
the requested limit and derived-only ghosts are therefore covered without an all-owner scan.

After inspection, run the validator's printed cleanup command. Retained artifacts are validation
fixtures, not migration inputs or production profiles.

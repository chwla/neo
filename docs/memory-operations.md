# Memory Operations

All commands below run from the repository root. The default target is the sole local account profile—the same database selected by the Neo UI. Supply `--profile-id` when more than one account exists.

## Start Neo

Backend:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Frontend:

```bash
cd frontend
npm run dev
```

Index worker:

```bash
.venv/bin/python scripts/memory_index_worker.py
```

The frontend is at `http://127.0.0.1:5173` and the backend is at `http://127.0.0.1:8000`. The worker is asynchronous: canonical writes commit before indexing, pending work survives restart, and vector failure does not prevent FTS processing or lexical recall.

Default memory settings are enabled:

```text
memory_enabled=true
memory_extraction_enabled=true
memory_index_worker_enabled=true
memory_semantic_recall_enabled=true
```

In the current local profile, chat and structured extraction resolve to `qwen3-coder:30b`; the embedding model is `nomic-embed-text:latest`. Local configuration may override either value.

## Read-only inspection

```bash
.venv/bin/python scripts/inspect_memory.py summary
.venv/bin/python scripts/inspect_memory.py records
.venv/bin/python scripts/inspect_memory.py candidates
.venv/bin/python scripts/inspect_memory.py relations
.venv/bin/python scripts/inspect_memory.py usage
.venv/bin/python scripts/inspect_memory.py outbox
.venv/bin/python scripts/inspect_memory.py derived
.venv/bin/python scripts/inspect_memory.py conversation --id <conversation-id>
```

The inspector opens SQLite in read-only, query-only mode. It does not run extraction or recall, change usage, process the outbox, call a language model, or call embeddings. Sensitive payloads are redacted.

Conversation inspection reads persisted, text-free per-turn diagnostics. It reports recalled IDs separately from current-turn-suppressed IDs and final serialized IDs, then joins the real usage-event and outbox IDs.

## Destructive reset

This permanently erases all personal-memory rows and indexes in the current UI profile and creates an empty canonical schema. It does not create a backup or export.

```bash
.venv/bin/python scripts/reset_memory.py --confirm ERASE_ALL_MEMORY
```

The command prints the exact profile ID and absolute database path before it begins. Any other confirmation string is rejected. The reset refuses unknown personal-memory table names and verifies that unrelated tables are byte-content/schema equivalent before and after.

## Health and recovery

- `GET /api/memory/health` reports canonical/derived coverage.
- Restarting `scripts/memory_index_worker.py` recovers pending or expired leases.
- `POST /api/memory/derived/reconcile` compares canonical and derived state.
- `POST /api/memory/derived/rebuild` reconstructs derived state for the authenticated owner.
- If embeddings or Ollama are unavailable, leave the worker running; lexical recall remains available while vector deliveries retry according to policy.

The separate Workspace Retrieval screen indexes non-personal workspace artifacts under `/api/workspace-memory`. It is not a source of canonical personal-memory records and does not share the `/api/memory` namespace.

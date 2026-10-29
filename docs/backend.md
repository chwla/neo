# Backend guide

Technical reference for Neo's Python backend: how it is laid out, how a request becomes a
response, how data is stored, and how each major subsystem works. For installation see
[setup.md](setup.md); for the browser app see [frontend.md](frontend.md).

## Contents

1. [Stack and layout](#stack-and-layout)
2. [Process lifecycle](#process-lifecycle)
3. [Configuration](#configuration)
4. [Request pipeline and security](#request-pipeline-and-security)
5. [Profiles and storage](#profiles-and-storage)
6. [Database conventions](#database-conventions)
7. [API surface](#api-surface)
8. [Chat](#chat)
9. [Memory](#memory)
10. [Routing, web search and live data](#routing-web-search-and-live-data)
11. [Research Mode](#research-mode)
12. [Agent Mode](#agent-mode)
13. [Workspaces, repositories and tooling](#workspaces-repositories-and-tooling)
14. [Isolation and external agents](#isolation-and-external-agents)
15. [Connectors and connected accounts](#connectors-and-connected-accounts)
16. [Other subsystems](#other-subsystems)
17. [CLI and TUI](#cli-and-tui)
18. [Operations](#operations)
19. [Conventions for contributors](#conventions-for-contributors)

---

## Stack and layout

- **Python 3.12**, **FastAPI** + **uvicorn**, **SQLAlchemy 2** (ORM for core models) with raw
  `sqlite3` in many service stores, **pydantic v2 / pydantic-settings**.
- **SQLite** everywhere (WAL mode). Qdrant client is a dependency but SQLite-based vector
  indexes are used for memory recall.
- **LLMs:** Ollama by default; OpenAI-compatible endpoints supported. Embeddings default to
  `nomic-embed-text`.
- Packaging: `pyproject.toml`; entry points `neo` (CLI) and `python -m app.runtime` (server).
- Lint: `ruff` (line length 100, rules E, F, I, UP, B).

```text
app/
  main.py            App factory, middleware, router registration, startup recovery
  runtime.py         `python -m app.runtime` -> uvicorn with host/port from settings
  core/              Settings, path-safety primitives, identifiers, text IO, SQLite backup
  db/                SQLAlchemy base, engine/session factory, schema upgrade helpers
  models/            ORM models: chat, memory, projects, enums, mixins
  schemas/           Pydantic request/response schemas shared by routes
  repositories/      Owner-bound persistence (memory, app store)
  api/routes/        One module per feature area (~48), each exposing `router`
  services/          All business logic, one package/module per feature
  cli/               `neo` command, HTTP client, formatters, and a text TUI
  static/            (built frontend, copied here by the Docker image)
```

The architectural rule: **routes are thin; services own behaviour.** A route parses the request,
calls a service, and shapes the response. Persistence conventions differ per service (see
[Database conventions](#database-conventions)).

## Process lifecycle

`app.main:create_app()` builds the FastAPI app at import time (`app = create_app()`):

1. Registers CORS and the security middleware stack.
2. Includes every router under `/api` (the `web` router is mounted both at `/` and `/api`).
3. Runs **every feature's `initialize_*_tables()`** against the base database. Tables are
   created idempotently (`CREATE TABLE IF NOT EXISTS` / `create_all`), and additive columns are
   patched in with `ALTER TABLE` helpers (e.g. `ensure_chat_columns`).
4. Seeds built-ins (`AgentDefinitionService.seed_builtins`, `EvaluationService.seed_builtins`,
   `LLMRegistryService.ensure_defaults`).
5. **Crash recovery:** `recover_interrupted_research_jobs`, `recover_interrupted_runs`
   (isolation), `recover_interrupted_sessions` (agent runs left `running`), and
   `delete_chatless_sessions`.
6. Starts the reminder sweep thread and (if SearXNG is the provider) warms the embedded SearXNG on
   a daemon thread.
7. On shutdown, deletes temporary guest profiles.
8. If `NEO_FRONTEND_DIR` contains `index.html`, mounts `/assets` and serves the SPA for any
   non-`/api` path, with `no-store` headers. Legacy service workers are retired by serving a
   self-unregistering worker at `/sw.js` and `/service-worker.js`.

Because each profile has its own database, feature tables are also created **per profile on first
use**, so a profile opened by an old session cookie picks up tables added by a newer build.

There is no auto-reload. Restart after code changes.

## Configuration

All settings live in `app/core/config.py` as a pydantic-settings `Settings` class.

- Env prefix `NEO_`, optional `.env` file, unknown keys ignored.
- Several fields accept **extra aliases** (e.g. `OLLAMA_BASE_URL`, `SEARCH_PROVIDER`,
  `TAVILY_API_KEY`); check the field's `validation_alias`.
- `get_settings()` is **profile-aware**: inside a profile context it returns a copy whose
  `database_url`, `data_dir`, workspace/gallery/skills/LLM-config paths point at that profile's
  storage. Outside a session it returns the base settings. `get_base_settings()` always returns
  process-wide settings. `get_settings.cache_clear()` resets the cache (used by the CLI and tests).
- Setting `NEO_DATA_DIR` derives `database_url`, `workspace_files_dir`, `gallery_thumbnails_dir`,
  `workspace_repos_dir`, `skills_dir` and `llm_config_path` unless those are set explicitly.
- Managed workspace copies live in a **separate code-only root** (`<data>/workspaces/<profile>`),
  deliberately outside profile private storage, because private storage is denied to agents
  wholesale.

Main setting groups (see the file for the full, commented list):

| Group | Examples |
|---|---|
| Server | `host`, `port`, `dev_cors_enabled`, `trusted_hosts`, `frontend_dir` |
| Storage | `data_dir`, `database_url`, `workspace_*_dir`, `skills_dir` |
| Models | `llm_provider`, `ollama_url`, `default_model`/`chat_model`, `openai_compat_*`, `chat_*` token budgets, `max_concurrent_turns` |
| Memory | `memory_*` (extraction, recall caps, worker leases/retries, embedding model, alert thresholds) |
| Search / web | `web_search_provider`, `web_search_fallback_providers`, `searxng_*`, provider API keys, `web_fetch_*`, `research_*` |
| Workspaces | `workspace_live_roots`, `workspace_host_root`, repo/file size caps |
| Gallery / vision | `gallery_*`, `vision_model` |
| Voice | `voice_*` |
| Integrations | `google_oauth_*`, `connector_master_key[_file]`, `integration_oauth_redirect_uri` |
| External agents | `external_agents_enabled`, `*_bin`, timeouts, concurrency, `external_agent_allow_unsafe`, `codex_allow_refresh_token_handoff` |

## Request pipeline and security

Middleware (outermost first, i.e. in execution order):

1. **TrustedHostMiddleware**: only loopback names and `NEO_TRUSTED_HOSTS` are accepted;
   others get `400 untrusted_host`. Exact-match on the host, not a suffix, to stop
   `127.0.0.1.evil.com`. Defends against DNS rebinding.
2. **OriginGuardMiddleware**: for non-safe methods on `/api` and `/web`, a present `Origin`
   must equal the request's own origin (or a dev origin when `dev_cors_enabled`); cross-site
   writes without `Origin` are rejected (`403 origin_required`).
3. **CsrfGuardMiddleware**: browser writes (those carrying `Origin`/`Sec-Fetch-Site`) with a
   session cookie must send an `X-Neo-Csrf` header matching the `neo_csrf` cookie
   (double-submit, constant-time compare). Native API clients that send neither header are exempt.
4. **NoStoreMiddleware**: every `/api` and `/web` response gets `Cache-Control: no-store`;
   HTML responses get a strict CSP (`default-src 'self'`, no inline scripts, no framing).
5. **ProfileSessionMiddleware**: resolves the session cookie to a profile (in a threadpool),
   returns `401 authenticated_profile_required` for protected paths with no session, issues the
   CSRF cookie on first GET, then runs the rest of the request **inside
   `profile_database(profile_id)`**, which sets context variables so every `get_settings()` and
   `SessionLocal()` call inside the request resolves to that profile's storage.
6. CORS (only enabled for dev origins).

Public (no session) paths: `/api/account-profiles/*` (profile picker) and `/api/health/*`, plus
the SPA shell and `/assets`. **Everything else under `/api` and `/web` requires a session.**
Authentication is enforced centrally so a new router cannot ship unprotected by omission.

Other security properties:

- Passwords: PBKDF2 (390k iterations); 5 failed unlocks trigger an exponentially growing
  lockout (15 s up to 15 min). Sessions expire after 14 idle / 90 absolute days.
- Secrets: connector credentials and OAuth tokens are sealed with AES-GCM, bound to
  profile and record, and never returned by read APIs.
- Path safety (`app/core/paths.py`): all containment checks go through one cross-platform
  implementation that handles case-insensitive filesystems, Windows drive-relative paths, UNC
  paths, reserved device names and junctions/symlinks.
- Redaction helpers exist in several services (`*/redaction.py`) so stored audit data excludes
  secrets.

## Profiles and storage

A **profile** is a local account. The registry (`<profiles root>/registry.db`) stores only
public metadata, password hashes and session tokens. Each non-guest profile gets:

- its own SQLite database,
- its own directory for uploaded files, gallery thumbnails, skills and `neo_llms.json`,
- its own memory key material.

Guest profiles are temporary and removed on session end or shutdown.

Where the profiles root lives (`profile_accounts._root`):

1. `<NEO_DATA_DIR>/profiles` if `data_dir` is set;
2. else beside the SQLite file named by `database_url` (`<db dir>/profiles`), anchored to the
   **install directory**, not the working directory (starting Neo from another folder must not
   create a second set of databases);
3. else `<install>/data/profiles`.

`profile_database(profile_id)` is a context manager that sets the `active_profile_database_url`
and `active_profile_storage_dir` context variables. Background threads that outlive a request
must re-enter it (or capture the profile) to touch the right database.

## Database conventions

- Engine/session factory: `app/db/session.py`. `SessionLocal()` is profile-aware: it resolves the
  current profile's URL, caching one engine/sessionmaker per URL behind a lock.
- SQLite is configured with `journal_mode=WAL`, `busy_timeout=30000`, `foreign_keys=ON`.
- **No Alembic.** Schema evolves by additive, idempotent changes:
  `Base.metadata.create_all`, `ensure_*_columns()` helpers using `ALTER TABLE ... ADD COLUMN`,
  and per-service `initialize_*_tables()`.
  Heavier features (notes, memory) use versioned, backed-up migration ledgers
  (`services/notes/migrations.py`, `db/memory_migrations.py`).
- Two persistence styles coexist:
  - ORM models (`models/`) with `Session` for chat, projects and memory.
  - Service-local stores (`services/*/store.py`) using raw `sqlite3` with one connection per
    call. This is intentional for worker threads, which should not share a request-scoped session.
- Every row is **owner-bound** (profile / owner UUID). The registry validates owner-profile pairs.
- `app/core/sqlite_backup.py` provides consistent online backups used before risky migrations.

## API surface

All routes are under `/api` (e.g. `GET /api/chats`). Each file in `app/api/routes/` maps to a
feature; the interactive OpenAPI docs are served by FastAPI at `/docs` and `/openapi.json`
(also reachable through the proxy in dev).

| Area | Route module(s) |
|---|---|
| Accounts / session | `accounts` (`/account-profiles`), `security` |
| Chat | `chat` (chats, generations, events, fork, compact, projects, config) |
| Memory | `memory`, `memory_health`, `memory_retrieval`, `context_memory` |
| Search / research | `search`, `web`, `web_search`, `research` |
| Agent Mode | `agent_sessions`, `agent_framework`, `external_agents`, `skills`, `rules` |
| Workspaces & code | `workspaces`, `files`, `repos`, `code_index`, `symbols`, `lsp`, `patches`, `git`, `github`, `test_runner`, `command_sandbox` |
| Productivity | `notes`, `tasks`, `calendar`, `projects`, `gallery` |
| Models | `llms`, `llm_registry`, `local_models`, `model_compare`, `provider_runtime`, `evaluation` |
| Connectors | `integrations` (connected accounts), `integration` (status) |
| Preferences | `appearance`, `keybindings`, `sidebar_nav`, `voice` |
| Portability | `bundles`, `continuity` |
| Health | `health` (`/health`, `/health/live`, `/health/ready`) |

## Chat

Files: `api/routes/chat.py`, `services/chat.py` (`NeoChatService`), `services/chat_events.py`,
`services/chat_intent.py`, `services/chat_prefs.py`, `models/chat.py`.

**Data model:** `Chat` (title, pin, archive, project, workspace/repo, agent mode, effort,
executor, disabled tools, skill overrides) -> `ChatMessage` (role, content, thinking, token
counts, duration, provider/model, response kind, trace id, metadata) plus generation rows for
background work.

**Sending a message** is a *generation*: `POST /chats/{id}/generations` starts a background
worker and returns immediately. Key guarantees:

- **Idempotency:** the client supplies a request id; a retry returns the existing turn rather
  than creating a duplicate user row.
- **Leases and fencing:** a worker holds an expiring lease and writes with a fence, so a stale
  worker (after a restart or takeover) cannot write into the transcript.
- **One assistant row per generation**, linked to the generation, so retries and refreshes
  cannot duplicate replies.
- **Live log:** every producer appends to one per-chat NDJSON event log
  (`services/chat_events.py`), tailed by `GET /chats/{id}/events` (and `GET /chat-events` across
  chats). Plain replies and agent runs share one event vocabulary (`agent_core/events.py`:
  `chunk`, `thinking`, `run.status`, `run.completed`, ...). The browser keeps one reconnecting
  reader instead of polling.
- Other operations: stream (`/messages/stream`), edit/rerun a message, fork, compact (summarise
  history), archive, delete, and per-chat tool listing.

**Effort:** a chat has an effort level (`low` default, up to high). Low uses the non-reasoning
path; High always reasons. Routing is shared with the model registry, and budgets
(`chat_num_predict`, thinking headroom) are settings.

**Turn construction** (`NeoChatService`): classify intent (`chat_intent`, `search/intent`), build
context (history turns, rules, memory selection, gallery matches, images for vision models),
route to a handler (conversation, memory command, live data, web research, connector call, or
agent), call the model, persist the reply with metadata, then run **post-response memory
extraction** in the background.

## Memory

Files: `services/memory/` (~39 modules), `memory_chat.py`, `memory_retrieval/`,
`models/memory.py`, `repositories/memory.py`.

Neo's personal memory is a **canonical, typed, owner-bound store** with derived indexes. It is
built in phases whose names appear in module docstrings:

| Phase | What | Key modules |
|---|---|---|
| 0 | Product policy and taxonomy (profile facts, education, preferences, goals, projects, events, current activities, general) | `policy`, `taxonomy`, `versions` |
| 2 | Mutation kernel: sole transactional coordinator for state transitions | `mutations`, `planner`, `normalization`, `tombstones`, `idempotency` |
| 3 | Adapters: every write (chat, UI, import) ends at one coordinator | `adapters`, `coordinator`, `contracts`, `source_changes` |
| 4 | Extraction: bounded model extraction with exact span grounding | `extraction*`, `grounding`, `preparser`, `model_schema` |
| 5 | Recall: canonical, owner-bound retrieval | `recall`, `queries`, `prompt` |
| 6 | Derived indexes: FTS and vector, reconstructible | `indexes`, `outbox`, `maintenance`, `metrics` |

Behaviours worth knowing:

- **Provenance and supersession:** a memory records its source messages; new facts supersede
  old ones rather than overwrite them. Replacing a source is reversible during an edit.
- **Tombstones:** deleting a memory (or its last supporting source) leaves a durable tombstone so
  later extraction does not resurrect it.
- **Encryption:** memory payloads are encrypted per profile (`local_crypto`, `keyring*`). A key
  ring writes with the newest key and reads with all; `keyring_migration` does offline,
  backed-up rotation.
- **Derived state is disposable.** FTS and vector indexes are rebuilt from canonical rows by
  `maintenance`. An **outbox** with leases, retries and a dead-letter threshold keeps indexes
  in sync after commit; `memory_health` routes expose coverage, lag and dead letters, with
  alert thresholds in settings (`memory_alert_*`).
- **Recall** is hybrid (FTS + semantic + slot match + importance weights), capped by
  `memory_recall_max_records` / `_max_chars`, and untrusted memory text is serialised safely into
  prompts (`prompt.py`) with usage accounting of which IDs were actually used.
- Extraction and embeddings use Ollama by default; failures put the provider on cooldown
  (`memory_provider_cooldown_seconds`) instead of failing chat.
- Switches: `memory_enabled`, `memory_extraction_enabled`, `memory_incognito` (don't write),
  `memory_semantic_recall_enabled`, `memory_index_worker_enabled`.

`context_memory/` is separate: workspace-level context summaries with token budgets and
redaction for Agent Mode.

## Routing, web search and live data

Files: `services/search/` (current), `services/web_search/` (audit/store/planner), `web_fetch.py`,
`source_citations.py`, `current_time.py`.

- **Intent routing** (`search/intent.py`) decides whether a message is conversation, a personal
  declaration, an internal command, a live-data request, or needs web research. Routing is
  deliberately conservative.
- **Live data** (`search/live_data.py`): weather and forecasts via Open-Meteo, currency
  conversion via Frankfurter, and local date/time, none requiring web search.
- **Providers** (`search/providers.py`): `searxng` (embedded in-process; `searxng_embedded.py`
  imports `searx.webapp` as a WSGI app, nothing binds a port), `external_searxng`, `tavily`,
  `brave`, `serper`, `duckduckgo`, `bing_html`, with an ordered **fallback chain**, per-provider
  cooldown after failures, and an optional Wikipedia last resort.
- **Pipeline:** query planning (`query_planner`) -> search -> relevance ranking (`ranking`) ->
  page fetch (`web_fetch`, with SSRF/DNS-rebinding protection in `tools/security.py`) ->
  content extraction (`content`) -> evidence assembly -> answer with validated citations
  (`citations`).
- **Evidence integrity:** snippets are for discovery only. **Claims and citations require
  successfully fetched page content.** Dates in evidence are checked against today
  (`temporal.py`), including release-date safeguards. Provider attempts and evidence are
  persisted for audit.
- Limits (results, pages fetched, context tokens, timeouts, byte caps) are settings
  (`web_*`), with higher caps for high effort.

## Research Mode

Files: `services/research/` (~23 modules), routes in `api/routes/research.py`.

A longer-running, evidence-driven pipeline that runs as a **background job** persisted in SQLite
(`jobs.py`, `store.py`) and recovered on restart. Three depths: **quick**, **standard**, **deep**.
Modes differ in *how much effort* they spend (`policy.py`), not in verification or willingness
to answer; Quick leads with the best-supported answer.

Stages (`engine.py` coordinates):

1. **Plan** (`planner`): what must be established and what evidence would establish it.
2. **Retrieve** (`strategy`, `retrieval`): choose the next action, search, triage, fetch pages
   (parallel, with retries and timeouts).
3. **Extract** (`extraction`): chunk fetched pages, strip chrome, detect duplicates.
4. **Evidence gate** (`evidence`, `authority`): candidate selection, model judgement, then
   *deterministic* acceptance; provenance determines what each item can support.
5. **Evidence map** (`evidence_map`): one account of what the evidence establishes per target,
   including conflicts.
6. **Claims** (`claims`): the report may state only claims synthesised from accepted evidence ids.
7. **Verify** (`verify`): mandatory audit; nothing reaches the report without passing it.
8. **Stop** (`stopping`): halt when more research could not materially change the answer.
9. **Confidence and report** (`confidence`, `report`, `report_depth`): confidence derives from
   what the evidence map establishes, never from volume collected.

Model calls use structured JSON (`llm_json`); `capability.py` reads what the research route's model
can do from the model registry.

## Agent Mode

Files: `services/agent_core/` (loop, tools, permissions, journal), `agent_framework/`
(definitions, roles), `rules/`, `skills/`.

An **agent session** is a turn of a chat in which the model uses tools to work on a workspace.

- **Loop** (`loop.py`): prompt build (`prompt.py`) -> model call -> tool calls -> results ->
  repeat until the objective is met (`completion.py`) or a limit/approval stops it. Two transports
  for one contract (`tool_protocol.py`): native tool calling, and a parsed-text fallback.
- **Worker** (`worker.py`): runs sessions in the background with a lease, heartbeat and
  cancellation; turns exceptions into `run.failed`. External executors plug in at the same place.
- **Persistence** (`store.py`) and **events** (`events.py`) feed the shared chat event log.
- **Context** (`context.py`) keeps the transcript within the model's window.
- **Tools** (`agent_core/tools/`): files (read/list/glob/grep/write/edit/delete), shell and
  tests, checkpoints, delivery, calendar, gallery, knowledge (web, code intelligence, memory),
  skills, and an optional todo list. `registry.py` assembles the per-session set;
  `connectors.py` builds the per-chat toggle-annotated catalog; users can disable tools per chat.
- **Permissions** (`permissions.py`): one canonical resolver. It runs the existing primitives
  (step allowlist, tool permissions, role clamps, policy) first and only then applies the
  **PLAN / NORMAL / AUTO** overlay. A mode can only narrow what the primitives allow:
  PLAN never mutates, NORMAL asks before mutating, AUTO mutates inside the sandbox but still stops
  at hard-forbidden operations. Outcomes: `allow`, `ask`, `deny`, `propose`.
- **Approvals** (`approvals.py`): an approved tool call is frozen so it cannot be redirected.
- **Journal** (`journal.py`): every file a run touches is recorded *before* it is changed, so a
  whole run can be undone.
- **Skills:** installed instruction packs, toggled globally or per chat, loaded mid-run.
- **Rules:** user rules/profiles that shape behaviour, with a hard-forbidden safety floor.

## Workspaces, repositories and tooling

A folder can be attached two ways:

- **Live** (default): Neo indexes the folder in place; the agent edits real files. Allowed only
  under `NEO_WORKSPACE_LIVE_ROOTS` (in Docker: `/workspace`). All edits are journaled.
- **Managed:** Neo imports supported text files into a private copy (`repos/`), the agent edits
  the copy, and the user applies verified changes themselves (`agent_core/delivery.py`,
  `patch_apply`, `patches`). The user's files are never written directly.

Supporting services: `files/` (explicit workspace files and text extraction), `repos/`
(scanner, importer, size caps, folder-safety rules in `repos/safety.py`), `code_index/` and
`symbol_awareness/` (indexing and symbols), `lsp/` (language servers), `git/` (local
checkpoints) and `github/` (connection), `test_runner/` (allowlisted test execution),
`command_sandbox/` (policy-gated commands), `workspace_orchestration/` (plans, readiness,
blockers, timelines), `bundles/` and `continuity/` (portable export/import).

Folder-picker rules (`repos/safety.py`): the mount root is browse-only; broad folders (Desktop,
Documents, Downloads, Library, AppData, ...) are refused as destinations but remain browsable;
symlinks/junctions are refused; `..` cannot escape; system trees and `$HOME` stay refused. Line
endings are preserved when editing.

## Isolation and external agents

`services/isolation/` is the only way Neo launches anything that touches a user's workspace:
external coding CLIs, plus Neo's own git, command sandbox, test runner and language servers.

- A run names its workspace and what it owns; everything else is denied via a **per-run macOS
  Seatbelt profile** (`policy.py`), verified immediately before launch (`attest.py`), with
  scrubbed environment (`environment.py`), validated executables (`executables.py`), anchored
  writes (`fs_helper`, `safe_fs`) and process-tree cleanup (`procs.py`).
- **External agents** (`services/external_agents/`) run Claude Code / Codex-style CLIs as
  executors via `run`, called by the agent worker in place of the internal loop. Components cover
  detection, one-time validation (`validation`, `compat`), preflight, minimal credentials,
  disclosure assembly (`disclosure`: the only place their input is built; the user previews and
  approves any earlier message), snapshots of what changed, and usage windows.
- On **Linux, Windows and Docker** external CLIs refuse to run. Neo-side tooling still works
  there with ordinary permissions and Git hardening, but is not OS-isolated.
- `services/disclosure/` labels data provenance and gates outbound disclosure.
  A private disclosure runs in a separate full copy and its changes need exact-diff review.

## Connectors and connected accounts

`services/integrations/` handles accounts (Google Calendar/Docs/Drive/Gmail) via **OAuth 2.0
Authorization Code with PKCE** (`oauth.py`), atomic token rotation, and sealed credential
storage (`vault.py`: AES-GCM, key from `NEO_CONNECTOR_MASTER_KEY[_FILE]`). `http.py` is the single
place a credential is attached to an outbound request. `capabilities.py` expresses what an
account may do in user-readable terms.

Broader connector types (OpenAPI, manual REST, MCP Streamable HTTP, legacy MCP SSE, trusted
stdio) are configured per profile. Reads from uniquely matched operations may be used
automatically; external or workspace **writes always require explicit approval**. Connector
administration requires an active profile session.

The OAuth redirect URI is a fixed setting (`integration_oauth_redirect_uri`), not derived from the
request, because providers match it exactly.

## Other subsystems

| Package / module | Purpose |
|---|---|
| `notes/` | Notes with folders, smart folders, locked (client-side encrypted) notes, versioned migrations, revisions and recovery. Server stores ciphertext; keys are wrapped by a password-derived key (`keys.py`). |
| `tasks/`, `calendar/` | Tasks and calendar events, proposals and a reminder sweep thread. |
| `projects/` | Chat projects and their files. |
| `gallery/` | Image library with thumbnails, vision descriptions and hybrid search. |
| `voice/` | Speech-to-text via faster-whisper (optional extra); model download and idle unload. |
| `local_models/` | Hardware-aware local-model recommendations and installs. |
| `llm_registry/`, `provider_runtime/` | Model registry/routing/health/usage; request audit, retries, rate limits, budgets. |
| `model_compare/` | Run one prompt across models. |
| `evaluation/` | Built-in evaluation suites and baselines. |
| `appearance.py`, `keybindings.py`, `sidebar_nav.py`, `chat_prefs.py` | Per-profile preferences (theme, keys, sidebar entries, chat defaults). |

## CLI and TUI

`pip install` adds a `neo` command (`app/cli/main.py`) that is a thin HTTP client over a running
server. Command groups: `status`, `health`, `agents`/`agent`, `web`, `providers`, `eval`,
`workspace`, `continuity`, `integration`, `research`, and `tui`. Global flags: `--api-url`
(`NEO_API_URL`), `--json` (`NEO_CLI_OUTPUT`), `--timeout`, `--no-color`. Exit codes: 1 input,
2 server unavailable, 3 API error, 4 denied. `neo tui` is a plain-terminal dashboard with
`--snapshot` for a single render.

## Operations

- **Health:** `/api/health/live` (process only; used by Docker), `/api/health`, and
  `/api/health/ready` (dependency-aware, `503` with a `checks` object when not ready).
- **Logs:** standard uvicorn/Python logging to stdout.
- **Data to back up:** the data directory and the connector master key (see
  [setup.md](setup.md#backups-and-upgrades)).
- **Docker image:** multi-stage (frontend build, pinned SearXNG source, Python runtime). Runs as
  uid 10001 / gid 0 with `/app/data` group-writable so any uid works; entrypoint
  `python -m app.runtime`; healthcheck on `/api/health/live`.
- **Concurrency:** `max_concurrent_turns` bounds simultaneous chat turns;
  `max_concurrent_external_turns` bounds external CLI runs. Long work runs in daemon/worker
  threads with leases so a restart can recover or fail it cleanly.

## Conventions for contributors

- Add a feature as: `services/<feature>/` (logic + `store.py` + `initialize_<feature>_tables`),
  `api/routes/<feature>.py` (thin router), register the router and initializer in `main.py`.
- Never read settings through a module-level constant for anything profile-scoped; call
  `get_settings()` at use time so it resolves the active profile.
- Background threads must establish the profile context (`profile_database`) before touching
  storage.
- Do all path containment through `app/core/paths.py`, never string comparison or bare
  `Path ==`.
- Schema changes must be additive and idempotent; anything destructive needs a backup and a
  versioned ledger.
- Never log or persist secrets, raw tokens or full prompts; use the `redaction` helpers.
- Fixes must be **generic**: no special-casing particular topics, models or providers.
- Ruff must pass: `ruff check app`.

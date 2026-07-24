# Neo Backend

This document is the backend reference for Neo. It describes the running architecture, data
model, routing behavior, every HTTP endpoint group, configuration, deployment controls, and
the validation procedure expected before release.

## 1. Runtime architecture

Neo is a Python 3.12 FastAPI application created by `app.main:create_app`. The same process
can serve the compiled React application and the JSON API.

```text
HTTP request
  -> CORS and profile-database middleware
  -> route module in app/api/routes
  -> domain service and safety/policy checks
  -> SQLAlchemy repository or feature-specific SQLite store
  -> local files, managed repository copy, or optional external provider
```

The major boundaries are:

- **Routes** validate HTTP input, translate service errors to HTTP responses, and define
  public schemas.
- **Services** own business rules, routing, extraction, orchestration, safety, redaction,
  provider interaction, and state transitions.
- **Repositories and stores** own persistence. Core memory uses SQLAlchemy; workspace
  subsystems use explicit SQLite stores.
- **Schemas and types** provide Pydantic request/response contracts and internal typed
  records.
- **Profile middleware** switches the active database and storage root for every authenticated
  request.

At application startup Neo creates missing tables, applies idempotent additive compatibility
migrations, seeds built-in tools, agents, evaluation suites, and LLM defaults, and scans
recoverable run state. Guest directories are removed on shutdown.

### Static frontend behavior

If `NEO_FRONTEND_DIR/index.html` exists, FastAPI mounts its `assets` directory and returns the
SPA entry point for non-API paths. Unknown `/api` paths remain real 404 responses. HTML uses
no-store/no-cache headers. `/service-worker.js` and `/sw.js` serve a retiring worker that
unregisters legacy service workers and navigates controlled clients, preventing an obsolete
frontend bundle from taking control of the origin.

## 2. Profiles, storage, and sessions

The process-wide profile registry is `profiles/registry.db`. Its `account_profiles` table
contains only:

- profile UUID;
- case-insensitive unique username;
- PBKDF2-HMAC-SHA256 salt and password hash;
- optional base64 avatar;
- creation time.

Passwords are never stored in plaintext. A successful create/unlock operation creates an
in-memory session token and places it in the HTTP-only, SameSite=Lax
`neo_profile_session` cookie. Sessions do not survive a backend restart.

Each account uses:

```text
<data-dir>/profiles/accounts/<profile-id>/
  neo.db
  neo_llms.json
  workspace_files/
  workspace_repos/
```

Each guest uses `profiles/guests/<guest-id>` and is deleted when the guest session ends or the
application shuts down. `ProfileDatabaseMiddleware` sets context variables so legacy
`SessionLocal()` and all feature stores resolve to the active profile database and directories.
This is the isolation boundary: code handling a request must use profile-aware settings and
must not cache a profile-specific absolute path globally.

SQLite runs with WAL, a 30-second busy timeout, foreign keys enabled, and short request-scoped
transactions.

## 3. Database reference

Neo has one SQLAlchemy metadata schema for personal memory/chat and several feature-specific
SQLite schemas. All are created inside the selected profile database unless explicitly noted.

### Core SQLAlchemy tables

| Table | Purpose and significant fields |
| --- | --- |
| `profile` | Typed identity facts: `key`, `value`, confidence, active flag, timestamps. Canonical singular facts are replaced/superseded by review logic. |
| `education` | Institution, degree, field of study, optional explicit graduation date, description, SHA-256 fingerprint, active flag, timestamps. |
| `preferences` | Category/value preferences, canonical slot, fingerprint, confidence, importance, active flag, timestamps. Additive interests can coexist; singular favorites use canonical slots. |
| `goals` | Goal text, notes, priority, active/completed/abandoned status, optional target date, horizon in months, fingerprint, completion and audit timestamps. |
| `activities` | Time-bounded current activity, category, description, fingerprint, start, expiry, archive time, active flag, timestamps. Current activities default to a 30-day lifetime. |
| `projects` | Legacy conversational-memory project: name, description, status, priority, timestamps, and relationships to chats, memories, and events. This is distinct from `workspace_projects`. |
| `events` | Timeline event, notes, optional explicit event date, fingerprint, importance, creation time, and project links. |
| `memories` | Canonical durable record: text, enum type, importance, confidence, source label/sentence/conversation, canonical slot, fingerprint, expiry, lifecycle status, supersedes/superseded-by links, update reason, active flag, access time, timestamps, typed-source relationship, embedding relationship, and project links. |
| `memory_sources` | Provenance edge from a durable memory to a user message: conversation ID, message ID, exact supporting sentence, source fingerprint, active flag, `replacement`/`deletion` detachment reason, and timestamps. The `(memory_id, source_fingerprint)` pair is unique. |
| `memory_candidates` | Extraction review queue: text, candidate type, confidence, importance, serialized reasoning/attributes, pending/accepted/rejected status, review time, and accepted-memory link. |
| `memory_embeddings` | One embedding record per memory: provider/model, dimensions, JSON vector, content hash, missing/ready/stale/error status, error, embedding time, timestamps. |
| `memory_lifecycle_audit` | Immutable lifecycle entries for archive, supersede, restore, delete, or maintenance decisions, including previous/new status, reason, related memory, source sentence, and time. |
| `reflections` | Persisted reflection text, importance, timestamps. |
| `chats` | Conversation title, optional legacy memory-project ID, archived flag, timestamps. |
| `chat_messages` | User/assistant content plus prompt/completion/total tokens, duration, model thinking, response kind, provider, model, route, finish reason, trace ID, JSON metadata, optional unique generation ID, and creation time. The generation link permits an interrupted worker to update its one assistant row instead of appending another. |
| `chat_generations` | Durable background generation: UUID, chat/prompt/model/client-request IDs, linked user and assistant messages, queued/running/completed/failed status, status detail, partial output, thinking, reply/error, timezone/locale, response metadata, usage/duration, worker ID, opaque lease token, attempt count, heartbeat, and lifecycle timestamps. `client_request_id` has a unique index for retry idempotency. |
| `memory_project_links` | Many-to-many durable-memory to legacy-project association. |
| `event_project_links` | Many-to-many event to legacy-project association. |

Memory types include identity, preference, goal-related, project-related, life fact,
instruction, relationship, knowledge, education, activity, and related lifecycle categories.
Candidate types include identity, education, preference, goal, project, activity, event,
memory, and none.

### Workspace and subsystem tables

The following tables are initialized with `CREATE TABLE IF NOT EXISTS`. JSON fields preserve
typed service snapshots without making the public API depend on SQLite column layout.

| Domain | Tables |
| --- | --- |
| Notes | `notes`, `note_tags`, `note_links` |
| Workspace projects/tasks | `workspace_projects`, `workspace_project_tags`, `workspace_project_links`, `workspace_project_notes`; `workspace_tasks`, `workspace_task_tags`, `workspace_task_notes`, `workspace_task_links` |
| Files, repositories, and code intelligence | `workspace_files`, `workspace_file_links`, `workspace_artifacts`, `workspace_patch_applications`, `workspace_patch_application_files`, `workspace_repos`, `workspace_repo_files`, `workspace_code_indexes`, `workspace_code_symbols`, `workspace_code_dependencies`, `workspace_code_file_summaries`, `workspace_code_references`, `workspace_code_symbol_relationships`, `workspace_code_related_files` |
| Agents | `workspace_agent_runs`, `workspace_agent_steps`, `workspace_agent_artifacts`, `workspace_agent_definitions`, `workspace_agent_delegations`, `workspace_agentic_runs`, `workspace_agentic_steps`, `workspace_coding_agent_runs`, `workspace_agent_action_requests`, `workspace_agent_recovery_events` |
| Guarded delivery | `workspace_command_runs`, `workspace_test_commands`, `workspace_test_runs`, `workspace_git_repos`, `workspace_git_checkpoints`, `workspace_git_operations`, `workspace_lsp_sessions`, `workspace_lsp_diagnostics` |
| Provider and model runtime | `workspace_llm_providers`, `workspace_llm_models`, `workspace_llm_routes`, `workspace_llm_calls`, `workspace_provider_requests`, `workspace_provider_health_checks`, `workspace_provider_rate_limits` |
| Search and research | `research_jobs`, `workspace_research_runs`, `workspace_research_claims`, `workspace_research_evidence`, `workspace_research_reports`, `workspace_research_conflicts`, `workspace_web_search_runs`, `workspace_web_sources`, `workspace_web_evidence`, `workspace_web_conflicts`, `workspace_web_source_cache` |
| Retrieval and context | `workspace_memory_items`, `workspace_memory_links`, `workspace_memory_retrievals`, `workspace_context_summaries`, `workspace_context_events` |
| Tools and connectors | `workspace_tool_servers`, `workspace_tool_definitions`, `workspace_skill_definitions`, `workspace_tool_calls`, `workspace_connector_credentials`, `workspace_connector_oauth_states` |
| Rules, GitHub, evaluation | `workspace_rule_profiles`, `workspace_rule_resolution_logs`; `workspace_github_connections`, `workspace_github_items`, `workspace_github_operations`; `workspace_eval_suites`, `workspace_eval_runs`, `workspace_eval_cases`, `workspace_eval_case_results`, `workspace_eval_baselines` |
| Bundles and continuity | `workspace_export_bundles`, `workspace_import_bundles`, `workspace_continuity_bundles`, `workspace_continuity_references`, `workspace_continuity_validation_results` |
| Workspace orchestration | `workspace_orchestration_workspaces`, `workspace_orchestration_nodes`, `workspace_orchestration_edges`, `workspace_orchestration_events`, `workspace_orchestration_artifacts`, `workspace_orchestration_readiness_checks` |

### Schema initialization and migrations

Neo currently uses startup migrations rather than Alembic:

1. `Base.metadata.create_all()` creates missing core tables.
2. `ensure_chat_message_metadata_columns()` adds usage, thinking, response-kind, provider,
   model, route, finish-reason, trace, metadata, and generation-link columns to older
   `chat_messages`, then creates the unique generation index.
3. `ensure_chat_generation_columns()` adds thinking, progress, browser context, response
   metadata, usage, worker, lease-token, attempt-count, and heartbeat columns to older
   generation tables.
4. `ensure_memory_metadata_columns()` adds source, canonical-slot, lifecycle, and supersession
   fields, then backfills source sentences.
5. `ensure_typed_memory_columns()` adds memory/preference/goal/event fingerprints, memory
   expiry, preference slots, goal horizon/target fields, and memory-source detachment reason,
   then creates their indexes.
6. `ensure_memory_embedding_table()` creates embedding storage when missing and backfills
   legacy memory status and canonical slots.
7. Every service initializer creates its tables and indexes idempotently.

Migrations are additive. Operators must back up the exact profile directory before upgrading;
the application does not provide a general down-migration.

## 4. Chat routing and generation

### Submission contract

`ChatSendRequest` contains:

- required `prompt`;
- optional `llm_id`;
- optional `client_request_id` for idempotency;
- optional IANA `timezone`;
- optional locale (normalized and length-bounded).

The browser supplies `Intl.DateTimeFormat().resolvedOptions().timeZone` and
`navigator.language`. Invalid time zones are rejected with 422 rather than silently used.

The durable generation API is the primary frontend path:

1. Create exactly one user message and one queued `chat_generations` row in the same
   transaction; a repeated `client_request_id` returns the existing generation instead of
   inserting another user turn.
2. Start a daemon worker in the correct profile context.
3. Atomically claim queued work with a random lease token, worker ID, heartbeat, and
   incremented attempt count.
4. Update `status_detail`, partial output, thinking, and heartbeat only while that lease is
   still owned by the worker.
5. Upsert exactly one assistant message under the generation’s unique ID and link it back to
   the generation. This closes the crash window between assistant persistence and generation
   completion.
6. Set a terminal completed/failed state, error, finish reason, metadata, and completion time
   through a lease-fenced update.

On refresh, the client asks for the active generation and resumes polling. Queued work is
scheduled; running work is reclaimed only after its heartbeat exceeds the lease duration
(the larger of 120 seconds or the configured chat timeout plus 60 seconds). An atomic
compare-and-set allows one recovery worker to win while preserving existing partial output.
A live lease is never stolen. Editing or rerunning fences affected workers as `Superseded`
before transcript changes, so a late worker cannot write into the replacement conversation.

### Shared routing order

`NeoChatService` resolves one typed search intent and then follows conservative, explicit
routes:

1. active-rule inspection or agent guidance when explicitly requested;
2. deterministic memory extraction and post-commit acknowledgement for a pure personal
   declaration;
3. explicit internal commands for coding, Recovery, Git, tests, or tasks;
4. an unambiguous connector call;
5. structured local date/time, weather, or currency;
6. general/release-date web evidence;
7. direct memory recall;
8. normal selected-LLM conversation with assembled memory/project/task/file/code/rule context.

Internal feature names alone do not execute a feature. The internal intent resolver requires a
command-shaped verb plus a matching target and rejects explanatory/documentation/comparison
language. “Explain recovery after restart” therefore stays normal chat; “Find my recoverable
runs” may use Recovery. Ambiguous intent defaults to chat or no connector selection.

### Streaming and metadata

Normal model output emits chunk events progressively. Thinking is emitted only when the
provider supplies it; Neo does not fabricate a reasoning trace. Search synthesis is buffered:
the stream can show search/read/validation status, but the answer is revealed only after its
citations validate.

If a provider finishes with `length`, Neo requests up to two continuations and merges them
with overlap removal. Repeated truncation becomes an explicit `incomplete_length` response;
truncated text is not presented as a complete success. Provider failure during a grounded
search can use a deterministic evidence-only fallback. Cancellation, transport failure, and
unsupported citations are non-success states.

Persisted assistant metadata includes, where applicable:

- `response_kind`: normal chat, direct memory, internal action, local date/time, structured
  weather, structured currency, web search, or connector;
- provider and model;
- route and finish reason;
- prompt/completion/total tokens;
- duration;
- provider trace ID;
- typed search intent plus search/connector trace and evidence diagnostics.

Direct local responses use meaningful response-kind/provider/route/duration metadata instead
of pretending to have model token counts.

## 5. Typed memory pipeline

Memory is extracted from the current **user** turn only. Assistant messages, quoted
third-party claims, greetings, questions, and one-off commands are excluded.

### Extraction and acceptance

1. Sentence and clause splitting preserves multiple declarations in one message.
2. Deterministic extractors recognize profile/location/country/occupation, education and
   graduation, additive interests, singular favorites, ordered language priorities, durable
   goals, named projects, events, hardware, and current activities.
3. A schema-constrained model fallback may inspect only ignored user text. Every proposed
   value must be grounded in a source span. Model-only candidates remain pending
   (`auto_accept=0`) and are never acknowledged as saved.
4. High-confidence deterministic candidates are persisted and accepted through the common
   review service.
5. Typed records and their canonical durable memory are committed together.
6. A pure declaration receives “saved” acknowledgement only after all its candidates were
   accepted successfully.

Education stores institution, degree, field, and a graduation event. A graduation date is
stored only if the user explicitly supplied it. Goal horizons are anchored to the source
message timestamp. Current playing/reading/watching/learning activities expire after 30 days
and are archived when expired or superseded.

### Deduplication, slots, and provenance

Normalized NFKC/case-folded values produce SHA-256 fingerprints. Repeated facts attach a new
`memory_sources` edge rather than duplicate the canonical record. Canonical slots enforce
singular facts such as `identity:occupation` or a favorite category; additive interests and
independent goals retain distinct fingerprints.

Editing or rerunning marks the old source edge with detachment reason `replacement`. The fact
remains active while another source supports it. If no source remains, the exact canonical
memory and its typed projection are archived/deactivated; re-extracting the same fact from
the replacement message reactivates that archived record instead of creating a duplicate.

Deleting a source chat marks its edges with reason `deletion`. When the final source is
deleted, the canonical record becomes a durable deleted tombstone and its typed projection is
deactivated. A later identical statement is rejected rather than silently reviving an
explicitly deleted fact; restoration must use the lifecycle restore operation. Manual memory
deletion has the same durable-tombstone protection. Every transition is recorded in
`memory_lifecycle_audit`.

### Recall and retrieval

Direct recall covers profile identity, education, occupation, interests, favorites, goals,
language priorities, location/country, projects, and current activity. Context assembly can
combine structured lookup, SQLite FTS, optional semantic embeddings, canonical-slot boosts,
importance, and recency. Retrieval does not allow memory text to override current
instructions, rules, or safety policy.

## 6. Search and live-data behavior

### Typed intent

`ResolvedSearchIntent` is one of:

- `none`;
- `general_web`;
- `release_date`;
- `weather`;
- `currency`;
- `local_datetime`;
- `connector_tool`.

It carries confidence, reason, resolved query, entity/location/region/date, decimal amount,
currencies, timezone, and locale. Previous message intent supplies bounded follow-up context:
for example, “what about 10 USD” can retain the earlier USD→INR pair, and “New Delhi today”
can retain weather. An unrelated query does not inherit the old intent.

Personal declarations are resolved before freshness terms. Words such as “recently”,
“current”, “playing”, or “release” do not independently trigger web search. In a mixed turn,
only the explicit live-information clause is searched; the personal clause is the only memory
candidate.

### Structured providers

- **Local date/time** uses a validated browser IANA timezone, then stored/profile context,
  then `NEO_DEFAULT_TIMEZONE`. It makes zero web calls.
- **Currency** uses Frankfurter’s daily-rate API and `Decimal` multiplication. The response
  records amount, base/quote currencies, rate, converted amount, provider, reference date, and
  source.
- **Weather** geocodes the requested place with Open-Meteo. Current/today requests retrieve a
  structured observation with resolved place/country, coordinates, timezone, observation
  time, temperature, apparent temperature, condition/code, and wind. A request for
  “tomorrow” uses the daily forecast endpoint instead of reusing current conditions and
  records forecast date, low/high temperature, condition/code, maximum precipitation
  probability, provider, and source.

Structured-provider failures produce bounded, truthful unavailable responses; they do not
fall through to invented model values.

### General web search

Configured providers are DuckDuckGo HTML, Bing HTML, external SearXNG (including the legacy
`searxng` alias), Tavily, Brave, Serper, or disabled. The primary/fallback chain continues
until it has usable ranked evidence, not merely raw results.

The pipeline:

1. resolve and minimally rewrite the query without inventing an entity/title/year;
2. record each provider attempt with status, duration, result/fetch/evidence/citation counts,
   and rejection reason;
3. normalize and rank results for entity relevance and freshness;
4. fetch bounded public pages with content-type, size, timeout, redirect, and address checks;
5. extract relevant evidence only from successfully fetched page bodies; provider snippets
   remain discovery/ranking metadata and cannot become evidence or a citation if page fetch
   fails;
6. synthesize or select a deterministic answer;
7. remove any model-generated Sources block;
8. validate every bracket marker against fetched and supported evidence;
9. append only backend-owned citation entries.

Unknown, orphaned, or unsupported citation markers fail validation.

The persisted chat search trace includes the resolved intent, provider query, every provider
attempt, rejected result and reason, selected/fetched pages, evidence excerpts, freshness and
publication-date data, and citation acceptance decisions. This audit exists even when no
provider produces usable evidence.

### Release-date safeguards

A page’s publication timestamp is not a product release date. Release extraction requires
explicit release-language evidence tied to the correct entity. A definitive date requires an
official source or corroborating independent authoritative evidence; otherwise Neo reports
that no verified release date has been announced. This prevents an announcement’s article date
from being presented as the release date of the announced game or film.

## 7. Connectors, tools, and skills

Neo extends the existing Tools & Skills subsystem rather than maintaining a parallel
integration store.

### Supported connectors

- OpenAPI 3.x import from HTTPS URL, JSON/YAML document, or uploaded JSON/YAML file
  (2 MiB limit);
- manual REST operation with method, path, base URL, JSON schemas, and parameter locations;
- MCP Streamable HTTP using protocol `2025-06-18`;
- legacy MCP SSE compatibility using the 2024-11-05 handshake;
- explicitly trusted stdio MCP with an argv array and `shell=False`.

MCP connections perform a real `initialize`, `notifications/initialized`, `tools/list`, and
`tools/call`. Discovery persists definitions, annotations, capability tokens, schemas, and a
read/write category. Read-only hints may become `external_read`; unknown or destructive tools
require approval.

### Authentication and OAuth

Connectors support no authentication, header API key, query API key, bearer token, and OAuth
2.0 Authorization Code with PKCE-S256. OAuth state is random, expires, and is bound to the
profile/session hash and exact redirect URI. Tokens are encrypted, refreshed through an
atomic credential update, and can be revoked; plaintext secret values are never included in
credential status responses or normal logs.

AES-GCM uses a 32-byte master key from `NEO_CONNECTOR_MASTER_KEY` or a file named by
`NEO_CONNECTOR_MASTER_KEY_FILE`. Development mode may generate a local `0600` key when neither
is present; production fails closed unless the inline key or existing file is supplied. The
active profile storage identity and connector/server ID are both included as additional
authenticated data, so ciphertext copied to another profile cannot be decrypted.

OAuth completion rotates credentials with a database compare-and-swap against
`updated_at` (in addition to the in-process refresh lock). Concurrent processes therefore
cannot overwrite a freshly rotated refresh token with stale credential state; the loser must
reload and retry.

### Selection, approval, and untrusted output

Enabled read tools are scored by deterministic capability tokens. Chat auto-selects only a
unique high-confidence read match; weak or tied matches return no selection. An explicit tool
ID may request a write, but workspace/external writes enter `pending_approval` and do not
execute until `/tools/calls/{id}/approve`. Rejection is terminal and executes nothing.
Explanatory prompts about a connector or tool are normal chat and never qualify for automatic
execution merely because a capability name appears.

Tool inputs are schema checked and may not contain credentials. Connector schemas and output
are untrusted. Responses are size- and time-bounded, redacted, and returned with provenance;
they are never treated as system instructions.

### Network and process safety

- Public connectors require HTTPS.
- Credentials in URLs and URL fragments are rejected.
- Private, loopback, link-local, reserved, multicast, unspecified, `.local`, `.internal`,
  `.lan`, and `.intranet` targets are blocked.
- An explicitly trusted localhost connector may use loopback HTTP; it cannot grant access to
  other private addresses.
- DNS is checked immediately before each request; cross-origin redirects and more than three
  redirects are blocked.
- Connector responses are limited to 2 MiB and normally 15 seconds.
- stdio commands must be argv arrays, may not contain shell operators, may not launch a shell,
  and can receive only environment-variable references declared in server configuration.

## 8. HTTP API reference

All routes below are under `/api` unless stated otherwise. FastAPI’s generated schema is
available at `/docs` while the backend is running; that runtime route is unrelated to the
repository’s Markdown `docs/` directory.

### Profiles, health, and integration

| Group | Endpoints |
| --- | --- |
| Account profiles | `GET/POST /account-profiles`; `POST /account-profiles/guest`; `POST /account-profiles/{profile_id}/unlock`; `GET /account-profiles/session/current`; `POST /account-profiles/session/end` |
| Health | `GET /health`; `GET /health/live`; `GET /health/ready` |
| Integration audit | `GET /integration/status`; `POST /integration/validate`; `GET /integration/report`; `POST /integration/smoke` |

### Chat and personal memory

| Area | Endpoints |
| --- | --- |
| Extraction/context | `POST /conversation`; `POST /extract-memory`; `POST /retrieve-context`; `POST /memory/review`; `POST /reflection/run` |
| Chat navigation | `GET /sidebar`; `POST /chats`; `GET /chats/{chat_id}`; `DELETE /chats/{chat_id}` |
| Chat response paths | `POST /chats/{chat_id}/messages` (synchronous); `POST /chats/{chat_id}/messages/stream` (newline-delimited JSON); `POST /chats/{chat_id}/generations`; `GET /chats/{chat_id}/generations/active`; `GET /chats/{chat_id}/generations/{generation_id}` |
| Edit/rerun | `PATCH /chats/{chat_id}/messages/{message_id}`; `POST /chats/{chat_id}/messages/{message_id}/rerun` |
| Typed memory reads | `GET /profile`; `GET /education`; `GET /activities`; `GET /preferences`; `GET /goals`; `GET /events`; `GET /memory` and `GET /memories` |
| Typed memory edits | `PATCH/DELETE /profile/{profile_id}`; `PATCH/DELETE /preferences/{preference_id}`; `PATCH/DELETE /goals/{goal_id}`; `PATCH/DELETE /events/{event_id}`; `PATCH /memories/{memory_id}`; `DELETE /memories/{memory_id}` |
| Lifecycle | `POST /memories/{memory_id}/archive`; `POST /memories/{memory_id}/supersede`; `POST /memories/{memory_id}/restore`; `GET /memories/{memory_id}/lifecycle`; `POST /memory/lifecycle/age`; `POST /memory/lifecycle/maintenance`; `GET /memory/candidates`; `POST /memory/explain` |
| Conversational projects | `GET/POST /chat-projects`; `PATCH/DELETE /chat-projects/{project_id}`; `DELETE /chat-projects/{project_id}/memory` |
| Legacy project aliases | `GET/POST /projects`; `PATCH/DELETE /projects/{project_id}`; `DELETE /projects/{project_id}/memory` are defined by the memory router, but the separately registered workspace-project routes occupy the same public prefix. New clients must use `/chat-projects` for conversational projects and the workspace project contract below for `/projects`. |

### Notes, workspace projects, tasks, files, and repositories

| Group | Endpoints |
| --- | --- |
| Notes | `POST/GET /notes`; `GET /notes/tags`; `GET/PATCH/DELETE /notes/{note_id}`; `POST /notes/{note_id}/pin`; `POST /notes/{note_id}/archive` |
| Workspace projects | `POST/GET /projects`; `GET /projects/tags`; `GET /projects/notes/{note_id}/projects`; `GET/PATCH/DELETE /projects/{project_id}`; `GET/POST /projects/{project_id}/tasks`; `POST /projects/{project_id}/pin`; `POST /projects/{project_id}/archive`; `POST/GET /projects/{project_id}/notes`; `DELETE /projects/{project_id}/notes/{note_id}` |
| Tasks | `POST/GET /tasks`; `GET /tasks/tags`; `GET /tasks/notes/{note_id}/tasks`; `GET/PATCH/DELETE /tasks/{task_id}`; `POST /tasks/{task_id}/status`; `POST /tasks/{task_id}/pin`; `POST /tasks/{task_id}/archive`; `POST/GET /tasks/{task_id}/notes`; `DELETE /tasks/{task_id}/notes/{note_id}` |
| Files | `POST /files/upload`; `GET /files`; `GET/DELETE /files/{file_id}`; `GET /files/{file_id}/download`; `POST /files/{file_id}/links`; `DELETE /files/{file_id}/links/{link_id}`; `POST /files/{file_id}/summarize` |
| Artifacts | `POST/GET /artifacts`; `GET /artifacts/{artifact_id}`; `GET /artifacts/{artifact_id}/download` |
| Managed repositories | `POST /repos/register`; `GET /repos`; `GET/DELETE /repos/{repo_id}`; `GET /repos/{repo_id}/files`; `GET /repos/{repo_id}/files/{repo_file_id}` |

### Models and provider runtime

| Group | Endpoints |
| --- | --- |
| Legacy LLM configuration | `GET /llms`; `PUT /llms/{config_id}`; `PUT /llms/active/select`; `POST /llms/{config_id}/test`; `DELETE /llms/{config_id}` |
| LLM registry providers | `GET/POST /llm/providers`; `PATCH/DELETE /llm/providers/{provider_id}` |
| LLM registry models | `GET/POST /llm/models`; `PATCH/DELETE /llm/models/{model_id}` |
| Routes and usage | `GET /llm/routes`; `PATCH /llm/routes/{route_name}`; `POST /llm/health`; `GET /llm/usage` |
| Runtime | `GET /providers/runtime/status`; `POST /providers/runtime/health-check`; `GET /providers/runtime/health`; `GET /providers/runtime/requests`; `GET /providers/runtime/requests/{request_id}`; `POST /providers/runtime/complete`; `POST /providers/runtime/stream/start`; `GET /providers/runtime/stream/{request_id}`; `POST /providers/runtime/stream/{request_id}/cancel`; `GET /providers/runtime/rate-limits`; `GET /providers/runtime/usage` |

### Search and research

| Group | Endpoints |
| --- | --- |
| Search configuration | `GET/POST /search/config`; `GET/POST /search/test`; `GET /search/providers` |
| Search operations | `POST /search`; `POST /search/query`; `POST /search/fetch` |
| Direct web utility | `POST /web/search`; `POST /web/fetch`; `POST /web/answer`. These are mounted both at `/api/web/...` and the root `/web/...` for compatibility. |
| Reliable web-search runs | `POST /web-search/plan`; `POST /web-search/run`; `GET /web-search/runs`; `GET /web-search/runs/{run_id}`; `GET /web-search/runs/{run_id}/sources`; `GET /web-search/runs/{run_id}/evidence`; `GET /web-search/runs/{run_id}/conflicts`; `POST /web-search/runs/{run_id}/refresh`; `GET /web-search/cache`; `DELETE /web-search/cache/{cache_id}` |
| Research mode | `POST /research/plan`; `POST /research/run`; `GET /research/runs`; `GET /research/runs/{run_id}`; `GET /research/runs/{run_id}/evidence`; `GET /research/runs/{run_id}/claims`; `GET /research/runs/{run_id}/conflicts`; `GET /research/runs/{run_id}/report`; `POST /research/runs/{run_id}/continue`; `POST /research/runs/{run_id}/refresh`; `POST /research/runs/{run_id}/validate-citations`; `DELETE /research/runs/{run_id}` |
| Legacy research jobs | `POST /research/start`; `GET /research` and `GET /research/list`; `DELETE /research/clear`; `GET /research/{job_id}`; `GET /research/{job_id}/status`; `GET /research/{job_id}/report`; `POST /research/{job_id}/save-to-note`; `POST /research/{job_id}/cancel`; `GET /research/{job_id}/events` |

### Memory retrieval and context compaction

| Group | Endpoints |
| --- | --- |
| Retrieval index | `POST /memory/index`; `POST /memory/retrieve`; `GET/POST /memory/items`; `GET/PATCH/DELETE /memory/items/{item_id}`; `GET /memory/scopes/{scope_type}/{scope_id}`; `GET /memory/retrievals`; `GET /memory/retrievals/{retrieval_id}`; `POST /memory/prune/preview`; `POST /memory/prune/apply` |
| Context memory | `GET /context-memory/summaries`; `GET /context-memory/summaries/{summary_id}`; `POST /context-memory/preview`; `POST /context-memory/compact`; `GET /context-memory/scopes/{scope_type}/{scope_id}`; `GET/POST /context-memory/scopes/{scope_type}/{scope_id}/events` |

### Agents, coding, recovery, and evaluation

| Group | Endpoints |
| --- | --- |
| Task agents | `POST /agents/plan-tasks`; `POST /agents/runs/from-objective`; `POST/GET /agents/runs`; `GET /agents/runs/{run_id}`; `POST /agents/runs/{run_id}/cancel`; `POST /agents/runs/{run_id}/steps/{step_id}/approve`; `POST /agents/runs/{run_id}/save-to-note`; `GET /tasks/{task_id}/agent-runs` |
| Agent definitions/delegation | `GET/POST /agents/definitions`; `GET/PATCH/DELETE /agents/definitions/{agent_id}`; `POST /agents/definitions/reset-builtins`; `POST/GET /agents/delegations`; `GET/PATCH /agents/delegations/{delegation_id}` |
| Agentic core | `POST/GET /agentic/runs`; `GET /agentic/runs/{run_id}`; `POST /agentic/runs/{run_id}/plan`; `POST /agentic/runs/{run_id}/step`; `POST /agentic/runs/{run_id}/continue`; `POST /agentic/runs/{run_id}/reflect`; `POST /agentic/runs/{run_id}/stop`; `GET /agentic/runs/{run_id}/steps`; `GET /agentic/runs/{run_id}/context` |
| Coding agent | `POST/GET /coding-agent/runs`; `GET /coding-agent/runs/{coding_run_id}`; `POST /coding-agent/actions/{action_request_id}/approve`; `POST /coding-agent/actions/{action_request_id}/reject`; `POST /coding-agent/runs/{coding_run_id}/revise-patch`; `POST /coding-agent/runs/{coding_run_id}/cancel`; `POST /coding-agent/runs/{coding_run_id}/commands/propose` |
| Recovery | `GET /recovery/runs`; `GET /recovery/runs/{run_type}/{run_id}`; `POST /recovery/runs/{run_type}/{run_id}/resume`; `POST /recovery/runs/{run_type}/{run_id}/retry`; `POST /recovery/runs/{run_type}/{run_id}/fork`; `POST /recovery/runs/{run_type}/{run_id}/repair-state`; `GET /recovery/events` |
| Evaluation | `GET/POST /evals/suites`; `GET /evals/suites/{suite_id}`; `POST /evals/suites/{suite_id}/run`; `GET /evals/runs`; `GET/DELETE /evals/runs/{run_id}`; `GET /evals/runs/{run_id}/cases`; `GET /evals/runs/{run_id}/report`; `POST /evals/runs/{run_id}/set-baseline`; `GET /evals/baselines`; `GET /evals/compare` |

### Code intelligence and guarded delivery

| Group | Endpoints |
| --- | --- |
| Code index | `POST /code-index/repos/{repo_id}/build`; `GET /code-index/repos/{repo_id}`; `GET /code-index/repos/{repo_id}/symbols`; `GET /code-index/symbols/{symbol_id}`; `GET /code-index/repos/{repo_id}/search`; `GET /code-index/repos/{repo_id}/routes`; `GET /code-index/repos/{repo_id}/dependencies`; `GET /code-index/repos/{repo_id}/files/{repo_file_id}/summary` |
| Symbol awareness | `POST /symbols/repos/{repo_id}/build`; `GET /symbols/repos/{repo_id}`; `GET /symbols/repos/{repo_id}/definition`; `GET /symbols/{symbol_id}/references`; `GET /symbols/repos/{repo_id}/references`; `GET /symbols/repos/{repo_id}/files/{repo_file_id}/document-symbols`; `GET /symbols/repos/{repo_id}/files/{repo_file_id}/related-files`; `GET /symbols/{symbol_id}/context` |
| LSP | `GET /lsp/status`; `GET /lsp/servers`; `POST /lsp/workspaces/{workspace_id}/start`; `POST /lsp/workspaces/{workspace_id}/stop`; `GET /lsp/workspaces/{workspace_id}/diagnostics`; `POST /lsp/workspaces/{workspace_id}/{action}` |
| Patches | `POST /patches/propose`; `GET /patches/applications`; `GET /patches/applications/{application_id}`; `GET /patches/applications/{application_id}/download`; `POST /patches/{artifact_id}/validate-apply`; `POST /patches/{artifact_id}/apply` |
| Command sandbox | `GET /command-sandbox/runs`; `GET /command-sandbox/runs/{run_id}`; `POST /command-sandbox/validate`; `POST /command-sandbox/propose`; `POST /command-sandbox/runs/{run_id}/approve`; `POST /command-sandbox/runs/{run_id}/execute`; `POST /command-sandbox/runs/{run_id}/cancel` |
| Test runner | `GET/POST /test-runner/repos/{repo_id}/commands`; `PATCH/DELETE /test-runner/commands/{command_id}`; `POST /test-runner/repos/{repo_id}/detect`; `POST /test-runner/commands/{command_id}/run`; `GET /test-runner/runs`; `GET /test-runner/runs/{run_id}` |
| Git checkpoints | `GET /git/repos/{repo_id}/status`; `POST /git/repos/{repo_id}/init`; `GET /git/repos/{repo_id}/diff`; `POST/GET /git/repos/{repo_id}/checkpoints`; `GET /git/checkpoints/{checkpoint_id}`; `POST /git/checkpoints/{checkpoint_id}/restore`; `GET /git/repos/{repo_id}/operations` |

### Tools, connectors, rules, and GitHub

Every `/tools` route requires an active local profile session and returns 401 otherwise. This
includes server discovery/testing, definitions, credentials, OAuth start/callback/refresh/
revoke, calls, approvals, and skills; connector configuration cannot fall back to the base
database or an anonymous profile.

| Group | Endpoints |
| --- | --- |
| Connector servers | `GET/POST /tools/servers`; `PATCH/DELETE /tools/servers/{server_id}`; `POST /tools/servers/{server_id}/health`; `POST /tools/servers/{server_id}/test`; `POST /tools/servers/{server_id}/discover` |
| Connector creation | `POST /tools/connectors/openapi/import`; `POST /tools/connectors/openapi/file`; `POST /tools/connectors/rest`; `POST /tools/connectors/select` |
| Credentials/OAuth | `PUT/GET/DELETE /tools/servers/{server_id}/credentials`; `POST /tools/servers/{server_id}/oauth/start`; `POST/GET /tools/servers/{server_id}/oauth/callback`; `POST /tools/servers/{server_id}/oauth/refresh`; `POST /tools/servers/{server_id}/oauth/revoke` |
| Tool definitions | `GET/POST /tools/definitions`; `PATCH/DELETE /tools/definitions/{tool_id}` |
| Skills | `GET/POST /tools/skills`; `PATCH/DELETE /tools/skills/{skill_id}` |
| Calls and approval | `POST /tools/calls`; `POST /tools/calls/{call_id}/approve`; `POST /tools/calls/{call_id}/reject`; `GET /tools/calls`; `GET /tools/calls/{call_id}` |
| Rules | `GET/POST /rules/profiles`; `GET/PATCH/DELETE /rules/profiles/{profile_id}`; `POST /rules/resolve`; `POST /rules/repos/{repo_id}/import`; `GET /rules/resolution-logs` |
| GitHub | `GET/POST /github/connections`; `PATCH/DELETE /github/connections/{connection_id}`; `POST /github/connections/{connection_id}/health`; `POST /github/connections/{connection_id}/issues/{number}/import`; `POST /github/connections/{connection_id}/pulls/{number}/import`; `GET /github/items`; `GET /github/items/{item_id}`; `POST /github/items/{item_id}/create-task`; `POST /github/items/{item_id}/create-pr-draft`; `GET /github/operations` |

### Bundles, continuity, and orchestration

| Group | Endpoints |
| --- | --- |
| Safe bundles | `POST /bundles/export`; `GET /bundles/exports`; `GET /bundles/exports/{bundle_id}`; `GET /bundles/exports/{bundle_id}/download`; `POST /bundles/import/validate`; `POST /bundles/import`; `GET /bundles/imports`; `GET /bundles/imports/{bundle_id}` |
| Continuity | `GET /continuity/bundles`; `POST /continuity/export`; `POST /continuity/import/dry-run`; `POST /continuity/import`; `GET /continuity/bundles/{bid}`; `GET /continuity/bundles/{bid}/manifest`; `GET /continuity/bundles/{bid}/references`; `GET /continuity/bundles/{bid}/validation`; `GET /continuity/bundles/{bid}/report`; `POST /continuity/validate-references`; `POST /continuity/validate-entity` |
| Workspaces | `POST/GET /workspaces`; `GET/PATCH/DELETE /workspaces/{wid}`; `POST /workspaces/{wid}/plan`; `GET /workspaces/{wid}/graph`; `POST /workspaces/{wid}/nodes`; `PATCH /workspaces/{wid}/nodes/{node_id}`; `POST /workspaces/{wid}/edges`; `DELETE /workspaces/{wid}/edges/{edge_id}`; `GET /workspaces/{wid}/timeline`; `POST /workspaces/{wid}/events`; `GET/POST /workspaces/{wid}/artifacts`; `GET /workspaces/{wid}/readiness`; `POST /workspaces/{wid}/readiness/recompute`; `GET /workspaces/{wid}/health`; `POST /workspaces/{wid}/link`; `POST /workspaces/{wid}/unlink`; `POST /workspaces/{wid}/index-memory`; `GET /workspaces/{wid}/report` |

## 9. Configuration reference

Pydantic Settings loads `.env`, accepts `NEO_*`, and ignores unknown fields. Notable settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `NEO_HOST`, `NEO_PORT` | `127.0.0.1`, `8000` | Uvicorn binding used by `app.runtime`. The container overrides host to `0.0.0.0`. |
| `NEO_DATA_DIR` | unset | When set, derives `neo.db`, workspace files/repos, and LLM config under this root. |
| `NEO_DATABASE_URL` | `sqlite:///./neo_memory.db` | Base database outside profile middleware. |
| `NEO_FRONTEND_DIR` | `app/static` | Compiled SPA directory. |
| `NEO_OLLAMA_URL` / `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama endpoint. |
| `NEO_LLM_PROVIDER`, `NEO_DEFAULT_MODEL` | `ollama`, `llama3.2:3b` | Legacy/default route. Docker overrides the model. |
| `NEO_OPENAI_COMPAT_BASE_URL`, `NEO_OPENAI_COMPAT_API_KEY_REF`, `NEO_OPENAI_COMPAT_MODEL` | empty / `OPENAI_API_KEY` / empty | OpenAI-compatible provider configuration. |
| `NEO_CHAT_TIMEOUT_SECONDS`, `NEO_CHAT_NUM_PREDICT`, `NEO_SIMPLE_CHAT_NUM_PREDICT`, `NEO_CHAT_HISTORY_TURNS` | `240`, `512`, `256`, `8` | Generation bounds and recent history window. |
| `NEO_DEFAULT_TIMEZONE` | `UTC` | Final fallback for local date/time. |
| `NEO_WEB_SEARCH_ENABLED`, `NEO_SEARCH_PROVIDER` | inferred, `disabled` | Search gate and primary provider. |
| `NEO_WEB_SEARCH_FALLBACK_PROVIDERS` | empty | Comma-separated fallback provider names. |
| `NEO_SEARXNG_URL` | `http://localhost:8080` | SearXNG base URL. |
| `TAVILY_API_KEY`, `BRAVE_API_KEY`, `SERPER_API_KEY`, `WEB_SEARCH_API_KEY` | unset | Search-provider credentials. |
| `NEO_WEB_SEARCH_MAX_RESULTS`, `NEO_WEB_FETCH_MAX_PAGES` | `5`, `3` | Search/fetch limits. |
| `NEO_WEB_FETCH_TIMEOUT_SECONDS`, `NEO_WEB_FETCH_MAX_BYTES`, `NEO_WEB_CONTEXT_MAX_TOKENS` | `8`, `1000000`, `1200` | Network/content/context bounds. |
| `NEO_WEB_CACHE_ENABLED` | `false` | Safe source cache. |
| `NEO_WORKSPACE_FILE_MAX_BYTES`, `NEO_WORKSPACE_EXTRACTED_TEXT_MAX_CHARS` | 5 MiB, 500,000 | Upload/extraction limits. |
| `NEO_WORKSPACE_REPO_MAX_FILES`, `NEO_WORKSPACE_REPO_MAX_TOTAL_BYTES`, `NEO_WORKSPACE_REPO_MAX_FILE_BYTES` | `500`, 25 MiB, 1 MiB | Managed repository import limits. |
| `NEO_SEMANTIC_RETRIEVAL_ENABLED`, `NEO_AUTO_EMBED_MEMORIES` | `false`, `false` | Optional semantic memory features. |
| `NEO_EMBEDDING_PROVIDER`, `NEO_EMBEDDING_MODEL`, `NEO_EMBEDDING_TIMEOUT_SECONDS` | `ollama`, `nomic-embed-text:latest`, `10` | Embedding configuration. |
| `NEO_CONNECTOR_MASTER_KEY`, `NEO_CONNECTOR_MASTER_KEY_FILE` | development-generated local file | Stable AES-GCM vault key. Production requires an inline key or an existing `0600` file; the Docker image points the file setting at `/app/data/secrets/connector-master-key`. |

## 10. Deployment and operations

### Local process

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
NEO_DATA_DIR="$PWD/data" .venv/bin/python -m app.runtime
```

### Docker image

The Dockerfile uses Node 22 Alpine to run `npm ci && npm run build`, then Python 3.12 slim for
the runtime. It installs Git, the `neo` package, creates data directories, removes old static
files before copying the new build, switches to UID 10001, exposes port 8000, and uses
process-only liveness as its Docker health check. Deployment automation must call
dependency-aware readiness separately before sending traffic.

Persist `/app/data`, create `/app/data/secrets/connector-master-key` with a URL-safe base64
value decoding to 32 bytes and mode `0600` before starting the production image (or supply the
inline setting), preserve that key across container replacement, and pass provider credentials
through Docker secrets/environment. Never bake secrets or profile data into the image.

### Backup and profile reset

For a targeted profile reset:

1. Resolve the exact profile UUID from `profiles/registry.db`.
2. Stop writers.
3. archive `profiles/accounts/<uuid>` to a timestamped location outside that directory;
4. calculate and record SHA-256;
5. verify the archive can be listed/read;
6. remove only that exact account directory;
7. retain `registry.db` and all other profile directories;
8. start the repaired image and unlock the account so tables/directories are recreated.

Do not delete the Docker volume when preserving account credentials. Neo does not load a
backup merely because it is present outside the active profile directory.

## 11. Automated validation

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
cd frontend
npm test
npm run build
cd ..
docker build -t neo:local .
```

The regression suite must cover:

- multi-fact extraction, typos/spelling variants, deduplication, corrections, negation,
  source edit/delete, expiry, and restart persistence;
- zero external calls for personal declarations and zero assistant-message extraction;
- false-positive internal routing for Recovery and other feature names;
- explicit internal commands still reaching their intended service;
- currency/weather/date typed intent and contextual follow-ups;
- release evidence rejecting publication dates;
- provider fallback, per-attempt tracing, snippet exclusion, and citation-marker validation;
- connector route authentication, OpenAPI/MCP/OAuth compare-and-swap/profile-bound vault/
  approval/SSRF contracts;
- chat transaction/idempotency, unique assistant upsert, lease ownership, stale recovery,
  metadata, heartbeat, and terminal-state recovery.

## 12. Manual production acceptance

Use a fresh test profile and record every result.

### A. Readiness and persistence

1. Call `/api/health/live`. Expect HTTP 200 and `{"status":"alive"}`.
2. Call `/api/health/ready`. Expect HTTP 200 and every check `ok: true`. A required connector
   should appear by name.
3. Create/unlock a profile, create a chat, restart the container, unlock again, and reopen the
   chat. Expect the transcript to persist once and no guest/account cross-contamination.

### B. Typed memory

1. Send one message containing education, occupation, two interests, a favorite, New
   Delhi/India, two goals, Python/C++/C priority, and a current game. Expect a saved
   acknowledgement only after persistence.
2. Inspect `/profile`, `/education`, `/preferences`, `/goals`, `/events`, `/activities`, and
   `/memories`. Expect every stated fact, one deduplicated graduation event, no invented date,
   goal horizon anchored to message time, and activity expiry 30 days later.
3. Repeat a fact. Expect one canonical memory with an additional provenance edge.
4. Edit one supporting message. Expect only that source contribution marked `replacement`; a
   multiply sourced fact remains. Replacing it with the same statement must reactivate/reuse
   the exact record with no rejected candidate or duplicate.
5. Delete the final supporting chat. Expect a `deletion` source detachment, a durable deleted
   tombstone, an inactive typed projection, and a lifecycle audit entry. Repeating the same
   statement must not bypass the deletion; use explicit restore when revival is intended.
6. Ask direct recall questions. Expect only stored facts; unstated name/age must not appear.
7. Restart and repeat recall. Expect identical active facts with no duplicates.

### C. Routing and chat generation

1. Send an informational paragraph containing “recovery”, “agent”, “memory”, “files”, and
   “projects”. Expect normal LLM chat, progressive content, exactly one assistant message,
   provider/model/token/timing when supplied, no Recovery lookup, and a usable composer.
2. Send “Explain recovery after an application restart.” Expect normal chat.
3. Send “Find my recoverable runs.” Expect Recovery output.
4. Refresh during a long answer. Expect polling to resume the existing generation, no
   duplicate user/assistant row, no lost text, and a usable composer at terminal state.
5. Force a model output limit. Expect bounded continuation without duplicated overlap; if it
   still cannot finish, expect an explicit incomplete response rather than truncated success.

### D. Search and live data

1. Send “I recently graduated from BITS Pilani.” Expect memory only and zero search attempts.
2. Ask the current date with `Asia/Kolkata`. Expect a local-date response and zero web calls.
3. Convert USD to INR, then ask “what about 10 USD”. Expect the follow-up to retain INR,
   decimal arithmetic, rate/reference date/provider metadata.
4. Ask New Delhi weather, then “new delhi today”. Expect resolved Open-Meteo location and
   current conditions. Ask for “New Delhi tomorrow” and expect the dated daily forecast with
   low/high and precipitation probability, not the current observation.
5. Ask a current factual question. Expect provider attempts/evidence/citations stored and only
   validated markers in the final answer.
6. Test a release announcement whose page date is visible but whose release date is not.
   Expect “no verified date announced,” never the page publication date.
7. Disable or break the primary search provider while keeping a fallback. Expect the fallback
   to continue. Break all providers. Expect an explicit unavailable/insufficient-evidence
   answer, never unsupported model knowledge.

### E. Connectors

1. Import a safe OpenAPI document from file and URL. Expect discovered read/write operations,
   schemas, capability labels, and no plaintext credentials.
2. Connect a test MCP Streamable HTTP server and run discovery. Expect real initialize and
   `tools/list`; call a read tool and expect `tools/call` plus connector provenance.
3. Repeat with legacy SSE and a trusted stdio fixture. Expect discovery/call success; an
   untrusted stdio definition must be rejected.
4. Configure each auth type. Expect status APIs to show only non-secret metadata.
5. Complete OAuth PKCE, refresh, and revoke. Expect exact state/session/redirect enforcement,
   profile-bound encrypted tokens, compare-and-swap refresh, and removal on revoke. A
   simultaneous stale refresh must fail instead of replacing the newer token.
6. Ask chat for a uniquely matched read capability. Expect automatic read execution. Ask an
   ambiguous capability. Expect no arbitrary call.
7. Request a write. Expect `pending_approval`, no external change, then execution only after
   explicit approval. Reject another call and verify it never runs.
8. Test loopback/private/redirect/oversize/timeout targets and prompt-injection-like output.
   Expect blocking or bounded untrusted output, with no instruction escalation.

### F. Workspace safety

1. Register a repository. Expect a managed copy and no original-repository modification.
2. Build code/symbol indexes and inspect routes/dependencies/references.
3. Propose a command, patch, test, and checkpoint restore. Expect validation and approval
   states before changes, bounded/redacted output, and an audit record.
4. Exercise notes, tasks, projects, files, research, bundles, continuity, agent runs,
   evaluation, and recovery. Restart and verify persisted terminal/audit states.

## 13. Troubleshooting

| Symptom | Check and expected resolution |
| --- | --- |
| `/api/health/live` fails | The process or port binding is down. Inspect container/process logs. |
| Liveness passes but readiness is 503 | Read the named check. Fix storage permissions, selected model, search provider, vault key, or required connector; do not treat 503 as an application crash. |
| Ollama fails from Docker | Verify host Ollama listens on port 11434 and `OLLAMA_BASE_URL=http://host.docker.internal:11434`; verify the selected model exists. |
| Search says unavailable | Inspect `/api/search/config`, `/api/search/test`, environment keys, provider attempt trace, DNS, and outbound connectivity. |
| Weather/currency unavailable | Verify outbound HTTPS to Open-Meteo/Frankfurter. Structured live data deliberately does not invent a fallback value. |
| Old UI appears | Confirm only the rebuilt `neo` container binds port 8000, inspect its image ID, hard-refresh once, and verify `/service-worker.js` returns the unregistering worker and HTML has no-cache headers. |
| Chat remains queued/running | Inspect generation heartbeat, worker/error fields, provider health, and logs. Reopen the chat to trigger active-generation recovery; do not submit duplicates. |
| A recovered answer duplicates | Verify the unique `chat_messages.generation_id` index, generation lease/heartbeat, and client-request ID. A stale worker must fail its fenced write rather than append. |
| No thinking text | The selected provider/model did not return a reasoning trace. The answer is valid; Neo does not synthesize hidden reasoning. |
| Connector vault cannot decrypt | Restore the exact original master key and profile storage identity. Replacing the key or copying ciphertext to another profile makes it intentionally unreadable. |
| Connector URL is blocked | Use public HTTPS or explicitly mark an actual loopback development connector trusted. Private-network bypasses are not supported. |
| Database locked | Verify a single active container/process owns the profile, storage supports file locking, and WAL files are preserved. |

# Neo Backend

## Purpose and architecture

Neo’s backend is a local FastAPI application that exposes the platform API, coordinates workflow services, persists local state, and optionally serves the compiled web application. It is designed around explicit service boundaries: route modules validate and translate HTTP requests, services apply business logic and safety checks, stores/repositories manage persistence, and models/schemas define durable and public data shapes.

```text
HTTP API / CLI clients
        |
FastAPI route modules (app/api/routes/)
        |
services: orchestration, providers, safety, persistence workflows
        |
SQLAlchemy models, SQLite databases, local files, managed repository copies
        |
optional local/external systems: LLMs, search providers, GitHub, MCP-style tools
```

This guide documents the backend, CLI, runtime configuration, and operational behavior. See [frontend.md](frontend.md) for the web client.

## Running the service

Neo requires Python 3.12 or later. Install the package and developer tooling in an isolated environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The application factory is `app.main:create_app`; `app.main:app` is the ready-to-run instance. `GET /api/health` is the container health-check endpoint. When a built frontend exists at `NEO_FRONTEND_DIR` (default `app/static`), FastAPI serves its assets and uses its `index.html` as the SPA fallback. Paths below `/api` are excluded from this fallback and return a normal API 404 when unknown.

The Dockerfile creates a production image with the compiled frontend, Python runtime, and Git installed. It runs as a non-root `neo` user, exposes port `8000`, and persists `/app/data`. A typical local run is:

```bash
docker build -t neo:local .
docker run --rm -p 8000:8000 -v neo_data:/app/data neo:local
```

## Startup lifecycle

`create_app()` configures CORS for the local Vite development and preview origins, applies profile-database middleware, registers route groups under `/api`, initializes all storage tables, seeds built-in tools, agents, and evaluation definitions, ensures default LLM registry entries, and scans recoverable run state. At shutdown it removes temporary guest profile storage.

`ProfileDatabaseMiddleware` resolves the request session and executes authenticated requests against that profile’s local database and storage directory. This preserves local separation between profiles while allowing unauthenticated health and bootstrap requests to use base configuration.

## Source layout

```text
app/
  main.py                    application factory, route registration, static hosting
  api/routes/                HTTP route groups
  core/config.py             environment-backed settings and profile overrides
  db/                        database setup and sessions
  models/                    SQLAlchemy domain models
  schemas/                   Pydantic request/response schemas
  services/                  domain logic, stores, adapters, and safety controls
  repositories/              reusable persistence abstractions
  cli/                       `neo` CLI and terminal UI
```

Most service areas use a `service.py` coordinator, a `store.py` persistence adapter, `types.py` records/enums, and focused helpers such as `safety.py`, `redaction.py`, `executor.py`, or `planner.py`. Follow that separation when extending the platform: route code should not contain policy or direct persistence logic, and service code should not depend on FastAPI request objects.

## API surface

All primary routes are mounted under `/api`. FastAPI provides the authoritative interactive schema at `/docs` when enabled by the runtime. The following map names the stable functional route groups; exact request/response schemas live alongside each route in `app/api/routes/` and `app/schemas/`.

| Area | Base path | Responsibilities |
| --- | --- | --- |
| Accounts | `/account-profiles` | Local profiles, guest sessions, unlock, and session lifecycle. |
| Memory and chat | root plus `/memory` | Conversations, streamed messages, extracted memories, profile facts, preferences, goals, events, lifecycle, reflection, and sidebar data. |
| Work organization | `/projects`, `/tasks`, `/notes`, `/files`, `/artifacts` | Project/task/note lifecycle, attachments, links, summaries, and artifact download. |
| Research | `/research`, `/search`, `/web`, `/web-search` | Search settings, provider tests, fetch/search, research planning/runs, evidence, claims, conflicts, citation validation, and reliable-search caches. |
| Models | `/llms`, `/llm`, `/providers/runtime` | Legacy/model configuration, LLM registry, provider health, completion/streaming, usage, budgets, and rate limits. |
| Agents | `/agents`, `/agentic`, `/coding-agent` | Definitions, delegation, objective-based runs, plan/step/continue/reflect control, coding actions, and approvals. |
| Repositories | `/repos`, `/code-index`, `/symbols`, `/lsp` | Managed copies, file catalogues, indexing, symbols, routes, dependencies, navigation, and language-server integration. |
| Guarded delivery | `/patches`, `/command-sandbox`, `/test-runner`, `/git` | Patch proposals/application, approved argv execution, test runs, and local checkpoints. |
| Platform controls | `/rules`, `/tools`, `/bundles`, `/continuity`, `/recovery` | Rule resolution, tool/skill definitions and calls, import/export, portable continuity, recovery scans, resume/retry/fork/repair. |
| Quality and coordination | `/evals`, `/workspaces`, `/integration`, `/github` | Evaluation suites/runs/baselines, workspace graphs/readiness/reports, integration checks, and GitHub connection/import/draft workflows. |
| Health | `/health` | Runtime health status. |

Use resource-oriented endpoints for persisted entities and action endpoints for stateful workflows. Mutations that require human confirmation use explicit action paths such as `/approve`, `/apply`, `/execute`, `/restore`, `/resume`, or `/cancel`; callers must pass the confirmation payload expected by the corresponding route. Do not treat an HTTP 200 as permission to bypass an approval workflow.

## Complete endpoint reference

Every path below is relative to `/api`. Braces identify a path parameter. `GET` endpoints read data unless their name says otherwise; `POST`, `PATCH`, and `PUT` endpoints accept a JSON request body unless stated as an upload. `DELETE` endpoints commonly return `204 No Content`. FastAPI's OpenAPI document is the source of truth for field-level request and response schemas at runtime.

### Identity, conversation, and personal memory

| Area | Endpoints |
| --- | --- |
| Account profiles | `GET, POST /account-profiles`; `POST /account-profiles/guest`; `POST /account-profiles/{profile_id}/unlock`; `GET /account-profiles/session/current`; `POST /account-profiles/session/end` |
| Chat and extraction | `POST /conversation`; `POST /extract-memory`; `POST /retrieve-context`; `POST /memory/review`; `POST /reflection/run`; `GET /sidebar`; `POST /chats`; `GET /chats/{chat_id}`; `POST /chats/{chat_id}/messages`; `POST /chats/{chat_id}/messages/stream`; `PATCH /chats/{chat_id}/messages/{message_id}`; `DELETE /chats/{chat_id}` |
| Core memory records | `GET /goals`; `PATCH, DELETE /goals/{goal_id}`; `GET, POST /projects`; `PATCH, DELETE /projects/{project_id}`; `DELETE /projects/{project_id}/memory`; `GET, POST /chat-projects`; `PATCH, DELETE /chat-projects/{project_id}`; `DELETE /chat-projects/{project_id}/memory`; `GET /events`; `PATCH, DELETE /events/{event_id}` |
| Durable-memory lifecycle | `GET /memory` and `GET /memories`; `PATCH /memories/{memory_id}`; `DELETE /memories/{memory_id}`; `POST /memories/{memory_id}/archive`, `/supersede`, and `/restore`; `GET /memories/{memory_id}/lifecycle`; `POST /memory/lifecycle/age`; `POST /memory/lifecycle/maintenance`; `GET /memory/candidates`; `POST /memory/explain` |
| Profile and preferences | `GET /profile`; `PATCH, DELETE /profile/{profile_id}`; `GET /preferences`; `PATCH, DELETE /preferences/{preference_id}` |
| Indexed workspace memory | `POST /memory/index`; `POST /memory/retrieve`; `GET, POST /memory/items`; `GET, PATCH, DELETE /memory/items/{item_id}`; `GET /memory/scopes/{scope_type}/{scope_id}`; `GET /memory/retrievals`; `GET /memory/retrievals/{retrieval_id}`; `POST /memory/prune/preview`; `POST /memory/prune/apply` |
| Context-memory compaction | `GET /context-memory/summaries`; `GET /context-memory/summaries/{summary_id}`; `POST /context-memory/preview`; `POST /context-memory/compact`; `GET /context-memory/scopes/{scope_type}/{scope_id}`; `GET, POST /context-memory/scopes/{scope_type}/{scope_id}/events` |

The root-level conversation routes power the primary chat experience and may stream newline-delimited JSON events. The `/memory` prefix is shared by memory retrieval endpoints; it is distinct from the legacy durable-memory endpoints above. Clients should use the current response schema rather than assuming that similarly named resources have an identical shape.

### Workspace content and planning

| Area | Endpoints |
| --- | --- |
| Workspace projects | `GET, POST /projects`; `GET /projects/tags`; `GET /projects/notes/{note_id}/projects`; `GET, PATCH, DELETE /projects/{project_id}`; `GET, POST /projects/{project_id}/tasks`; `POST /projects/{project_id}/pin`; `POST /projects/{project_id}/archive`; `GET, POST /projects/{project_id}/notes`; `DELETE /projects/{project_id}/notes/{note_id}` |
| Workspace tasks | `GET, POST /tasks`; `GET /tasks/tags`; `GET /tasks/notes/{note_id}/tasks`; `GET, PATCH, DELETE /tasks/{task_id}`; `POST /tasks/{task_id}/status`, `/pin`, and `/archive`; `GET, POST /tasks/{task_id}/notes`; `DELETE /tasks/{task_id}/notes/{note_id}`; `GET /tasks/{task_id}/agent-runs` |
| Notes | `GET, POST /notes`; `GET /notes/tags`; `GET, PATCH, DELETE /notes/{note_id}`; `POST /notes/{note_id}/pin`; `POST /notes/{note_id}/archive` |
| Files and artifacts | `POST /files/upload` (multipart); `GET /files`; `GET /files/{file_id}`; `GET /files/{file_id}/download`; `DELETE /files/{file_id}`; `POST /files/{file_id}/links`; `DELETE /files/{file_id}/links/{link_id}`; `POST /files/{file_id}/summarize`; `POST /artifacts`; `GET /artifacts`; `GET /artifacts/{artifact_id}`; `GET /artifacts/{artifact_id}/download` |
| Workspace orchestration | `GET, POST /workspaces`; `GET, PATCH, DELETE /workspaces/{wid}`; `POST /workspaces/{wid}/plan`; `GET /workspaces/{wid}/graph`; `POST /workspaces/{wid}/nodes`; `PATCH /workspaces/{wid}/nodes/{node_id}`; `POST /workspaces/{wid}/edges`; `DELETE /workspaces/{wid}/edges/{edge_id}`; `GET, POST /workspaces/{wid}/timeline`; `GET, POST /workspaces/{wid}/artifacts`; `GET /workspaces/{wid}/readiness`; `POST /workspaces/{wid}/readiness/recompute`; `GET /workspaces/{wid}/health`; `POST /workspaces/{wid}/link`, `/unlink`, and `/index-memory`; `GET /workspaces/{wid}/report` |

### Research, search, and providers

| Area | Endpoints |
| --- | --- |
| Search configuration and direct search | `GET, POST /search/config`; `GET, POST /search/test`; `GET /search/providers`; `POST /search`; `POST /search/query`; `POST /search/fetch` |
| Web utility API | `POST /web/search`; `POST /web/fetch`; `POST /web/answer` |
| Reliable web-search runs | `POST /web-search/plan`; `POST /web-search/run`; `GET /web-search/runs`; `GET /web-search/runs/{run_id}`; `GET /web-search/runs/{run_id}/sources`, `/evidence`, and `/conflicts`; `POST /web-search/runs/{run_id}/refresh`; `GET /web-search/cache`; `DELETE /web-search/cache/{cache_id}` |
| Research mode | `POST /research/plan`; `POST /research/run`; `GET /research/runs`; `GET /research/runs/{run_id}`; `GET /research/runs/{run_id}/evidence`, `/claims`, `/conflicts`, and `/report`; `POST /research/runs/{run_id}/continue`, `/refresh`, and `/validate-citations`; `DELETE /research/runs/{run_id}` |
| Legacy research jobs | `POST /research/start`; `GET /research` and `/research/list`; `DELETE /research/clear`; `GET /research/{job_id}`; `GET /research/{job_id}/status`, `/report`, and `/events`; `POST /research/{job_id}/save-to-note`; `POST /research/{job_id}/cancel` |
| Legacy LLM configuration | `GET /llms`; `PUT /llms/{config_id}`; `PUT /llms/active/select`; `POST /llms/{config_id}/test`; `DELETE /llms/{config_id}` |
| LLM registry | `GET, POST /llm/providers`; `PATCH, DELETE /llm/providers/{provider_id}`; `GET, POST /llm/models`; `PATCH, DELETE /llm/models/{model_id}`; `GET /llm/routes`; `PATCH /llm/routes/{route_name}`; `POST /llm/health`; `GET /llm/usage` |
| Provider runtime | `GET /providers/runtime/status`; `POST /providers/runtime/health-check`; `GET /providers/runtime/health`; `GET /providers/runtime/requests`; `GET /providers/runtime/requests/{request_id}`; `POST /providers/runtime/complete`; `POST /providers/runtime/stream/start`; `GET /providers/runtime/stream/{request_id}`; `POST /providers/runtime/stream/{request_id}/cancel`; `GET /providers/runtime/rate-limits`; `GET /providers/runtime/usage` |

The `research` router deliberately contains both the persisted research-mode API and older job-oriented endpoints. Callers should choose one flow for a run and preserve the run/job ID returned by its creation endpoint. Search configuration must be tested against an installed provider; disabled or unavailable providers return an explicit degraded/error result rather than synthetic evidence.

### Agents, coding, and controlled delivery

| Area | Endpoints |
| --- | --- |
| Agent definitions and delegation | `GET, POST /agents/definitions`; `GET, PATCH, DELETE /agents/definitions/{agent_id}`; `POST /agents/definitions/reset-builtins`; `POST /agents/delegations`; `GET /agents/delegations`; `GET, PATCH /agents/delegations/{delegation_id}` |
| Task agents | `POST /agents/plan-tasks`; `POST /agents/runs/from-objective`; `POST /agents/runs`; `GET /agents/runs`; `GET /agents/runs/{run_id}`; `POST /agents/runs/{run_id}/cancel`; `POST /agents/runs/{run_id}/steps/{step_id}/approve`; `POST /agents/runs/{run_id}/save-to-note` |
| Agentic Core | `POST /agentic/runs`; `GET /agentic/runs`; `GET /agentic/runs/{run_id}`; `POST /agentic/runs/{run_id}/plan`, `/step`, `/continue`, `/reflect`, and `/stop`; `GET /agentic/runs/{run_id}/steps`; `GET /agentic/runs/{run_id}/context` |
| Coding Agent | `POST /coding-agent/runs`; `GET /coding-agent/runs`; `GET /coding-agent/runs/{coding_run_id}`; `POST /coding-agent/actions/{action_request_id}/approve` and `/reject`; `POST /coding-agent/runs/{coding_run_id}/revise-patch`, `/cancel`, and `/commands/propose` |
| Patch application | `POST /patches/propose`; `GET /patches/applications`; `GET /patches/applications/{application_id}`; `GET /patches/applications/{application_id}/download`; `POST /patches/{artifact_id}/validate-apply`; `POST /patches/{artifact_id}/apply` |
| Command Sandbox | `GET /command-sandbox/runs`; `GET /command-sandbox/runs/{run_id}`; `POST /command-sandbox/validate`; `POST /command-sandbox/propose`; `POST /command-sandbox/runs/{run_id}/approve`, `/execute`, and `/cancel` |
| Test Runner | `GET, POST /test-runner/repos/{repo_id}/commands`; `PATCH, DELETE /test-runner/commands/{command_id}`; `POST /test-runner/repos/{repo_id}/detect`; `POST /test-runner/commands/{command_id}/run`; `GET /test-runner/runs`; `GET /test-runner/runs/{run_id}` |
| Git checkpoints | `GET /git/repos/{repo_id}/status`; `POST /git/repos/{repo_id}/init`; `GET /git/repos/{repo_id}/diff`; `POST /git/repos/{repo_id}/checkpoints`; `GET /git/repos/{repo_id}/checkpoints`; `GET /git/checkpoints/{checkpoint_id}`; `POST /git/checkpoints/{checkpoint_id}/restore`; `GET /git/repos/{repo_id}/operations` |

### Repositories, tools, quality, and integrations

| Area | Endpoints |
| --- | --- |
| Managed repositories | `POST /repos/register`; `GET /repos`; `GET /repos/{repo_id}`; `GET /repos/{repo_id}/files`; `GET /repos/{repo_id}/files/{repo_file_id}`; `DELETE /repos/{repo_id}` |
| Code index | `POST /code-index/repos/{repo_id}/build`; `GET /code-index/repos/{repo_id}`; `GET /code-index/repos/{repo_id}/symbols`, `/search`, `/routes`, and `/dependencies`; `GET /code-index/symbols/{symbol_id}`; `GET /code-index/repos/{repo_id}/files/{repo_file_id}/summary` |
| Symbol awareness | `POST /symbols/repos/{repo_id}/build`; `GET /symbols/repos/{repo_id}`; `GET /symbols/repos/{repo_id}/definition`, `/references`, `/files/{repo_file_id}/document-symbols`, and `/files/{repo_file_id}/related-files`; `GET /symbols/{symbol_id}/references` and `/context` |
| Language Server Protocol | `GET /lsp/status`; `GET /lsp/servers`; `POST /lsp/workspaces/{workspace_id}/start` and `/stop`; `GET /lsp/workspaces/{workspace_id}/diagnostics`; `POST /lsp/workspaces/{workspace_id}/{action}` |
| Rules | `GET, POST /rules/profiles`; `GET, PATCH, DELETE /rules/profiles/{profile_id}`; `POST /rules/resolve`; `POST /rules/repos/{repo_id}/import`; `GET /rules/resolution-logs` |
| Tools and skills | `GET, POST /tools/servers`; `PATCH, DELETE /tools/servers/{server_id}`; `POST /tools/servers/{server_id}/health` and `/discover`; `GET, POST /tools/definitions`; `PATCH, DELETE /tools/definitions/{tool_id}`; `GET, POST /tools/skills`; `PATCH, DELETE /tools/skills/{skill_id}`; `POST /tools/calls`; `POST /tools/calls/{call_id}/approve` and `/reject`; `GET /tools/calls`; `GET /tools/calls/{call_id}` |
| Evaluation | `GET, POST /evals/suites`; `GET /evals/suites/{suite_id}`; `POST /evals/suites/{suite_id}/run`; `GET /evals/runs`; `GET /evals/runs/{run_id}`; `GET /evals/runs/{run_id}/cases` and `/report`; `POST /evals/runs/{run_id}/set-baseline`; `GET /evals/baselines`; `GET /evals/compare`; `DELETE /evals/runs/{run_id}` |
| Bundles | `POST /bundles/export`; `GET /bundles/exports`; `GET /bundles/exports/{bundle_id}`; `GET /bundles/exports/{bundle_id}/download`; `POST /bundles/import/validate` (multipart); `POST /bundles/import` (multipart); `GET /bundles/imports`; `GET /bundles/imports/{bundle_id}` |
| Continuity | `GET /continuity/bundles`; `POST /continuity/export`; `POST /continuity/import/dry-run`; `POST /continuity/import`; `GET /continuity/bundles/{bid}`, `/manifest`, `/references`, `/validation`, and `/report`; `POST /continuity/validate-references`; `POST /continuity/validate-entity` |
| Recovery | `GET /recovery/runs`; `GET /recovery/runs/{run_type}/{run_id}`; `POST /recovery/runs/{run_type}/{run_id}/resume`, `/retry`, `/fork`, and `/repair-state`; `GET /recovery/events` |
| GitHub | `GET, POST /github/connections`; `PATCH, DELETE /github/connections/{connection_id}`; `POST /github/connections/{connection_id}/health`; `POST /github/connections/{connection_id}/issues/{number}/import` and `/pulls/{number}/import`; `POST /github/items/{item_id}/create-task` and `/create-pr-draft`; `GET /github/items`; `GET /github/items/{item_id}`; `GET /github/operations` |
| Integration and health | `GET /integration/status`; `POST /integration/validate`; `GET /integration/report`; `POST /integration/smoke`; `GET /health` |

## Data, profiles, and local storage

The default database URL is `sqlite:///./neo_memory.db`. Supplying `NEO_DATA_DIR` creates the directory and, unless individually overridden, relocates the SQLite database, managed workspace files, managed repository copies, and LLM configuration into that root. In container deployments this is `/app/data`.

Profile-aware requests receive a profile-specific database URL and storage root through context variables. Service code must obtain settings through `get_settings()` rather than caching paths at import time, because the active profile can differ per request. `get_base_settings()` is available only for true process-wide paths.

Uploaded workspace files and derived artifacts are stored locally. Repository workflows operate on registered **managed copies**, not on the user’s original checkout. Export/bundle/continuity workflows serialize controlled records and apply redaction to sensitive values and host-specific paths.

### Database engine and initialization

`app/db/session.py` creates SQLAlchemy engines. SQLite connections use `check_same_thread=False`, a 30-second busy timeout, foreign keys, and WAL journal mode so normal local reads can proceed while short writes occur. `SessionLocal()` is profile-aware: it resolves the active database URL for the current request and maintains a sessionmaker per URL. The base schema is created with SQLAlchemy metadata; service stores add their own SQLite tables with idempotent `CREATE TABLE IF NOT EXISTS` statements.

This is a SQLite-first application with in-place compatibility helpers, not a migration-framework-based deployment. At initialization, Neo also adds missing chat-message metadata columns, durable-memory traceability/lifecycle columns, and the `memory_embeddings` table when opening older databases. Back up `NEO_DATA_DIR` before upgrading production data.

### ORM-managed domain tables

These SQLAlchemy models are declared in `app/models/`. Every `TimestampMixin` model has `created_at` and `updated_at`; its timestamps are generated by the database/default model configuration.

| Table | Model and important fields | Relationships and purpose |
| --- | --- | --- |
| `chats` | `Chat`: `id`, `title`, optional `project_id`, `archived`, timestamps | A conversation thread. A project deletion leaves chats unassigned; deleting a chat deletes its messages. Indexed by project/update and archived/update. |
| `chat_messages` | `ChatMessage`: `id`, `chat_id`, `role`, `content`, token counts, `duration_ms`, optional `thinking`, `created_at` | Ordered transcript messages. Token/timing/thinking fields make provider output auditable. |
| `projects` | `Project`: `id`, `name`, `description`, status, priority, timestamps | Legacy/personal-memory project model. Links many-to-many to memories and events and one-to-many to chats. |
| `goals` | `Goal`: `id`, goal/description, priority, status, `completed_at`, timestamps | Long-lived personal goals; priority is constrained to 1–10. |
| `memories` | `Memory`: text, `memory_type`, importance, confidence, source traceability, canonical slot, lifecycle status, supersession pointers, activity/access state | Durable accepted memory rather than raw transcript. Supports self-referential supersession, project links, and one embedding record. |
| `memory_candidates` | `MemoryCandidate`: candidate text/type, confidence, importance, reasoning, review status/timestamps, optional accepted memory | Review queue between extraction and durable memory acceptance. |
| `memory_embeddings` | `MemoryEmbedding`: memory PK/FK, provider/model, dimensions, serialized vector, hash, status/error, embedded time | Best-effort semantic retrieval metadata. The vector is stored as JSON; a missing or failed embedding is explicit. |
| `memory_lifecycle_audit` | `MemoryLifecycleAudit`: memory/action, previous/new status, reason, related memory, source sentence, creation time | Append-only audit trail for archive, supersede, restore, aging, and maintenance actions. |
| `profile` | `ProfileFact`: key/value, confidence, active flag, timestamps | Durable identity facts with active-key indexing. |
| `preferences` | `Preference`: category/value, confidence, importance, active flag, timestamps | Evolving user preferences; importance is 1–10. |
| `events` | `Event`: event/description, optional event date, importance, created time | Timeline facts, linkable to projects. |
| `reflections` | `Reflection`: reflection text, importance, timestamps | Higher-level insight generated by reflection workflows. |
| `memory_project_links` | Association table: `memory_id`, `project_id` | Cascading many-to-many memory/project link. |
| `event_project_links` | Association table: `event_id`, `project_id` | Cascading many-to-many event/project link. |

`MemoryType`, `GoalStatus`, `ProjectStatus`, and candidate enums are string-backed values. The database enforces confidence ranges of 0–1 and priority/importance ranges of 1–10 where applicable. Treat the database as private implementation state: use API endpoints and service stores rather than directly editing rows.

### Service-owned SQLite tables

Feature services manage additional tables in the same active profile database. Their schemas are deliberately local and often store JSON for flexible run metadata, policy decisions, reports, and provider payload summaries.

| Domain | Tables | What they retain |
| --- | --- | --- |
| Accounts | `account_profiles` (base registry database) | Named/password-protected and guest profile metadata, with each active profile using a separate database/storage root. |
| Workspace organization | `workspace_projects`, `workspace_project_tags`, `workspace_project_links`, `workspace_project_notes`; `workspace_tasks`, `workspace_task_tags`, `workspace_task_links`, `workspace_task_notes`; `notes`, `note_tags`, `note_links` | The current workspace project/task/note system, including filters, pin/archive/deletion flags, hierarchy, tags, and links. |
| Files and repository analysis | `workspace_files`, `workspace_file_links`, `workspace_artifacts`, `workspace_patch_applications`, `workspace_patch_application_files`, `workspace_repos`, `workspace_repo_files`, `workspace_code_indexes`, `workspace_code_symbols`, `workspace_code_dependencies`, `workspace_code_file_summaries`, `workspace_code_references`, `workspace_code_symbol_relationships`, `workspace_code_related_files` | Upload metadata, artifacts, patches, managed repository manifests, and derived index/symbol/relationship data. File contents and managed copies remain on disk. |
| Agent execution | `workspace_agent_runs`, `workspace_agent_steps`, `workspace_agent_artifacts`; `workspace_agent_definitions`, `workspace_agent_delegations`; agentic-core and coding-agent run/step/action tables | Agent plans, statuses, actions, artifacts, definition configuration, delegation lineage, and recovery-relevant progress. |
| Commands, tests, and Git | `workspace_command_runs`; test command/run tables; `workspace_git_repos`, `workspace_git_checkpoints`, `workspace_git_operations` | Proposed/approved command argv, policy decisions, output redaction, test history, local Git state, checkpoints, restore attempts, and operation audit. |
| Research and retrieval | `workspace_memory_items`, `workspace_memory_links`, `workspace_memory_retrievals`; context-memory tables; `workspace_research_runs`, `workspace_research_claims`, `workspace_research_evidence`, `workspace_research_reports`, `workspace_research_conflicts`; `workspace_web_search_runs`, `workspace_web_sources`, `workspace_web_evidence`, `workspace_web_conflicts`, `workspace_web_source_cache` | Scoped retrieval records, compaction summaries, research evidence/claims/reports/conflicts, and reliable-web-search source/cache records. |
| Providers and evaluation | LLM registry tables; `workspace_provider_requests`, `workspace_provider_health_checks`, `workspace_provider_rate_limits`; `workspace_eval_suites`, `workspace_eval_runs`, `workspace_eval_cases`, `workspace_eval_case_results`, `workspace_eval_baselines` | Provider/model/routes, non-secret provider audit and health state, rate counters, evaluation fixtures/results, reports, and comparison baselines. |
| Governance and integrations | `workspace_rule_profiles`, `workspace_rule_resolution_logs`; tool server/definition/skill/call tables; `workspace_github_connections`, `workspace_github_items`, `workspace_github_operations`; recovery event tables | Rules resolution, discovery and approval records for tools, GitHub configuration/item/operation audit, and interrupted-run recovery state. |
| Portability and coordination | export/import bundle tables; continuity tables; workspace-orchestration workspace/node/edge/timeline/artifact/readiness tables; LSP tables | Redacted archive manifests, continuity references/validation, delivery graph/report state, and language-server workspace state. |

The exact service-owned table list evolves with capabilities. Consult the appropriate `app/services/<domain>/store.py` for the field-level schema and use its service API for writes. Most tables have indexes tailored to their listing/filter paths, such as run status and time, workspace IDs, repository IDs, and source/reference keys.

#### Exhaustive service-table catalog

For database administrators and backup reviewers, the following is the current complete catalog of service-created table names. It supplements the grouped descriptions above; a table may contain JSON columns so the service store remains the authoritative field-level reference.

| Subsystem | Tables |
| --- | --- |
| Account registry | `account_profiles` |
| Notes, projects, and tasks | `notes`, `note_tags`, `note_links`, `workspace_projects`, `workspace_project_tags`, `workspace_project_links`, `workspace_project_notes`, `workspace_tasks`, `workspace_task_tags`, `workspace_task_notes`, `workspace_task_links` |
| Files and code intelligence | `workspace_files`, `workspace_file_links`, `workspace_artifacts`, `workspace_patch_applications`, `workspace_patch_application_files`, `workspace_repos`, `workspace_repo_files`, `workspace_code_indexes`, `workspace_code_symbols`, `workspace_code_dependencies`, `workspace_code_file_summaries`, `workspace_code_references`, `workspace_code_symbol_relationships`, `workspace_code_related_files` |
| General agents | `workspace_agent_runs`, `workspace_agent_steps`, `workspace_agent_artifacts`, `workspace_agent_definitions`, `workspace_agent_delegations`, `workspace_agent_action_requests`, `workspace_agentic_runs`, `workspace_agentic_steps`, `workspace_coding_agent_runs`, `workspace_agent_recovery_events` |
| Commands, tests, and local Git | `workspace_command_runs`, `workspace_test_commands`, `workspace_test_runs`, `workspace_git_repos`, `workspace_git_checkpoints`, `workspace_git_operations` |
| Memory and context | `workspace_memory_items`, `workspace_memory_links`, `workspace_memory_retrievals`, `workspace_context_summaries`, `workspace_context_events` |
| Research and web evidence | `research_jobs`, `workspace_research_runs`, `workspace_research_claims`, `workspace_research_evidence`, `workspace_research_reports`, `workspace_research_conflicts`, `workspace_web_search_runs`, `workspace_web_sources`, `workspace_web_evidence`, `workspace_web_conflicts`, `workspace_web_source_cache` |
| LLM/provider and evaluation | `workspace_llm_providers`, `workspace_llm_models`, `workspace_llm_routes`, `workspace_llm_calls`, `workspace_provider_requests`, `workspace_provider_health_checks`, `workspace_provider_rate_limits`, `workspace_eval_suites`, `workspace_eval_runs`, `workspace_eval_cases`, `workspace_eval_case_results`, `workspace_eval_baselines` |
| Rules, tools, and integrations | `workspace_rule_profiles`, `workspace_rule_resolution_logs`, `workspace_tool_servers`, `workspace_tool_definitions`, `workspace_skill_definitions`, `workspace_tool_calls`, `workspace_github_connections`, `workspace_github_items`, `workspace_github_operations` |
| Bundles, continuity, LSP, and orchestration | `workspace_export_bundles`, `workspace_import_bundles`, `workspace_continuity_bundles`, `workspace_continuity_references`, `workspace_continuity_validation_results`, `workspace_lsp_sessions`, `workspace_lsp_diagnostics`, `workspace_orchestration_workspaces`, `workspace_orchestration_nodes`, `workspace_orchestration_edges`, `workspace_orchestration_events`, `workspace_orchestration_artifacts`, `workspace_orchestration_readiness_checks` |

### Filesystem data layout and retention

When `NEO_DATA_DIR` is set, Neo places `neo.db`, `workspace_files/`, `workspace_repos/`, and `neo_llms.json` below it unless more specific variables override them. Files uploaded through the API retain metadata in SQLite and bytes in the workspace-files directory. Repository registration copies material into the workspace-repositories directory after size/path safety checks. Bundles, continuity exports, reports, and download artifacts are stored under service-managed local paths and represented by database metadata.

The container image mounts `/app/data` as a volume. Preserve that volume to retain profiles, database records, managed copies, artifacts, and exported state. Removing it is a destructive reset of local Neo data; it is not recoverable through an API endpoint.

## Configuration

Settings use Pydantic Settings with the `NEO_` prefix and optional `.env` file. Important settings include:

| Variable | Default / purpose |
| --- | --- |
| `NEO_HOST`, `NEO_PORT` | Local bind settings (`127.0.0.1`, `8000`). |
| `NEO_DATA_DIR` | Root for local database and workspace data. |
| `NEO_DATABASE_URL` | Explicit SQLite/SQLAlchemy database URL. |
| `NEO_FRONTEND_DIR` | Compiled frontend directory served by FastAPI. |
| `OLLAMA_BASE_URL` | Ollama endpoint; accepted as an alias for Neo’s Ollama URL. |
| `NEO_LLM_PROVIDER`, `NEO_DEFAULT_MODEL` | Initial model provider and default model. |
| `NEO_OPENAI_COMPAT_BASE_URL`, `NEO_OPENAI_COMPAT_MODEL` | OpenAI-compatible provider configuration. |
| `NEO_OPENAI_COMPAT_API_KEY_REF` | Environment-variable name holding a provider key, not the key itself. |
| `NEO_SEARCH_PROVIDER` | Search provider; `disabled` leaves research/search in an explicit degraded state. |
| `NEO_SEARXNG_URL` | SearXNG instance URL where configured. |
| `NEO_WEB_SEARCH_*` | Search results, fetch limits, caching, timeouts, provider credentials, and fallbacks. |
| `NEO_WORKSPACE_*` | Managed repository/file size and extraction limits. |

Provider secrets belong in process environment variables. Neo stores references to those variables where appropriate and redacts secret material from recorded provider errors. Do not commit `.env` files, provider keys, or generated runtime data.

## Service domains

The service layer organizes the platform into focused domains:

- `projects`, `tasks`, `notes`, `files`, and `bundles` manage local work records and transferable snapshots.
- Memory, retrieval, reflection, lifecycle maintenance, context memory, and continuity preserve and retrieve scoped knowledge over time.
- `research`, `web_search`, `web_fetch`, and source-citation helpers create bounded research runs backed by recorded evidence.
- `llm_registry` and `provider_runtime` maintain providers/models and apply routing, health, rate limits, budgets, retries, streaming, usage accounting, and redaction.
- `agents`, `agent_framework`, `agentic_core`, and `coding_agent` coordinate persisted agent plans, steps, delegation, and approved coding actions.
- `repos`, `code_index`, `symbol_awareness`, and `lsp` provide code-aware managed-workspace context.
- `patches`, `command_sandbox`, `test_runner`, and `git` create controlled execution and modification paths.
- `rules`, `tools`, `evaluation`, `workspace_orchestration`, `recovery`, `github`, and `integration` provide policy, extensibility, validation, recovery, and integration support.

When creating a service, define its domain types first, isolate storage access, validate unsafe input before it reaches an executor, and persist enough audit/status information for the UI and recovery scanner to explain what happened.

## CLI and TUI

Installing the package registers `neo` via `app.cli.main:main`. The CLI is an API client for headless operations and supports an API URL option or `NEO_API_URL`. Common groups include `research`, `providers`, `eval`, `workspace`, `continuity`, `coding`, `agentic`, `recovery`, `rules`, `tools`, `skills`, `tests`, `git`, and `bundles`; `neo tui` opens the terminal interface.

Use `--json` (or `NEO_CLI_OUTPUT=json`) for automation-friendly output. A CLI `--yes` flag can suppress only the command’s local prompt; it does not bypass backend approval gates. Scripts should therefore inspect returned status and approval requirements rather than assuming a command has executed a destructive action.

## Safety and operational model

Neo’s safety controls are product behavior, not merely UI conventions:

- Original repositories are not modified; guarded work runs in managed copies.
- Patch workflows validate paths, hashes, hunks, and metadata before a confirmed atomic apply. Applying a patch does not implicitly run tests or create a checkpoint.
- Command Sandbox and Test Runner use saved, allowlisted argv commands and persist approval and execution records.
- Git is limited to local checkpoint operations in managed copies. Remote Git commands, arbitrary shell access, package installation, and automatic commits are outside the controlled workflow.
- Tool calls, external writes, test runs, patches, and checkpoints retain audit state and can wait for approval.
- Provider, search, and integration failures should be represented as persisted degraded/error states rather than fabricated success.
- Recovery scans incomplete or interrupted runs and exposes resume, retry, fork, and repair actions with confirmation.

Respect these boundaries in routes, services, and new integrations. A feature that can alter files, invoke a network service, or spend provider budget must have a clear persisted state transition, an audit trail, and the appropriate approval check.

## Development and verification

Run backend tests and linting from the repository root:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check app tests
```

For a backend change, verify the focused service/unit behavior, the related API endpoint, and the frontend or CLI flow that consumes it. For persistence changes, verify profile isolation and clean initialization. For external-provider features, test both a configured provider and the unavailable/degraded path. For approval-controlled workflows, test proposal, rejection/cancellation, approval, execution, persisted audit detail, and recovery after interruption.

## Adding an API capability

1. Define or update Pydantic schemas and domain types.
2. Implement service logic, including validation, persistence, redaction, and safety checks.
3. Add route handlers that map HTTP input/output to the service without embedding business policy.
4. Register the router in `app/main.py` under `/api` if it is a new route group.
5. Initialize any new storage tables during application startup and seed only deterministic built-ins.
6. Add tests for normal, invalid, unavailable-provider, approval-required, and recovery-relevant states.
7. Add the corresponding `frontend/src/api.js` method and a transparent UI status surface where applicable.

This sequence keeps the HTTP layer small, makes the capability operable from both the web app and CLI, and preserves Neo’s auditable local-first behavior.

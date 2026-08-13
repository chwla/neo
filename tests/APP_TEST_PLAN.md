# Neo — application test plan

Goal: get Neo to the point where it can be trusted in real daily use. This document
covers the **whole application**. The memory layer has its own plan at
[tests/memory/TEST_PLAN.md](memory/TEST_PLAN.md) and is the reference for what "done"
looks like here.

> **Status: deferred.** The memory layer is being finished first — 338 cases remain there.
> Nothing in this document is being worked on yet. It exists so the survey work isn't
> repeated later, and so the sizing below is available when the decision comes up again.

---

## What is actually there

Measured, not estimated:

| | |
|---|---|
| Application code | 76,331 lines |
| Memory layer (has a plan, 62% written) | 20,212 lines |
| **Everything else (no tests at all)** | **56,119 lines** |
| API routers | 41 |
| API operations | 386 across 320 paths |
| Service packages | 35 |
| Existing test files | 19 — **all of them memory** |

The application starts, all 41 routers register, `/api/health` responds, and the OpenAPI
schema builds. That is currently the only thing verified about 74% of the codebase.

## Honest sizing

The memory plan enumerates 880 cases for 20,212 lines — about one case per 23 lines. At
that density the rest of the application would need roughly **2,400 more cases**, which is
not a plan, it's a wish.

So this plan is **risk-ranked instead of uniform**. Density follows consequence:

| Band | What it covers | Density | Cases |
|---|---|---|---|
| **P0** | Irreversible or trust-losing: profile isolation, credentials, code-writing, shell execution | Memory-level | ~400 |
| **P1** | Daily-use paths: chat, routing, search grounding, live data | Moderate | ~350 |
| **P2** | Breadth: the 20-odd feature services | Contract + smoke | ~300 |
| **P3** | API surface: auth and shape for every endpoint | Parametrised sweep | ~200 |
| | Memory layer remaining | | 338 |
| | **Total remaining** | | **~1,590** |

**Status legend:** `[ ]` not written · `[~]` partially covered · `[x]` covered and passing.

---

# P0 — Things that cannot be undone

These get memory-level scrutiny because a bug here loses data, leaks between profiles, or
runs something on the machine. Everything in this band is a "you only find out afterwards"
failure.

## P0.1 Profile isolation and authentication — `ISO`, `AUT`

`app/services/profile_accounts.py`, `ProfileDatabaseMiddleware`, `app/api/routes/accounts.py`

Every non-guest account has its own directory and SQLite database. This is the guarantee
that matters most in a multi-profile personal assistant, and it is enforced by a middleware
that swaps the database per request — so any request path that misses it reads the wrong
profile's data.

- [ ] **AUT-01** `create_profile` rejects a duplicate username.
- [ ] **AUT-02** Passwords are stored salted and hashed, never recoverable.
- [ ] **AUT-03** `_verify_password` is constant-time (no early return on first mismatch).
- [ ] **AUT-04** `authenticate` fails for a wrong password and for an unknown profile with
      the same error, so the response can't enumerate accounts.
- [ ] **AUT-05** Session tokens are stored only as hashes; the raw token never hits disk.
- [ ] **AUT-06** `profile_for_session` rejects an expired session.
- [ ] **AUT-07** …rejects a revoked session.
- [ ] **AUT-08** …rejects a token that is a valid hash of nothing (empty/whitespace).
- [ ] **AUT-09** `revoke_profile_sessions` ends every session for that profile, not just one.
- [ ] **AUT-10** `delete_profile` requires the correct password.
- [ ] **AUT-11** `delete_profile` removes the profile directory and database.
- [ ] **AUT-12** A guest profile is removed on session end.
- [ ] **AUT-13** …and on application shutdown (`cleanup_guests`).
- [ ] **AUT-14** Avatar upload rejects a non-image and an oversized payload.
- [ ] **ISO-01** `database_url_for` returns a different path per profile.
- [ ] **ISO-02** `owner_id_for_profile` is stable across calls and distinct per profile.
- [ ] **ISO-03** A guest and a permanent profile with the same id get different owner ids.
- [ ] **ISO-04** `validate_profile_owner_pair` rejects a mismatched pair.
- [ ] **ISO-05** `database_identity_for_profile` carries the guest/account prefix, so a
      permanent profile can never be served from a guest database.
- [ ] **ISO-06** `memory_key_material_for_profile` differs per profile — one profile's key
      cannot decrypt another's memories.
- [ ] **ISO-07** `ProfileDatabaseMiddleware` binds the request to the session's database.
- [ ] **ISO-08** …and a request with no session touches no profile database.
- [ ] **ISO-09** Two concurrent requests for different profiles do not cross databases
      (the middleware uses a context manager; this pins it under threads).
- [ ] **ISO-10** Writing data as profile A then reading as profile B returns nothing.
- [ ] **ISO-11** …repeated for notes, tasks, projects, files, and chat.
- [ ] **ISO-12** The profile registry migration assigns owner ids to pre-existing rows.
- [ ] **ISO-13** `_validate_profile_owner_rows` refuses to start on a corrupt registry.

## P0.2 Command sandbox — `CMD`

`app/services/command_sandbox/{policy,runner,redaction,service}.py`

This runs subprocesses. The allowlist in `policy.py` is the only thing between a model's
suggestion and the shell.

- [ ] **CMD-01** Every category in `ALLOWED` accepts its listed prefixes.
- [ ] **CMD-02** A command not in `ALLOWED` is refused.
- [ ] **CMD-03** An unknown category is refused.
- [ ] **CMD-04** A command whose prefix matches but with extra leading args is refused.
- [ ] **CMD-05** Shell metacharacters (`;`, `&&`, `|`, backticks, `$()`) cannot smuggle a
      second command past the prefix check.
- [ ] **CMD-06** A path argument escaping the workspace (`../`, absolute, symlink) is refused.
- [ ] **CMD-07** The runner never uses `shell=True`.
- [ ] **CMD-08** stdin is closed, so a command cannot block waiting for input.
- [ ] **CMD-09** A command exceeding its timeout is killed and reported as a timeout.
- [ ] **CMD-10** Output is truncated at its bound rather than read unboundedly.
- [ ] **CMD-11** `redaction.py` removes secrets from captured output.
- [ ] **CMD-12** …including tokens that appear only in the environment.
- [ ] **CMD-13** A non-zero exit is reported, not raised.
- [ ] **CMD-14** Runs are recorded with their command, exit code, and duration.
- [ ] **CMD-15** A run is owner-scoped and invisible to another profile.

## P0.3 Patch application — `PAT`

`app/services/patch_apply/{parser,validator,safety,applier}.py`

This writes to the user's code.

- [ ] **PAT-01** A malformed diff is rejected by the parser with a named error.
- [ ] **PAT-02** A patch touching a path outside the workspace is refused.
- [ ] **PAT-03** …including via `../` and via an absolute path.
- [ ] **PAT-04** …including via a symlink pointing outside.
- [ ] **PAT-05** A patch whose context does not match the file is refused, not force-applied.
- [ ] **PAT-06** A patch is applied atomically — a failure halfway leaves no partial write.
- [ ] **PAT-07** Applying the same patch twice is detected rather than doubled.
- [ ] **PAT-08** `safety.py` refuses deletion of a file the patch never read.
- [ ] **PAT-09** Binary files are refused.
- [ ] **PAT-10** A patch larger than the configured bound is refused.
- [ ] **PAT-11** Line endings are preserved (CRLF file stays CRLF).
- [ ] **PAT-12** A rejected patch leaves the working tree byte-identical.
- [ ] **PAT-13** The original registered repository is never edited directly — writes land
      in the managed copy, as the README promises.

## P0.4 Git operations — `GIT`

`app/services/git/`

- [ ] **GIT-01** A checkpoint captures the working tree state.
- [ ] **GIT-02** Restoring a checkpoint returns the tree to that state.
- [ ] **GIT-03** Restoring does not discard uncommitted work without saying so.
- [ ] **GIT-04** Operations are confined to the managed copy, never the user's original.
- [ ] **GIT-05** A destructive operation (reset/clean) requires explicit confirmation.
- [ ] **GIT-06** A git failure surfaces as an error, not a silent no-op.
- [ ] **GIT-07** Branch names from user input cannot inject git arguments (`--upload-pack`).
- [ ] **GIT-08** Operations are owner-scoped.

## P0.5 Workspace files — `FIL`

`app/services/files/`, `app/services/workspace_orchestration/`

- [ ] **FIL-01** Reading a path outside the workspace is refused.
- [ ] **FIL-02** Writing outside the workspace is refused.
- [ ] **FIL-03** …including through a symlink created inside the workspace.
- [ ] **FIL-04** …including through `..` segments and URL-encoded variants.
- [ ] **FIL-05** A null byte in a path is refused.
- [ ] **FIL-06** File size limits are enforced on write.
- [ ] **FIL-07** A delete is confined to the workspace.
- [ ] **FIL-08** Listing a directory does not traverse outside it.
- [ ] **FIL-09** Files are owner-scoped between profiles.

## P0.6 Credentials and connectors — `CRD`

`app/services/integration.py`, `app/services/tools/`

The README promises profile-bound AES-GCM credential storage, OAuth PKCE, atomic token
rotation, and explicit approval for external or workspace writes.

- [ ] **CRD-01** A stored credential is encrypted at rest.
- [ ] **CRD-02** …bound to the profile, so another profile's key cannot decrypt it.
- [ ] **CRD-03** A credential is never returned in an API response.
- [ ] **CRD-04** …nor in logs or error messages.
- [ ] **CRD-05** OAuth uses PKCE, and the verifier is not reused between flows.
- [ ] **CRD-06** The `state` parameter is validated on callback.
- [ ] **CRD-07** Token rotation is atomic — an interrupted rotation leaves a usable token.
- [ ] **CRD-08** An expired token triggers refresh, not a silent failure.
- [ ] **CRD-09** A read operation may auto-run only when uniquely matched.
- [ ] **CRD-10** An external write always requires explicit approval.
- [ ] **CRD-11** A workspace write always requires explicit approval.
- [ ] **CRD-12** Connector administration requires an active profile session.
- [ ] **CRD-13** An MCP server URL pointing at localhost/internal ranges is handled per policy.

---

# P1 — The paths used every day

## P1.1 Chat — `CHT`

`app/services/chat.py` (2,400+ lines), `app/api/routes/chat.py` (15 endpoints)

The main surface. The README makes specific durability claims — idempotent submissions,
expiring worker leases, fenced writes, a generation-linked assistant row — all aimed at one
property: **no duplicate transcript entries across retries, refreshes, and restarts.**

- [ ] **CHT-01** `send_message` persists exactly one user row and one assistant row.
- [ ] **CHT-02** The same submission twice (same idempotency key) creates one pair.
- [ ] **CHT-03** A retry after a crashed generation does not duplicate the assistant row.
- [ ] **CHT-04** A worker lease expires and another worker can claim the generation.
- [ ] **CHT-05** …but two workers cannot hold one lease simultaneously.
- [ ] **CHT-06** A fenced write from a stale worker is rejected.
- [ ] **CHT-07** A resumed generation continues rather than restarting.
- [ ] **CHT-08** Streaming produces progressive output and one final persisted row.
- [ ] **CHT-09** A stream that disconnects mid-generation still persists the result.
- [ ] **CHT-10** An edited user message reruns and supersedes rather than appending.
- [ ] **CHT-11** Thinking traces are captured separately from the reply.
- [ ] **CHT-12** Provider, model, token counts, and timing are recorded.
- [ ] **CHT-13** History is bounded — an old conversation does not grow the prompt without limit.
- [ ] **CHT-14** A memory-forgetting turn is detected and handled.
- [ ] **CHT-15** Chat is owner-scoped: another profile cannot read the conversation.
- [ ] **CHT-16** An LLM provider failure surfaces as an error turn, not a 500.
- [ ] **CHT-17** …and does not persist a half-written assistant row.

## P1.2 Routing and intent — `RTE`

`app/services/chat_intent.py`

The README calls this "conservative routing" between conversation, personal declarations,
explicit commands, live data, web research, and connector calls. Mis-routing is the most
visible everyday failure: a question answered from stale memory, or a chat turn that
triggers a web search.

- [ ] **RTE-01** Plain conversation routes to conversation.
- [ ] **RTE-02** A personal declaration routes to memory, not search.
- [ ] **RTE-03** An explicit internal command routes to the command path.
- [ ] **RTE-04** A live-data question (weather/currency/time) routes to the structured path.
- [ ] **RTE-05** A research question routes to web research.
- [ ] **RTE-06** A connector-shaped request routes to the connector path.
- [ ] **RTE-07** Ambiguous input prefers conversation — the conservative default.
- [ ] **RTE-08** A mixed turn (question + declaration) is handled without dropping either.

## P1.3 Search, research, and grounding — `SRC`, `RSH`

`app/services/search/` (4,977 lines), `app/services/research/` (5,893), `app/services/web_search/`

The README is specific: *"Search-result snippets help discovery only; claims and citations
require successfully fetched page content."* That is a testable safety property.

- [ ] **SRC-01** A citation is only produced from fetched page content, never from a snippet.
- [ ] **SRC-02** A claim without supporting fetched content is not asserted.
- [ ] **SRC-03** Citation validation rejects a URL that was never fetched.
- [ ] **SRC-04** Release-date safeguards reject a stale page presented as current.
- [ ] **SRC-05** A failing provider falls back to the next one.
- [ ] **SRC-06** …and all-providers-failed degrades to an honest "I couldn't find out".
- [ ] **SRC-07** Provider attempts and evidence are persisted for audit.
- [ ] **SRC-08** Relevance ranking is deterministic for identical inputs.
- [ ] **SRC-09** A fetched page over the size bound is truncated, not loaded whole.
- [ ] **SRC-10** Fetching an internal/localhost URL is refused (SSRF).
- [ ] **SRC-11** Evidence extraction never fabricates a quote absent from the page.
- [ ] **RSH-01..12** Research mode: session lifecycle, step persistence, resumption,
      cancellation, and owner scoping.

## P1.4 Live data — `LIV`

Open-Meteo weather, Frankfurter currency, local date/time.

- [ ] **LIV-01** Current weather parses a known Open-Meteo payload correctly.
- [ ] **LIV-02** A daily forecast maps days to the right dates in the user's timezone.
- [ ] **LIV-03** Currency conversion parses a known Frankfurter payload.
- [ ] **LIV-04** An unknown currency code is refused, not silently zero.
- [ ] **LIV-05** Local date/time answers without any network call.
- [ ] **LIV-06** …and uses the profile's timezone.
- [ ] **LIV-07** A provider outage degrades to an honest failure, not a fabricated number.
- [ ] **LIV-08** A malformed provider payload is rejected rather than partially parsed.

---

# P2 — Breadth across the feature services

Contract-level coverage: each service round-trips its data, enforces owner scoping, rejects
invalid input, and fails honestly. Roughly 12–20 cases each.

- [ ] **NTS** Notes — `app/services/notes/`
- [ ] **TSK** Tasks — `app/services/tasks/`
- [ ] **PRJ** Projects — `app/services/projects/`
- [ ] **AGT** Agents — `app/services/agents/`, `agent_framework/`, `agentic_core/`
- [ ] **TLS** Tools — `app/services/tools/` (32 endpoints, the largest router)
- [ ] **CDX** Code index — `app/services/code_index/`
- [ ] **SYM** Symbol awareness — `app/services/symbol_awareness/`
- [ ] **LSP** LSP — `app/services/lsp/`
- [ ] **TRN** Test runner — `app/services/test_runner/`
- [ ] **EVL** Evaluation — `app/services/evaluation/`
- [ ] **CNT** Continuity — `app/services/continuity/`
- [ ] **RCV** Recovery — `app/services/recovery/`
- [ ] **BND** Bundles (export/import) — `app/services/bundles/`
- [ ] **REP** Repos — `app/services/repos/`
- [ ] **GHB** GitHub — `app/services/github/`
- [ ] **LLM** LLM registry and provider runtime — `app/services/llm_registry/`, `provider_runtime/`
- [ ] **RUL** Rules — `app/services/rules/`
- [ ] **CAG** Coding agent — `app/services/coding_agent/`

---

# P3 — API surface sweep

386 operations. Rather than hand-writing each, these are parametrised over the OpenAPI
schema, so a new endpoint is covered the day it is added.

- [ ] **API-01** Every non-public endpoint returns 401/403 without a session.
- [ ] **API-02** Every endpoint with a profile-scoped resource 404s for another profile's id.
- [ ] **API-03** Every endpoint rejects a malformed body with 422, not 500.
- [ ] **API-04** No endpoint returns a stack trace or internal path in an error body.
- [ ] **API-05** The OpenAPI schema builds and every route has a response model.
- [ ] **API-06** CORS allows only the configured origins.
- [ ] **API-07** No endpoint leaks a credential, token, or password field.

---

# Cross-cutting

- [ ] **SEC-01..10** A content sweep for secrets across every database table.
- [ ] **PRF-01..05** Tripwires: startup time, a chat turn's query count, no N+1 in list views.
- [ ] **E2E-01..10** Journeys: create profile → chat → memory stored → recalled next session
      → forget → gone; register repo → index → patch → checkpoint → restore.

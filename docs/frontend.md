# Neo Frontend

This document is the frontend reference for Neo’s current user interface. It covers the
application shell, every feature surface, state and API behavior, connector setup, build and
deployment, and a step-by-step manual production test with expected results.

## 1. Technology and structure

The frontend is a React 18 single-page application built by Vite 5. It intentionally avoids a
large client framework: React function components and hooks manage UI state, browser History
provides shareable chat/project locations, and `src/api.js` is the only network boundary.

```text
frontend/
  package.json                 development, test, and build scripts
  vite.config.js               React plugin and /api development proxy
  src/
    main.jsx                   React root
    App.jsx                    profile gate, shell, chat, navigation, dialogs, memory
    api.js                     JSON, upload, download, and stream client
    chatPresentation.js        response-kind/token/time formatting and thinking separation
    connectorForms.js          connector/auth form normalization and validation
    index.css                  complete visual and responsive system
    *.jsx                      focused feature panels
  tests/
    *.test.mjs                 Node-based frontend unit/contract tests
```

`main.jsx` imports the stylesheet and renders `App`. `App` checks the active profile session:
while checking it shows a loading state; without a session it renders `ProfilePicker`; after
create/unlock/guest selection it renders `NeoApp`.

The production build is copied into `app/static` by the Docker image and served by FastAPI.
Development runs at `127.0.0.1:5173` and proxies `/api` to the backend.

## 2. Application shell and navigation

### Top-level shell

The shell consists of:

- a top status bar and local/private branding;
- a profile button that ends the session and returns to the profile picker;
- a collapsible sidebar;
- the current full-page workspace or chat transcript;
- a fixed conversation/agent composer;
- modal feature surfaces;
- one shared confirmation dialog for chat/project deletion.

The sidebar exposes Chat, Memory, Research, Notes, Projects, Tasks, Files, and Repositories,
followed by new-chat/new-project controls, project-grouped chat history, and Settings.

Chats can be created globally or inside a conversational project. The active chat ID is saved
as `neo-active-chat-id` in local storage only as a navigation hint; the transcript remains
server-owned. Invalid stored IDs are discarded.

### Permalinks and browser history

The shell recognizes:

- `/chats/:chatId`;
- `/projects`;
- `/projects/:projectId`.

It also consumes compatibility query actions such as `open_chat`, `request_delete_chat`,
`request_delete_project`, `new_project_chat`, and `select_project`, then clears them.
`history.pushState`/`replaceState` and `popstate` keep the visible panel synchronized without a
router dependency. IDs are treated as opaque server identifiers.

### State ownership

`NeoApp` owns state shared across panels: profile, sidebar, active chat, transcript, selected
LLM, current project, active background generation, modal visibility, initial deep-linked
record IDs, chat/agent mode, and global errors. Each feature component owns its form, loading,
selection, and status state.

The backend is the source of truth. After mutations, components reload the affected collection
or use the returned canonical record. UI status must not imply a write succeeded before the
backend confirms it.

## 3. Chat experience

### Chatbot mode

The composer provides:

- a Chatbot/Agent mode switch;
- an enabled-LLM selector;
- an auto-growing textarea bounded by viewport height;
- Enter to send and Shift+Enter for a newline;
- disabled state while no chat is ready or a response is active.

On send, the UI:

1. clears the composer;
2. adds one optimistic user bubble with a temporary ID;
3. creates a chat if necessary;
4. sends `prompt`, selected `llm_id`, a random `client_request_id`, browser IANA timezone, and
   browser locale to the durable-generation API;
5. stores the returned generation ID;
6. polls every 250 ms (one second after a polling error);
7. renders partial answer, model thinking, status detail, and elapsed time;
8. reloads the canonical transcript on completed or failed state;
9. clears the active-generation state and re-enables the composer.

If submission fails, the optimistic message remains marked “Not sent,” the text returns to the
composer, and the UI explains that the message was preserved. It does not invent a server ID.

### Refresh and restart recovery

Whenever a chat is loaded, the frontend requests
`/chats/{id}/generations/active`. If a queued/running generation exists, the UI restores its
ID, start time, partial response, thinking, and status detail and resumes polling. This is what
prevents refresh from producing a second assistant answer or an indefinitely disabled
composer.

The browser does not decide whether work is stale and never starts a replacement response
while one is active. The backend keeps a fresh worker lease, atomically reclaims only queued
or expired work, fences late workers, and upserts the one assistant row correlated to the
generation. The UI’s responsibility is to retain the returned generation ID, poll it, and
reload the canonical transcript at a terminal state.

Only the pending bubble for the currently visible chat is shown. Switching panels does not
insert a generation into another transcript. Completion reloads the database-backed message
list, removing the optimistic copy.

### Message controls and metadata

User messages support:

- Copy;
- Edit and Save/Cancel;
- rerun after editing, which truncates later transcript state server-side and starts one new
  durable generation.

Assistant messages support:

- Copy;
- Rerun from the immediately preceding user message;
- View/Hide thinking.

Thinking is shown only if the provider returned a trace. Otherwise the panel explicitly says
reasoning is unavailable and suggests a reasoning-capable model; it never generates a fake
chain of thought.

Assistant metadata displays only meaningful values:

- a friendly response-kind label (Memory, Web search, Weather, Currency, Local date & time,
  Connector, or Neo action), or provider/model for model output;
- total tokens when finite;
- duration in milliseconds or seconds when finite.

The old `Tokens n/a` and `Time n/a` placeholders are not rendered.

### Partial and grounded output

The pure helpers in `chatPresentation.js` omit unavailable token/time values, choose a
response-kind or provider/model label, and separate any provider `<think>...</think>` content
from answer text without duplicating or leaking an incomplete thinking block. For normal
chat, visible answer content grows as the backend persists
`partial_response`. For web-grounded chat, backend status text can progress through search and
validation while the final answer remains buffered until citations pass; the UI then receives
the canonical replacement.

### Agent mode

Agent mode does not send a chat message. It offers optional project, existing task, and agent
definition selectors plus:

- dry-run task planning;
- explicit “Create Tasks” or “Create Tasks & Run Agent” after plan review;
- run from an existing task or a free-form objective;
- persisted run/step/tool status;
- output save-to-note;
- linked Recovery controls;
- an Advanced Coding Workbench for repository/patch/test/checkpoint workflows.

Active agent runs disable conflicting submission. Protected tool and coding actions retain
their backend approval state.

## 4. Memory interface

The Memory dialog loads all sections in parallel through `api.memory()`:

- Profile;
- Education;
- Activities;
- Preferences;
- Goals;
- Projects;
- Events;
- General memories.

Every existing card has an Edit/Hide expander, so records are readable at a glance and can be
expanded without navigating away.

Profile, preference, goal, conversational project, event, and general-memory cards can be
edited or deleted. Typed education and activities are displayed with their structured fields:
institution/degree/field/explicit graduation date and
category/start/expiry respectively.

General memories show:

- canonical slot;
- expiry when present;
- count of active source messages;
- type, importance, and editable text.

General memory sorting supports newest, oldest, recently updated, highest importance, and
alphabetical ordering. Empty/loading/error states are explicit. After a project-memory edit or
delete, both the dialog and sidebar refresh.

The Memory dialog is distinct from **Memory Retrieval**:

- Memory is the user-facing personal profile and lifecycle store.
- Memory Retrieval is the scoped workspace index, search, score audit, and pruning surface.

## 5. Feature surface reference

### Primary sidebar screens

| Screen/component | User-visible behavior |
| --- | --- |
| `ProfilePicker` | Lists device profiles, creates a password-protected profile with optional avatar, unlocks an account, or starts a temporary guest. |
| Chat in `App.jsx` | Persistent conversation, background generation, thinking, response metadata, copy/edit/rerun, chat/project history, and Chatbot/Agent modes. |
| Memory in `App.jsx` | Typed profile, education, activities, preferences, goals, conversational projects, events, and durable memory editing/provenance. |
| `Research` | Starts and monitors legacy research jobs, views status/report/events, cancels, clears history, and saves output to a note. |
| `Notes` | Lists/searches/filters notes, opens a note, creates/edits content and tags, pins, archives, deletes, links work records, and opens linked tasks/files. |
| `Projects` | Lists/searches/filters workspace projects, creates/edits metadata/tags/status, pins, archives, deletes, and links tasks/notes/files/repositories. |
| `Tasks` | Lists/searches/filters tasks, creates/edits priority/status/tags, pins, archives, deletes, links notes/files, and inspects task agent runs. |
| `Files` | Uploads, filters, opens, downloads, summarizes, links, and deletes workspace files; shows extracted text and related artifacts/patch applications. |
| `Repos` | Registers a local repository into a managed copy, lists and filters files, opens file records, and launches code-index, symbol, LSP, test, Git, and coding tools. |

### Intelligence and automation

| Screen/component | User-visible behavior |
| --- | --- |
| LLM Providers in `App.jsx` | Manages legacy and registry providers/models/routes, tests a model or route, enables/disables records, and views recent routed usage. Secret values remain environment references. |
| `ProviderRuntime` | Shows route/provider health, request audits, latency/usage, rate limits, fallback/retry status, and stream activity. |
| `AgentSettings` | Creates/edits/disables agent definitions, assigns routes/tools/skills/rules, and resets built-ins. |
| `AgenticRuns` | Creates persisted coding/research/task runs; plans, steps, continues, reflects, stops, shows context, and links search/research evidence. |
| `CodingAgent` | Starts a repository-aware coding run, reviews planning/action states, approves or rejects protected actions, revises a patch, proposes commands, cancels, and opens Recovery. |
| `EvaluationHarness` | Lists suites/runs/cases/reports, runs fixture evaluations, sets a baseline, and compares regressions. |
| `WorkspaceOrchestration` | Creates a goal-oriented workspace and reviews readiness, health scoring, timeline, artifacts, and manual-review checklist. |

### Knowledge, search, and continuity

| Screen/component | User-visible behavior |
| --- | --- |
| Web Search settings in `App.jsx` | Loads provider status, selects/configures a provider, supplies write-only API keys, saves configuration, and runs a test query. |
| `WebSearch` | Previews a reliable search plan, runs it, and inspects source rankings, evidence/citations, conflicts, safe cache, degraded state, and history. |
| `ContextMemory` | Lists summaries, previews deterministic redacted compaction, saves a scoped summary after confirmation, and reads/creates scope events. |
| `MemoryRetrieval` | Searches hybrid memory by scope/type, indexes context summaries, shows scoring and retrieval audit, opens indexed content, and previews safe pruning. |
| `RelatedMemories` | Embeddable list of memories related to a project/task/coding scope. |
| `Bundles` | Exports safe bundles, downloads exports, validates an uploaded archive, imports only after confirmation, and reviews history/details. |
| `Continuity` | Exports portable redacted workspace continuity state and displays reference/validation/report output. |
| `RecoveryPanel` | Displays interrupted-run summary/events and conditionally enables confirmed resume, retry-safe-step, fork, and state-repair operations. |

### Code and workspace safety

| Screen/component | User-visible behavior |
| --- | --- |
| `CodebaseIndex` | Builds/rebuilds a managed repository index and explores symbols, search results, routes, dependencies, and file summaries. |
| `SymbolAwareness` | Builds symbol data and looks up definitions, references, document symbols, related files, and symbol context. |
| `LspPanel` | Lists detected servers, starts/stops a workspace language server, views diagnostics, and sends supported LSP queries. |
| `ArtifactsPanel` | Opens/downloads artifacts and validates/applies patch artifacts through guarded APIs. |
| `PatchApplications` | Filters stored patch applications, shows versions/status/diff, and downloads application artifacts. |
| `CommandSandbox` | Validates a command, stores a proposal, displays the policy decision, approves separately, executes only an approved command after browser confirmation, cancels, and shows bounded output. |
| `TestRunner` | Detects or creates saved argv-based test commands, runs a confirmed command, opens output/history, and creates checkpoints from test context. |
| `GitCheckpoints` | Initializes managed Git state, shows status/diff, creates checkpoints, opens checkpoint detail, restores after confirmation, and shows operations. |
| `FileAttachments` | Reusable uploader/linker for a task/project/note/coding scope. |

### Governance and external systems

| Screen/component | User-visible behavior |
| --- | --- |
| `RulesProfiles` | Creates/edits scoped rules, resolves effective guidance, imports repository rules, and inspects resolution logs. Rules cannot grant permissions. |
| `ToolsSkillsSettings` | Guided connector setup, authentication, health/discovery, enable/disable, tool schema/permission review, skill editing, call audit, and exact-call approval/rejection. |
| `GitHub` | Adds an environment-reference connection, health checks it, imports an issue/PR read-only, creates a local task, and reviews operation audit. It does not silently push, merge, comment, or close. |

## 6. Connector wizard and approval UI

The Connectors, Tools & Skills dialog has three tabs:

- **Connectors**: server setup, health, discovered capabilities, authentication, and tool
  enable/permission state.
- **Skills**: instruction bundles/workflows/checklists with linked tool, agent, and rule IDs.
- **Approvals**: pending/recent calls with input, status, output/error, approve, and reject.

The dialog is available only after profile create/unlock/guest selection. Every underlying
`/tools` request is also session-protected by the backend, including OAuth callbacks and
approval actions; hiding the dialog at the profile gate is not the authorization boundary.

### Guided connector choices

The wizard supports:

1. **OpenAPI URL** — HTTPS document URL and connector name.
2. **OpenAPI file** — JSON/YAML upload up to 2 MiB.
3. **REST endpoint** — base URL, operation/display name, method, path, description, and
   one-per-line parameter locations (`path`, `query`, `header`, `body`). Non-GET/HEAD methods
   show an explicit write-approval warning.
4. **MCP over HTTP** — Streamable HTTP endpoint, followed by real discovery.
5. **Legacy MCP SSE** — event-stream endpoint and same-origin message endpoint negotiation.
6. **Local MCP process** — executable, one argument per line, environment-variable references,
   and an explicit trust checkbox. The browser never constructs a shell command.

HTTP-style connectors offer a separate trusted-loopback checkbox. It is not a general private
network bypass.

After connection, the UI selects the new connector, loads its credential status, and shows how
many tools were discovered. Test Connection records health and tool count; Discover refreshes
definitions; Enable/Disable controls whether chat can select them.

### Authentication UI

Authentication methods are:

- none;
- API key in a named header;
- API key in a named query parameter;
- bearer token;
- OAuth 2.0 Authorization Code with PKCE.

Secret inputs use password fields and are cleared after save. A configured status shows only
label, method, public client ID/header/query name, scopes, expiry, and refresh-token presence.
The UI never receives the encrypted payload or plaintext secret.

OAuth requires client ID, optional client secret, authorization URL, token URL, optional
revocation URL, exact redirect URI, and scopes. “Authorize” opens the returned PKCE URL in a
new tab; after completing the provider flow the user can check status, refresh, or revoke.
The callback itself may be completed through the backend GET callback or the API method
provided for a code/state pair.

### Approvals

Read-only connector calls may complete automatically only when backend selection is unique and
high-confidence and the prompt asks to use the capability. A question explaining or
documenting the connector stays ordinary chat. Weak or tied matches do not execute. Writes
appear as pending approvals. Approve executes that exact persisted input; Reject records a
terminal rejection and performs no external request. The UI must not transform a pending call
into “completed” optimistically.

## 7. API client contract

`src/api.js` exports one `api` object. Components should never build backend URLs directly
unless the method returns a download/event URL by design.

### Request behavior

`request(path, options)`:

- prefixes `VITE_API_BASE_URL` or `/api`;
- sends JSON content type unless the body is `FormData`;
- parses JSON by content type, maps 204 to `null`, and preserves text responses;
- converts FastAPI string/list/object error detail into a readable `Error`;
- gives an actionable local-backend message when fetch cannot connect.

Browser requests are same-origin in production, so the HTTP-only profile cookie is included by
normal fetch defaults. When hosting frontend and backend on separate origins, configure CORS,
credentials, and cookies deliberately; setting an API URL alone does not establish a secure
cross-site session.

### Streaming behavior

`streamRequest` supports the legacy newline-delimited JSON chat endpoint. It buffers incomplete
lines across `ReadableStream` chunks, parses each completed event, forwards normal events, and
throws on an error event. The current main chat uses the more restart-safe generation +
polling contract, but the stream method remains supported.

### Client operation families

The API object covers:

- account create/unlock/guest/current/end;
- chat/sidebar/message/generation/edit/rerun;
- personal memory and lifecycle;
- notes, projects, tasks, files, artifacts, repositories;
- LLM legacy configuration, registry, and provider runtime;
- typed search configuration, direct web utility, reliable web runs, and research;
- context memory and retrieval;
- task agents, agent definitions/delegations, agentic core, coding agent, and recovery;
- code index, symbols, LSP, patch, command, test, and Git;
- tools/connectors/credentials/OAuth/skills/calls;
- rules, GitHub, evaluation, workspace orchestration, bundles, continuity, and integration
  status.

Optional query parameters are built with `URLSearchParams`; path IDs are encoded where needed.
Uploads use `FormData`; downloads use backend-owned URLs.

## 8. Styling, responsiveness, and accessibility

`index.css` contains the full design system: app shell, sidebar, typography, buttons, forms,
cards, dialogs, tables, chat, responsive breakpoints, and connector-specific states.

Implementation rules:

- Keep semantic `button`, `form`, `label`, `input`, `select`, `textarea`, `nav`, `section`,
  and `article` elements.
- Every icon-only control needs `aria-label` and normally `title`.
- Dialog containers use `role="dialog"` and `aria-modal="true"`; tab sets use `role="tablist"`
  and `aria-selected`.
- Status and errors use `role="status"`/`alert` or stable live regions where appropriate.
- Do not use color as the only status signal; include text such as Pending, Ready, Failed, or
  Approval required.
- Preserve keyboard submission and visible focus. Modal close, destructive confirmation, and
  approval actions must be keyboard reachable.
- Ensure long JSON, paths, output, and message content wrap or scroll without widening the
  viewport.
- Validate desktop and narrow/mobile widths, collapsed sidebar, short-height viewports, and
  the auto-growing composer.

The UI has useful ARIA foundations but no dedicated focus trap. Any change to modal behavior
should include keyboard focus-order and focus-return testing.

## 9. Frontend security model

- Never place credentials in `VITE_*`; Vite embeds those values in public JavaScript.
- Secret connector fields are write-only and should never be copied into component logs,
  notices, local storage, URLs, or rendered JSON.
- External links use `target="_blank"` with `rel="noreferrer"` where shown.
- Render connector/search/model text as React text, not `dangerouslySetInnerHTML`.
- Confirmation in the browser improves clarity but is not an authorization boundary; the
  backend enforces every approval, path, URL, and schema rule.
- Treat provider thinking, tool output, repository content, search pages, file text, and error
  detail as untrusted display data.
- The frontend stores only the active chat navigation hint locally. Profile sessions and
  durable state remain on the backend.

## 10. Development, tests, and build

Install exact dependencies:

```bash
cd frontend
npm ci
```

Run development:

```bash
npm run dev
```

The default page is `http://127.0.0.1:5173`. Override only the proxy target when needed:

```bash
VITE_API_PROXY_TARGET=http://127.0.0.1:9000 npm run dev
```

`VITE_API_BASE_URL` changes the browser’s API prefix and is normally unset:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000/api npm run build
```

Run tests and production compilation:

```bash
npm test
npm run build
```

`npm test` runs Node test files under `frontend/tests`. `npm run build` must complete with no
unresolved import or syntax error and creates `frontend/dist`.

The current unit contracts cover:

- `chatPresentation.test.mjs`: meaningful direct/model metadata, omission of `n/a`, clean
  thinking extraction, and incomplete thinking-block isolation;
- `connectorForms.test.mjs`: REST read/write categorization, stdio trust and argv/env
  boundaries, legacy SSE identity, secret-field scoping, OAuth scope normalization, and
  rejection of ambiguous parameter/environment syntax.

The Dockerfile always runs `npm ci` and `npm run build`, removes the runtime static directory,
and copies the new `dist` tree. This guarantees that the image cannot retain a previous UI
bundle merely because an old file was present in the source/runtime layer.

## 11. Manual end-to-end production test

Use a newly reset test profile in the rebuilt Docker container. Keep browser DevTools Network
and Console open. For each step, record actual result, screenshot/error, and pass/fail.

### A. Deployment and old-UI removal

1. Confirm only one container publishes host port 8000.
   - **Expect:** the `neo` container uses the newly built image; no other container/process
     binds 8000.
2. Open `http://127.0.0.1:8000`.
   - **Expect:** the current “LOCAL INTELLIGENCE SYSTEM” UI, not the obsolete green sidebar UI.
3. Reload normally, hard reload, close/reopen the tab, and directly open `/chats/1`.
   - **Expect:** the current UI every time; deep links load the SPA.
4. Inspect `/service-worker.js` and `/sw.js`, then Application → Service Workers.
   - **Expect:** the retiring worker response uses no-cache and any legacy worker becomes
     unregistered; no old cached shell controls the page.
5. Inspect the HTML response.
   - **Expect:** no-store/no-cache headers and asset hashes matching the rebuilt image.

### B. Profiles and navigation

1. Create a profile with username, valid password, and optional image.
   - **Expect:** immediate signed-in shell; profile appears after logout.
2. Logout and enter a wrong password.
   - **Expect:** clear 401-derived error and no session.
3. Unlock correctly.
   - **Expect:** profile-scoped chats/data only.
4. Start a guest, add data, and end the session.
   - **Expect:** guest data is gone; account data is untouched.
5. Create chats globally and within a conversational project. Use browser back/forward and
   copy a `/chats/:id` and `/projects/:id` URL into a fresh tab.
   - **Expect:** correct record opens, history order is sensible, and unknown IDs recover
     without corrupting local storage.
6. Collapse/expand the sidebar and test at narrow width.
   - **Expect:** content remains usable with no horizontal page overflow.

### C. Chat, thinking, and metadata

1. Send `hi`.
   - **Expect:** one user bubble, progressive assistant output, then one persisted assistant
     bubble; the composer re-enables.
2. Inspect assistant metadata.
   - **Expect:** provider/model and token/duration when returned, or a meaningful direct
     response-kind label; never `Tokens n/a` or `Time n/a`.
3. Click View thinking.
   - **Expect:** provider reasoning if supplied; otherwise the explicit unavailable
     explanation, with no invented trace.
4. Copy both message types.
   - **Expect:** exact visible content in clipboard.
5. Rerun the answer.
   - **Expect:** one additional user/assistant exchange; no doubled text inside one answer.
6. Edit an earlier user message and Save.
   - **Expect:** later messages are removed, one replacement generation starts, and the
     transcript reloads consistently.
7. Make the backend/provider fail before submission.
   - **Expect:** optimistic message marked not sent, text restored to composer, actionable
     error, and composer usable.

### D. Refresh during generation

1. Send a prompt long enough to run several seconds.
2. While content is appearing, refresh twice.
   - **Expect:** one active generation resumes from server partial output, not a fresh
     generation.
3. Wait for completion.
   - **Expect:** exactly one user and one assistant database-backed message, no missing or
     duplicate segment, final metadata, and enabled composer.
4. Restart the container mid-generation, reopen/unlock the profile, and open the chat.
   - **Expect:** the server either recovers the generation or reports a terminal failure;
     polling does not continue forever.
5. Refresh after completion.
   - **Expect:** identical transcript and metadata.

### E. Intent-routing regression

Send each as a normal chat:

1. `Explain recovery after an application restart.`
2. `How should coding-agent recovery work?`
3. `Describe how Neo finds recoverable runs.`
4. `What is the purpose of the Recovery page?`
5. `Compare backup, recovery, and continuity.`
6. `Write documentation about recovering interrupted agent runs.`

- **Expect for all:** selected LLM response, progressive output, no Recovery lookup/run, and
  normal model metadata.

Then send:

1. `Find my recoverable runs.`
2. `Show interrupted agent runs.`
3. `Open recovery and check for incomplete runs.`

- **Expect:** intentional Recovery response with a Neo action label. Ambiguous phrases should
  stay chat or ask for clarification.

Repeat explanatory/ambiguous prompts containing research, files, projects, notes, tasks,
tools, GitHub, and coding-agent names.

- **Expect:** no internal feature executes solely because its name appeared.

### F. Typed memory and recall

1. Send an explicit multi-fact personal statement covering education, occupation, chess and
   samurai interests, Bobby Fischer favorite, New Delhi/India, two goals, ordered
   Python/C++/C priority, and playing Ghost of Yotei.
   - **Expect:** saved acknowledgement only after persistence; no web-search status.
2. Open Memory and inspect every tab.
   - **Expect:** every fact in its correct typed section, one graduation event, no invented
     graduation date, two goals, ordered language preference, and activity with 30-day expiry.
3. Expand each existing record.
   - **Expect:** Edit becomes Hide and the complete fields are visible.
4. Ask `who am I?`, location, education, interests, goals, favorite, languages, and current
   activity.
   - **Expect:** correct direct-memory answers and Memory metadata. Unstated name/age must not
     appear.
5. State the same fact in a second message, then edit/delete one source.
   - **Expect:** no duplicate canonical card and active source count decreases while the fact
     remains. Replacing one source with the identical fact reuses/reactivates the same record.
6. Delete the final supporting source.
   - **Expect:** the fact becomes a durable deleted tombstone and its typed card disappears.
     Repeating the same sentence does not silently recreate it; an explicit restore is
     required.
7. Edit a profile/preference/goal/event/general memory from the dialog.
   - **Expect:** saved value survives closing/reopening and refresh.
8. Restart the container.
   - **Expect:** all active memory and source counts persist without duplication.

### G. Date, currency, weather, and web

1. Ask today’s date from a browser in `Asia/Kolkata`.
   - **Expect:** Local date & time label, correct date/timezone, no search network request.
2. Ask to convert 1 USD to INR, then `what about 10 USD`.
   - **Expect:** Currency label, retained USD→INR context, amount/rate/reference date/provider,
     and correct decimal multiplication.
3. Ask New Delhi weather, then `new delhi today`.
   - **Expect:** Weather label, Open-Meteo place/country/current observation, retained weather
     context.
4. Ask `What is the weather in New Delhi tomorrow?`
   - **Expect:** a dated Open-Meteo daily forecast with low/high, condition, and maximum
     precipitation probability; it must not repeat current conditions.
5. State `I recently graduated from BITS Pilani`.
   - **Expect:** memory acknowledgement and no web call despite “recently.”
6. Ask a current product/news/release question.
   - **Expect:** visible search/read/validation status, then a complete cited answer; citation
     links correspond to claims. Inspect message search trace: provider status/timing,
     rejected results, fetched-page evidence, freshness, and citation decisions are present;
     an unfetched search-result snippet never becomes evidence.
7. Ask for an announced item with no verified release date.
   - **Expect:** no-date-announced answer; an article publication date is never substituted.
8. In Web Search settings, test each configured provider and an invalid key/URL.
   - **Expect:** clear success/degraded/error status with no secret echoed.
9. Open Reliable Web Search and run a query.
   - **Expect:** plan, ranked sources, evidence/citations, conflicts if any, history, and safe
     cache count.

### H. Connectors, credentials, OAuth, and approval

1. Open Settings → Connectors, Tools & Skills → Add connector.
   - **Expect:** all six connector choices, readable descriptions, keyboard-selectable fields.
2. Import a valid OpenAPI file and URL; try an oversized/invalid/private document.
   - **Expect:** valid operations discovered; invalid inputs show bounded errors and create no
     usable connector.
3. Add GET and POST manual REST endpoints.
   - **Expect:** GET categorized read; POST shows write warning and requires approval.
4. Connect MCP HTTP/SSE and a trusted stdio test server.
   - **Expect:** test health/tool count and real discovered definitions; untrusted stdio is
     rejected.
5. Configure header key, query key, bearer, and OAuth in turn.
   - **Expect:** password fields clear after save; status reveals no secret.
6. Complete OAuth in the opened tab, return, check status, refresh, and revoke.
   - **Expect:** expiry/refresh status updates; revocation removes authorization. A stale
     concurrent refresh is rejected instead of overwriting a newly rotated token.
7. Disable/enable a connector and individual tool.
   - **Expect:** disabled capability is not selected in chat.
8. Trigger a uniquely named read in chat.
   - **Expect:** Connector label, result and provenance, one completed call.
9. Ask an explanatory question containing that connector/tool name.
   - **Expect:** normal assistant response and zero connector calls.
10. Trigger a write.
   - **Expect:** pending approval; no remote change. Approve from Approvals and verify exactly
     one execution. Reject another and verify none.
11. Resize the dialog and navigate entirely by keyboard.
    - **Expect:** fields, tabs, disclosures, status, and action buttons remain reachable.

### I. Workspace records

1. Create, edit, filter, pin, archive, link, and delete a Note, Project, and Task.
   - **Expect:** every mutation refreshes the correct list; link navigation opens the target;
     delete confirmation prevents accidental removal.
2. Upload a permitted file, open/download/summarize/link it, then delete it.
   - **Expect:** metadata/extracted text are bounded, download matches source, links update.
3. Register a repository and browse its managed files.
   - **Expect:** repository appears once; original source remains unchanged.
4. Build code and symbol indexes and inspect routes/dependencies/definition/references.
   - **Expect:** successful/stale/error states are explicit.
5. Start/stop LSP and inspect diagnostics.
   - **Expect:** missing server is shown as unavailable, not success.

### J. Guarded coding and operations

1. Validate and propose a safe command.
   - **Expect:** policy details and pending approval; no execution.
2. Approve, then execute after browser confirmation.
   - **Expect:** bounded/redacted stdout/stderr and audit status.
3. Propose a blocked command.
   - **Expect:** blocked audit record and no approval/execution path.
4. Detect/save/run tests; create a Git checkpoint; inspect diff; restore after confirmation.
   - **Expect:** explicit terminal state and managed-copy changes only.
5. Propose/validate/apply a patch.
   - **Expect:** validation first, explicit apply confirmation, application history/download.
6. Start task agent, agentic, and coding-agent runs; exercise approval, cancellation, output
   save, and Recovery.
   - **Expect:** accurate persisted states and no automatic approval.

### K. Remaining settings panels

1. Test LLM Provider create/edit/enable/test/route and Provider Runtime views.
2. Create/resolve a Rules profile and inspect logs.
3. Run an Evaluation suite and set a baseline.
4. Create a Workspace Orchestration record and inspect readiness/health/timeline.
5. Export/validate/import a Bundle and export Continuity state.
6. Configure GitHub by environment reference, health-check, import an issue/PR, and create a
   local task.

- **Expect:** loading, empty, success, error, approval, and persisted-history states are
  distinguishable; secret values never appear in page text, Console, Network responses, or
  local storage.

### L. Accessibility and responsive release gate

1. Complete profile, chat, memory, connector, approval, and delete workflows using keyboard
   only.
2. Test at 320, 768, 1024, and wide desktop widths plus a short-height window.
3. Increase browser text size to 200%.
4. Inspect labels/roles/names with browser accessibility tools.
5. Test high-content cases: long chat, long path, long JSON, many sidebar chats, many sources.

- **Expect:** visible focus, logical order, no inaccessible icon button, no clipped critical
  action, no horizontal page overflow, and scrollable dialogs/content.

## 12. Troubleshooting

| Symptom | Frontend checks |
| --- | --- |
| Backend unreachable | Verify `/api/health/live`, Vite proxy target, `VITE_API_BASE_URL`, container port mapping, and browser Network error. |
| Old interface returns after refresh | Verify sole port-8000 owner and image, unregister legacy worker, clear only this origin’s old cache once, and confirm server no-cache headers/current asset hashes. |
| Composer stays disabled | Inspect active generation API/status/error, current chat ID, polling Network calls, and Console. Terminal completed/failed should clear `sending`. |
| Duplicate messages | Check whether multiple generation POSTs used different client request IDs, whether the optimistic item was replaced by the canonical transcript, and whether two containers served the same volume. |
| “View thinking” has no trace | The provider/model did not supply thinking. The explicit unavailable panel is expected. |
| Metadata absent | Inspect persisted assistant/generation fields and provider completion event. Direct response types should still display their friendly label. |
| Search answer appears only at the end | Expected for web grounding: the backend buffers until citation validation. Status should continue to update. |
| Connector credential looks empty | Read APIs intentionally hide secrets. `configured: true` and public status fields are the success signal. |
| OAuth opened but status unchanged | Complete the exact registered callback, return to Neo, use Check authorization, and inspect state expiry/redirect/session mismatch errors. |
| Mobile dialog overflows | Capture viewport/component, long content, and focus path; fix shared CSS rather than adding per-record inline widths. |

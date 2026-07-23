# Neo Frontend

## Purpose and scope

The Neo frontend is a browser-based control surface for the local workbench. It presents conversational memory, planning, research, coding, validation, recovery, and administration workflows while delegating state changes and policy enforcement to the FastAPI backend. The frontend is intentionally thin: it renders persisted backend state, gathers user input, displays progress and errors, and asks for confirmation where a workflow requires approval.

This guide covers the web application in `frontend/`. The platform overview is in the root [README](../README.md); backend contracts and service behavior are in [backend.md](backend.md).

## Technology and commands

| Concern | Implementation |
| --- | --- |
| UI runtime | React 18 |
| Bundler and dev server | Vite 5 |
| Styling | Tailwind/PostCSS plus `src/index.css` |
| API transport | Native `fetch` through `src/api.js` |
| Package metadata | `frontend/package.json` |

Install dependencies and start the development server:

```bash
cd frontend
npm install
npm run dev
```

Vite listens on `127.0.0.1:5173`. Its development configuration proxies `/api` calls to `http://127.0.0.1:8000`, so start the backend before exercising API-backed functionality. Create an optimized static build with:

```bash
npm run build
npm run preview
```

The production Docker build runs `npm ci` and `npm run build`, then copies `dist/` to the backend’s static directory. This means the deployed application is served by FastAPI rather than by the Vite development server.

## Application structure

```text
frontend/
  index.html                 Vite document entry point
  vite.config.js             React plugin and development proxy
  tailwind.config.js         Tailwind content configuration
  src/
    main.jsx                 React bootstrap
    App.jsx                  Shell, navigation, chat, dialogs, and shared UI
    api.js                   Central API client and stream reader
    index.css                Global and component styles
    *.jsx                    Feature dialogs and workflow panels
```

`main.jsx` mounts the root `App` component. `App.jsx` owns the application shell: profile startup, sidebar data, chat selection, URL/permalink handling, settings navigation, modal visibility, shared error handling, and the principal chat and memory UI. Feature-specific surfaces are isolated in sibling components and are opened from the settings/dialog flow rather than through a separate client-side router.

Permalinks support chat and project locations (`/chats/:id`, `/projects/:id`, and `/projects`). Navigation is synchronized through the browser History API, so internal links can be shared and browser back/forward controls work without a routing library.

## Feature components

The components below correspond to distinct backend capabilities. They are dialogs or panels managed by `App.jsx`, and each should keep workflow-specific state local unless the shell must coordinate it.

| Area | Component | Primary purpose |
| --- | --- | --- |
| Core work | `Projects`, `Tasks`, `Notes`, `Files` | Organize work, attachments, and artifacts. |
| Research and memory | `Research`, `WebSearch`, `ContextMemory`, `MemoryRetrieval`, `RelatedMemories` | Plan/search research, manage context, and inspect retrieved memory. |
| Repositories and delivery | `Repos`, `CodebaseIndex`, `SymbolAwareness`, `LspPanel`, `CodingAgent`, `PatchApplications`, `CommandSandbox`, `TestRunner`, `GitCheckpoints` | Inspect managed repositories and run guarded coding workflows. |
| Agents and controls | `AgenticRuns`, `AgentSettings`, `RulesProfiles`, `ToolsSkillsSettings` | Configure agents, rules, tools/skills, and agentic execution. |
| Platform administration | `ProviderRuntime`, `EvaluationHarness`, `WorkspaceOrchestration`, `Continuity`, `Bundles`, `RecoveryPanel`, `GitHub`, `ProfilePicker` | Manage providers, evaluation, continuity, recovery, integrations, and profiles. |

When adding a capability, create a focused component when it has substantial state or a distinct workflow. Import it in `App.jsx`, add a clear navigation entry, and close the settings dialog before opening its panel. Avoid duplicating shared modal primitives, formatting helpers, or request error handling.

## Complete UI feature reference

Neo does not use a conventional page router for most product capabilities. After a profile is selected, `NeoApp` renders the main conversation workspace and opens feature surfaces as dialogs. The settings control center organizes those surfaces into Intelligence, Capabilities, Knowledge, and Workspace. This section maps every first-party JSX module to the user-facing behavior it owns.

### Application shell and account flow

| Component | User-facing behavior | Integration responsibility |
| --- | --- | --- |
| `main.jsx` | Starts the web application. | Creates the React root and imports global CSS before rendering `App`. |
| `App.jsx` | Hosts the profile gate, persistent chat workspace, sidebar, chat composer, settings control center, shared dialogs, confirmations, and history/permalink behavior. | Calls account, sidebar, chat, memory, LLM, and agent APIs. It owns cross-panel state such as selected chat/project, dialog visibility, profile session, and refresh callbacks. |
| `ProfilePicker.jsx` | Lists/unlocks local profiles, creates a profile, and starts a temporary guest session. | Calls the account-profile session endpoints. The picker is shown whenever the current profile request returns no active profile. |
| `FileAttachments.jsx` | Lets a user attach uploaded files to conversational or work context where the shell embeds it. | Uses the shared file metadata and upload/download API client methods. |
| `RelatedMemories.jsx` | Presents memory context associated with a conversation or feature workflow. | Consumes retrieval results; it is a display aid and does not replace the durable-memory management dialog. |

The shell first requests `currentAccountProfile()`. While that request is pending it displays a loading state; without a session it renders `ProfilePicker`; otherwise it renders the full application. Switching profiles ends the server session, clears the locally remembered active chat ID, and returns to the picker. This division is important: a panel should not try to emulate profile switching by merely clearing its own state.

The sidebar uses backend-supplied project/chat data. It supports creating chats and projects, opening or deleting chats, and deleting projects. Deletion is protected by `ConfirmDeleteDialog`. The browser pathname is kept in sync for `/chats/:id`, `/projects`, and `/projects/:id`; query-string actions are cleared after use. New functionality that needs a shareable URL should follow this History API pattern rather than introduce a second routing mechanism.

### Intelligence and agent controls

| Component | What a user can do | Backend capability represented |
| --- | --- | --- |
| `AgenticRuns.jsx` | Create, inspect, plan, step, continue, reflect on, and stop persisted agentic runs. | `/agentic/runs` and run step/context actions. |
| `AgentSettings.jsx` | View, create, update, disable, and reset agent definitions; inspect delegation configuration. | `/agents/definitions` and `/agents/delegations`. |
| `CodingAgent.jsx` | Start coding runs for a repository/task, follow planning/action state, approve or reject requested actions, revise a patch, propose commands, and cancel. | `/coding-agent` plus downstream repo/patch/command workflows. |
| `ProviderRuntime.jsx` | Inspect provider runtime status, health checks, recorded requests, rate limits, usage, and streaming activity. | `/providers/runtime`. |
| `EvaluationHarness.jsx` | Review evaluation suites/runs/cases/reports, run suites, set baselines, and compare quality/safety regressions. | `/evals`. |
| `WorkspaceOrchestration.jsx` | Build workspaces with delivery plans, nodes, edges, timelines, artifacts, links, readiness, health, memory indexing, and reports. | `/workspaces`. |
| `Continuity.jsx` | Export portable, redacted continuity state; validate references; dry-run and perform imports; review bundle manifest/validation/report details. | `/continuity`. |

`App.jsx` also contains the LLM-provider settings dialog, which manages the legacy LLM configuration and registry-facing model/provider/route operations. It deliberately stays in the shell because the current chat experience needs the resulting configuration refresh. A provider setting is not a secret store: keys are resolved by the backend through named environment-variable references.

### Connected capabilities and guarded execution

| Component | What a user can do | Backend capability represented |
| --- | --- | --- |
| `ToolsSkillsSettings.jsx` | Configure tool servers, discover their tool definitions, maintain tool/skill records, see call history, and approve or reject tool calls. | `/tools`. |
| `WebSearch.jsx` | Plan/run reliable search, inspect sources/evidence/conflicts, revisit a run, refresh it, and view cached source state. | `/web-search`. |
| `LspPanel.jsx` | Inspect available language servers, start or stop workspace services, see diagnostics, and send supported LSP queries. | `/lsp`. |
| `CommandSandbox.jsx` | Validate a command, save a proposal, examine policy/audit status, approve it, execute it, cancel it, and review output/history. | `/command-sandbox`. |
| `Repos.jsx` | Register, browse, inspect, and remove managed repository copies. | `/repos`. |
| `CodebaseIndex.jsx` | Build/rebuild an index and explore code symbols, search results, routes, dependencies, and file summaries. | `/code-index`. |
| `SymbolAwareness.jsx` | Build symbol awareness and inspect definitions, references, document symbols, related files, and symbol context. | `/symbols`. |
| `PatchApplications.jsx` | Review stored patch applications/downloads, validate a patch against a managed copy, and submit a confirmed apply operation. | `/patches`. |
| `TestRunner.jsx` | Create/detect/update/delete saved test commands, run an approved command, and inspect test-run history/output. | `/test-runner`. |
| `GitCheckpoints.jsx` | Initialize Git state for a managed copy, view status/diff, create/review checkpoints, restore them, and inspect operations. | `/git`. |

These panels expose deliberate rather than silent automation. An action that modifies a managed workspace or calls an external capability has visible proposal/approval/status state. UI code must preserve the distinction between **proposed**, **waiting for approval**, **approved**, **running**, **completed**, **failed**, and **cancelled**; it must never optimistically claim completion before the backend reports it.

### Knowledge, planning, and portability

| Component | What a user can do | Backend capability represented |
| --- | --- | --- |
| `Projects.jsx` | Create/find/filter projects, edit metadata, pin/archive/delete a project, link tasks and notes, and follow project permalinks. | `/projects`. |
| `Tasks.jsx` | Create/filter/edit tasks, set status, pin/archive/delete, attach notes, and inspect task-specific agent runs. | `/tasks`. |
| `Notes.jsx` | Create/list/search/edit notes, manage tags and links, pin/archive/delete notes, and open attached work context. | `/notes`. |
| `Files.jsx` | Upload/list/filter/download/delete workspace files, link them to work records, request summaries, and manage artifacts. | `/files` and `/artifacts`. |
| `ArtifactsPanel.jsx` | Shows generated artifacts in the context where they were produced and makes downloads available. | `/artifacts`. |
| `Research.jsx` | Plan and run research, review evidence/claims/conflicts/reports, continue or refresh a run, validate citations, and access legacy research jobs where presented. | `/research`. |
| `ContextMemory.jsx` | Inspect scoped summaries, preview compaction, compact long-running context, and review/create scope events. | `/context-memory`. |
| `MemoryRetrieval.jsx` | Index/retrieve scoped memory, maintain individual memory items, inspect retrieval audit, and preview/apply pruning. | `/memory` retrieval endpoints. |
| `Bundles.jsx` | Create/export safe bundles, inspect export/import history, validate uploaded bundle files, and import an archive. | `/bundles`. |
| `RecoveryPanel.jsx` | List interrupted runs/events and perform confirmed resume, retry, fork, or repair-state actions. | `/recovery`. |
| `GitHub.jsx` | Create/manage GitHub connections, health-check them, import issues or pull requests, create tasks, inspect operations, and request a PR draft workflow. | `/github`. |
| `RulesProfiles.jsx` | Define scoped rules, resolve the effective rule set for work context, import repository rules, and inspect resolution logs. | `/rules`. |

The shell’s built-in **Memory** dialog covers profile facts, preferences, goals, projects, events, and durable memories. It uses tab-local views, type filtering, sort selection, lifecycle actions, and server refreshes. It is separate from `MemoryRetrieval.jsx`: the former is the user’s durable personal-memory domain; the latter is a retrieval/indexing subsystem for scoped workspace context.

## User workflow model

Most work begins with one of three paths:

1. **Conversation-first:** choose a profile, create/open a chat, attach context, send a message, and optionally save a generated agent result, note, memory, or project linkage.
2. **Workspace-first:** create a project/task/note, add files or a managed repository, then use research, indexing, agents, tests, patches, or checkpoints from that persisted work context.
3. **Operations-first:** open Settings to configure providers, rules, tools, or integrations; inspect health; then launch a controlled workflow that has its required provider and policy state available.

The frontend does not assume every optional dependency exists. A missing provider, disabled search, unavailable LSP server, stale index, pending tool approval, or recovery-required run is useful state and should be rendered as such. Users should always be able to identify what is unavailable, why it is unavailable when the server provides a reason, and what action can resolve it.

## API client contract

`src/api.js` is the single frontend transport boundary. `API_BASE` uses `VITE_API_BASE_URL` when provided and otherwise defaults to `/api`. Every method accepts or returns JSON unless it explicitly uploads a `FormData` payload. The internal `request` helper:

1. Adds `Content-Type: application/json` for JSON requests, but leaves multipart requests untouched.
2. Parses successful JSON responses and handles `204 No Content` as `null`.
3. Converts backend error details into `Error` objects usable by feature panels.
4. Gives a direct startup hint when the backend cannot be reached.

Chat streaming uses `streamRequest`. It posts JSON, reads the response body incrementally, splits newline-delimited JSON events, sends normal events to the supplied callback, and throws when an event has `type: "error"`. Components using it should update visible output incrementally and always restore their submitting state on both success and failure.

Keep endpoint knowledge in `api.js`, not scattered among components. A new endpoint should receive a named, narrowly scoped API method; that keeps request shape, encoding, and error behavior consistent. Query parameters should be built with `URLSearchParams`, especially for optional IDs or user-provided text.

### API-client method inventory

The exported `api` object is intentionally a typed-by-convention interface over the REST API. The frontend uses it in feature components rather than exposing raw `fetch` calls. Its methods cover the following groups:

| Client group | Methods/operations represented |
| --- | --- |
| Profile and chat | Profile list/current/create/unlock/guest/end-session; sidebar; chats; message update; standard and streamed send; chat agent planning/runs/step approval/save. |
| Memory | Core memories/profile/preferences/goals/events; lifecycle archive/supersede/restore/maintenance; memory retrieval items/index/query/prune; context summaries/events/compaction. |
| Workspace records | Projects, tasks, notes, tags, note links, project/task associations, files, attachments, artifacts, and downloads. |
| Research/search | Search-provider configuration/test, direct web search/fetch/answer, reliable web-search plans/runs/cache, and research-mode plan/run/report/evidence/claim/conflict/citation operations. |
| Providers | Legacy LLM configuration, LLM registry providers/models/routes/health/usage, and provider-runtime health/request/stream/rate-limit/usage operations. |
| Agents | Agent definitions/delegations/task-agent runs, agentic-core runs, and coding-agent runs/actions/patch revision/cancellation. |
| Code delivery | Repository registration/files, code index, symbols, LSP, patch validation/application, command sandbox, test commands/runs, and Git checkpoints. |
| Governance and operations | Rules, tool servers/definitions/skills/calls, evaluations, workspace orchestration, bundles, continuity, recovery, GitHub, and integration health. |

This inventory is a frontend contract, not a replacement for the backend API reference. A component must call the method whose name expresses its user operation—such as `approveCommand`, `validatePatchApply`, or `setEvalBaseline`—rather than manually constructing the equivalent URL. This makes confirmations, body shapes, and parameter serialization discoverable in one file.

### Request, upload, download, and stream behavior

`request(path, options)` applies a JSON content type unless the body is `FormData`, performs the fetch, parses JSON where advertised by the response, maps `204` to `null`, and converts failed response details into `Error` messages. Upload methods create `FormData` and therefore do not set a JSON content type; bundle validation/import and file upload are the relevant examples. Download helpers return download endpoints or resource metadata for the calling component to use in browser navigation.

`streamRequest(path, payload, onEvent)` is used for incremental chat output. It posts a JSON payload and expects each completed line in the response body to be a JSON event. It buffers partial lines across reads, ignores blank lines, calls `onEvent` for normal events, and throws on a stream event with `type: "error"`. A streaming component must:

1. Initialize a local in-progress message before starting the request.
2. Merge every received event without assuming fixed chunk boundaries.
3. Preserve existing completed transcript data.
4. Clear busy/abort UI state in a `finally` path.
5. Refresh canonical thread data when the stream completes or fails if the backend may have persisted a partial record.

All request failures should be caught at the nearest feature boundary and rendered in that panel. The generic unreachable-backend message is intentionally actionable: it tells a development user to start FastAPI at `127.0.0.1:8000`.

## UI and state conventions

- Use React function components and hooks. The shell uses `useState`, `useEffect`, `useCallback`, `useRef`, and `useLayoutEffect` for user interaction, loading, focus, and scroll behavior.
- Treat backend records as the source of truth. After a mutating operation, refresh the relevant collection or replace it with the returned record instead of assuming local state succeeded.
- Preserve explicit status values such as `queued`, `planning`, `running`, and `waiting_approval`; render them as readable labels, not inferred progress.
- Show server-provided errors verbatim when safe and give users a practical recovery action. Do not hide unavailable-provider or approval-required states.
- Keep identifiers opaque. Encode IDs in paths and query parameters; do not manufacture them client-side.
- Use the shared button and modal patterns in `App.jsx` and the existing CSS classes in `index.css` to maintain keyboard, focus, and visual consistency.
- Never place provider credentials or secrets in frontend configuration. The browser only supplies non-secret settings and talks to the local API.

## Environment configuration

`VITE_API_BASE_URL` is the frontend-specific setting. It is normally left unset because the browser uses `/api`. Set it only when the frontend is intentionally hosted separately from the backend, for example:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000/api npm run dev
```

Vite exposes `VITE_*` values to browser code. Do not use that prefix for credentials, provider API keys, filesystem locations, or any value that must remain private.

## Build and release checks

Before shipping frontend changes:

1. Run `npm run build`; Vite must complete without warnings that indicate a broken import or bundle.
2. Run the backend and verify the intended panel against a real or clearly degraded backend configuration.
3. Exercise loading, empty, success, failure, and approval-required states for changed workflows.
4. Test responsive layout, keyboard focus, dialog closing, and any chat/project permalinks affected by the change.
5. If changing `api.js`, validate the exact request method, JSON body, query encoding, response handling, and streamed-event behavior.

## Extension example

To expose a new backend operation, first add a method to `api.js`, such as `listWidgets: () => request("/widgets")`. Build a `Widgets.jsx` panel that calls it during initialization, renders pending and failure states, and invokes a refresh after mutations. Finally, wire that panel into `App.jsx` through the same settings/modal pattern used by adjacent feature areas. Keep authorization and safety logic on the backend; the frontend’s responsibility is transparent initiation and accurate status presentation.

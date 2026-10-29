# Frontend guide

Technical reference for Neo's browser app: structure, state handling, the conventions that keep
it fast and race-free, and how to work on it. For installation see [setup.md](setup.md); for the
API it talks to see [backend.md](backend.md).

## Contents

1. [Stack](#stack)
2. [Directory layout](#directory-layout)
3. [Running, building, testing](#running-building-testing)
4. [App shell and navigation](#app-shell-and-navigation)
5. [Talking to the backend](#talking-to-the-backend)
6. [Chat](#chat)
7. [Panels and code splitting](#panels-and-code-splitting)
8. [Keyboard system](#keyboard-system)
9. [Notes](#notes)
10. [Themes, backgrounds and styling](#themes-backgrounds-and-styling)
11. [Voice dictation](#voice-dictation)
12. [Responsive and mobile behaviour](#responsive-and-mobile-behaviour)
13. [State and concurrency conventions](#state-and-concurrency-conventions)
14. [Screens reference](#screens-reference)
15. [Testing](#testing)
16. [Benchmarks](#benchmarks)
17. [Adding things](#adding-things)

---

## Stack

- **React 18** with function components and hooks, **Vite 5** for dev server and build.
- **Plain JavaScript** (`.js` / `.jsx`), no TypeScript.
- **No router, no state library.** Navigation is app state plus history `pushState` permalinks;
  state is `useState`/`useReducer`, small external stores, and `useSyncExternalStore`.
- **Tailwind 3 is present only for its preflight reset** (and `width`). The interface is
  written in hand-authored CSS in `src/index.css`; colours are CSS custom properties.
- Libraries: **TipTap 3** (notes editor and extensions), **marked** (markdown in notes),
  **perfect-freehand** (sketches), **three** (a chat background effect). Chat markdown is rendered
  by Neo's own small renderer (`markdown.js`), not a library.
- No service worker. `main.jsx` actively unregisters any left by older builds and clears
  Cache Storage. The app renders under `React.StrictMode`, so effects mount, unmount and remount
  in development; anything with a loop or listener must tear down cleanly.

## Directory layout

```text
frontend/
  index.html              Vite entry (mounts #root, loads src/main.jsx)
  vite.config.js          Dev server :5173, proxies /api -> backend
  tailwind.config.js      Preflight only
  public/assets/          Static files (e.g. the PCM recorder AudioWorklet)
  src/
    main.jsx              Root render, service-worker cleanup
    App.jsx               The app shell: sidebar, chat view, settings, most wiring (large)
    api.js                `request()` + the `api` object: every backend call
    index.css             All styling, themes, tokens (~5.9k lines)
    <Panel>.jsx           One file per screen/panel (Notes, Research, Gallery, ...)
    chat*.js, sendGuard.js, setWriter.js, modalStack.js, ...   Logic modules (see below)
    keys/                 Keyboard engine, commands, keymap, palette search
    notes/                Notes controller, saver, crypto, editor, TipTap extensions, sketch
    backgrounds/          Animated chat backgrounds + render budget
    voice/                Dictation state machine, recorder, resampler
  tests/                  node:test suites + JSX loader hook
  bench/                  Browser benchmarks (CDP-driven), fixtures, seeders
```

Rule of thumb: **UI in `.jsx`, rules in `.js`.** Behaviour that can be reasoned about without a DOM
(race handling, state machines, formatting, key dispatch) lives in plain modules so it can be unit
tested in Node.

## Running, building, testing

From `frontend/`:

| Command | What |
|---|---|
| `npm ci` | Install exact dependencies (uses `package-lock.json`). |
| `npm run dev` | Vite dev server on `127.0.0.1:5173`, proxying `/api`. |
| `npm run build` | Production build to `frontend/dist`. |
| `npm run preview` | Serve the production build (used by benchmarks). |
| `npm test` | Run `node --test` over `tests/*.test.mjs`. |

Environment variables (Vite): `VITE_API_PROXY_TARGET` (dev proxy target, default
`http://127.0.0.1:8000`) and `VITE_API_BASE_URL` (API prefix, default `/api`).

In production the backend serves `dist` itself (Docker copies it to `/app/app/static`), so the
app and API share one origin.

**Judge performance on production builds**, throttled, and across browsers; dev builds with
StrictMode double-invocation are not representative. See [Benchmarks](#benchmarks).

## App shell and navigation

`App.jsx` holds the shell. Notable structure:

- **Profile gate:** until a profile session exists, `ProfilePicker` is shown (create, unlock,
  guest). Switching or ending a profile disposes profile-scoped state (notes controller,
  caches); a test (`profile-scoped-state`) holds this.
- **Sidebar:** pinned/recent chats, projects, search, the **SYSTEM** list (Memory, Research, Notes,
  Calendar, Gallery, Local Models, Compare Models, ...) and Settings. SYSTEM entries are
  per-profile toggles; their ids are defined in `systemNav.js` and **mirrored in the backend**
  (`app/services/sidebar_nav.py`) so invalid ids can be refused before storage.
- **Views:** an `activeView` value (chat, settings, or a panel id) decides what is on screen. It is
  also what keyboard shortcut scopes are keyed to.
- **Permalinks:** `parsePermalink` / `updatePermalink` / `chatPermalink` keep chat and project URLs
  in the address bar with `history.pushState`; links respect modifier-clicks
  (`handlePermalinkClick`).
- **Modals:** a shared `Modal` component registers with `modalStack.js`.

## Talking to the backend

All HTTP goes through `api.js`.

- `request(path, options)` prefixes `API_BASE`, sets JSON headers (skipped for `FormData`),
  and **adds the CSRF header** for non-safe methods: it reads the `neo_csrf` cookie and sends it as
  `X-Neo-Csrf`. This pairs with the backend's double-submit check.
- Errors are normalised (`errorDetail` handles strings, FastAPI validation arrays and objects).
  Network failure becomes "Backend API is not reachable"; **`AbortError` is passed through**
  unchanged so callers can distinguish "superseded" from "down".
- `204` returns `null`; JSON responses are parsed; other types return text.
- The `api` object is a flat catalogue of named functions (`api.accountProfiles()`,
  `api.chatTools(id)`, ...). Components never build URLs themselves.
- A protected route called without a session returns `401 authenticated_profile_required` from the backend; the app gates on the profile session before loading anything profile-owned.

Streaming: chat output is not read from an HTTP stream in the component. It is tailed from the
chat's NDJSON event log (see [Chat](#chat)).

## Chat

The core screen, mostly in `App.jsx` (`ChatMessage`, composer) with logic split out:

| Module | Responsibility |
|---|---|
| `chatStream.js` | `useChatStreams`: reduces the backend event log into live turn state (`text`, `thinking`, agent `entries`, `todo`, `approval`, `sessionStatus`). One shape covers plain replies and agent runs. Batches updates on animation frames with a 250 ms backstop for hidden tabs. |
| `chatView.js` | Which chat is on screen. `shouldCommitLoad` ensures a slow response for an old navigation never replaces a newer one; sidebar edits are patched optimistically (`patchSidebarChat`). |
| `chatPresentation.js` | Formatting (durations, tokens, timestamps), message HTML caching, arrival keys (`createArrivals`) so only genuinely new rows animate. |
| `chatStatus.js`, `chatEffort.js` | Status text and the Low/High effort tooltips; "always reasons" notes are derived from observed replies, never guessed from model names. |
| `sendGuard.js` | Single-flight guard for submission: `begin` flips synchronously (a ref, not React state), so a double-click in one tick cannot create two turns. Each submission owns one request id (`createRequestId`) reused on retry, which the backend uses for idempotency. |
| `markdown.js` | Safe markdown to HTML: escapes first, links limited to `http(s)`/`mailto`, code spans split out before emphasis, linear-time regexes. |
| `AgentTurn.jsx` | Renders an agent run inside the thread: steps, tool calls, approvals, todo, executor labels. |
| `MessageActionsMenu.jsx`, `ContextWindowIndicator.jsx`, `BackgroundTurnToast.jsx` | Per-message actions, context-usage ring, notification when a background turn finishes. |

Flow of one turn: composer -> `sendGuard.begin` -> `api` starts a generation -> the event stream
reducer shows progressive text/thinking -> on a terminal event (`run.completed`, `run.failed`,
`run.cancelled`) the transcript is reloaded from message rows and the live state returns to idle.
Because turns are durable on the server, refreshing the page resumes the live view.

## Panels and code splitting

Every secondary screen is lazy-loaded.

- `lazyPanel(() => import("./X.jsx"))` wraps a component in `Suspense` and an error boundary
  (a failed chunk shows "This view could not load. Reload Neo to try again."). It exposes
  `.preload()` and remembers resolution so an already-downloaded panel renders with **no loading
  flash**. A panel never swaps component type under its own state.
- `panelPreload.js` (`usePanelPreloading`) fetches code ahead of need without interfering:
  - *Intent:* focus/press/80 ms hover on a control marked `data-preload="<id>"` starts the
    download (max two at once).
  - *Background:* sidebar destinations the profile actually shows, one at a time during idle time,
    a couple of seconds after load. Paused during input and when the tab is hidden; **skipped
    entirely** on Save-Data, 2G, or <=2 GB device memory (`isConstrained`).

## Keyboard system

`src/keys/` is a small framework; read the header comment of each file.

- `commands.js`: **data only.** Every command the keyboard can reach, with default bindings. A
  command can have `keys` (modifier chord, works anywhere) and `altKeys` (a single letter or a
  `g` sequence, which only fires when not typing in a field). `keys: ""` means unbound by default.
- `keymap.js` builds the active keymap from defaults plus the profile's rebinds (stored via the
  `keybindings` API); `chord.js` parses/normalises chords; `capture.js` records new ones in
  settings.
- `dispatcher.js` decides what a keystroke means (pure, testable); `mode.js` classifies focus
  (typing in a field or not); `engine.js` is the one window listener with a 900 ms sequence
  timeout; `registry.js` maps command ids to handlers.
- **Handlers are registered by the component that owns the state** (`registerCommandHandlers`),
  as a module-level stack so the most recent wins and unmount removes only its own entry. No
  state needs lifting into `App` to become keyboard-reachable. A handler returning `false`
  declines and leaves the key to the browser.
- Precedence rules: events already `preventDefault`ed by nearer handlers are skipped (composer
  Enter, note Tab), and **the engine is inert while any dialog is open** (the modal stack owns
  Escape).
- `paletteSearch.js` powers the command palette (`CommandPalette.jsx`); `KeyboardSettings.jsx` is
  the rebinding UI. `useCommands.js` has the React glue (`useKeyboardEngine`, `useScopes`).

## Notes

A full Apple-Notes-style app in `src/notes/` plus `Notes.jsx`.

- **`notesController.js`:** all Notes state, *outside React*, one controller per profile, read via
  `useSyncExternalStore` selectors. It outlives the screen so leaving Notes never drops a save.
  It is built against specific races: numbered opens (newest wins), bounded note cache with
  background refresh, keyed writers for metadata (optimistic, one request in flight), deletes that
  wait behind pending saves, and cross-tab announcements over `BroadcastChannel` (a tab with
  unsaved edits meets a conflict on next save and both versions are kept).
- **`noteSaver.js`:** one queue per note, never more than one write in flight. Edits go to a
  **local recovery journal** (`recoveryJournal.js`) about 250 ms after typing, and to the server after
  600 ms (or 3 s into a long burst), so content is on the device whatever the network does.
  Replies update revision and list rows but **never touch the editor**. Conflicts, deleted notes
  and refused writes block that note's queue until the user decides; offline/5xx retry with
  backoff. New notes are drafts until first save; the create carries a fixed write id so a retried
  create is idempotent.
- **Locked notes** (`crypto.js`, `noteKeys.js`, `lockTransitions.js`): encryption happens in the
  browser with WebCrypto. The server stores a data key only wrapped (AES-KW) by a key derived
  (PBKDF2-SHA256) from the profile password; content is AES-GCM-256 with a fresh IV per save and
  associated data binding the ciphertext to note id and key version. Plaintext never leaves the
  page. Keys live in memory only: a profile switch, reload, or **15 idle minutes** locks all
  notes. If sealing is impossible, an edit is **not** saved anywhere new; there is no plaintext
  fallback. The server-side counterpart is `app/services/notes/keys.py` (the two implementations are meant to agree on shared key-derivation test vectors).
- **Editor:** `NoteEditor.jsx` on TipTap with local extensions in `notes/extensions/`
  (attachments, blocks, checklist, editing, note links, sections, sketch). `SketchEditor.jsx`
  with `perfect-freehand` strokes. `noteSchema.js`, `projection.js` and `fingerprint.js` handle
  content shape and change detection.
- **Organisation:** folders, tags (`tags.js`), smart folders (`smartRules.js`,
  `SmartFolderDialog.jsx`), dates (`noteDates.js`), list/sidebar components, keyboard navigation
  (`noteKeys`-adjacent tests).

## Themes, backgrounds and styling

- **CSS tokens:** `index.css` begins with the palette contract: a theme declares ~15 anchor
  colours and every other token is mixed from them. Themes are selected by `data-theme` on
  `<html>`; **no attribute means the default (Crimson)**.
- **Themes** are listed in `themes.js` (`crimson`, `phosphor`, `cyberpunk`, `amber`, `ice`,
  `mono`, `indigo`, `paper`). `themes.js` is only the picker's data; the stylesheet owns the
  palette. A theme with a swatch but no `[data-theme]` block fails a test. Ids are stored in the
  profile database and must not be renamed.
- **Backgrounds** (`backgrounds/`): `none`, `jellyfish` (default), `stars`, `rain`, `gradient`,
  each with intensity (subtle/medium/vivid). `engine.js` is the **single animation loop**: it owns
  sizing, DPR, resize, frame loop, tab visibility, reduced motion, theme changes and teardown;
  effect modules just draw. `renderBudget.js` adapts cost to the device. Diagnostics code is
  folded out of production builds via `import.meta.env`.
- Settings screens: `AppearanceSettings`, `BackgroundSettings`; the backend persists choices in
  `app/services/appearance.py` (whose id list is held in sync with `backgrounds/index.js` by a test).
- **CSS rules:** author scoped hand-written classes in `index.css`; do not add Tailwind utilities
  (core plugins are deliberately limited to keep style recalculation cheap).

## Voice dictation

`voice/` implements push-to-talk dictation into the composer.

- `dictationMachine.js` is an explicit state machine (`idle -> starting -> recording ->
  processing -> idle`, with `error`) kept free of browser APIs so illegal transitions are testable.
- `useDictation.js` owns side effects; `recorder.js` captures audio through an AudioWorklet
  (`public/assets/pcm-recorder.worklet.js`), `resampler.js` converts to the rate the backend expects,
  `meter.js` drives the level indicator, `insertion.js` places text at the caret/selection.
- Backend support is optional (`.[voice]` extra); `VoiceSettings.jsx` downloads models and shows
  availability.

## Responsive and mobile behaviour

At **<= 640 px** (`NARROW_SHELL_QUERY` in `MobileShell.jsx`) the sidebar becomes a drawer opened
from a slim top bar showing the current screen title. The drawer *is* the desktop sidebar, so
anything added there is reachable on mobile automatically. It closes on navigation, Escape (via the
modal stack) and outside tap, moving focus in and back out. Neo targets real users on any
browser and device, so verify layout and performance on throttled mobile browsers, not just a
desktop Chromium.

## State and concurrency conventions

These recur across the codebase; follow them in new code.

1. **Race-safe opens.** Number each navigation/load; only the latest may commit (`chatView`,
   notes controller). A late reply for something the user left must be dropped.
2. **Serialized optimistic writes.** For preferences and metadata, use a keyed single-flight
   writer (`setWriter.js`): clicks update a `desired` value and one loop sends until the server
   agrees; no disabled controls, no request-per-click, no out-of-order repaint. Replies repaint
   only fields nobody touched since, and failure restores only the last acknowledged value.
3. **Synchronous guards for double submits.** Use a ref-based guard (`sendGuard`), not React state.
4. **Explicit state machines** where booleans multiply (dictation, notes saving).
5. **State outside React** when a lifetime must exceed a screen (notes controller, key registry,
   modal stack), exposed with `useSyncExternalStore`.
6. **Stable actions.** `useStableActions` gives callbacks a stable identity so memoised children do
   not re-render.
7. **Profile scoping.** Anything cached per profile must be disposed on profile change.
8. **Scoped CSS, no layout thrash.** Prefer class toggles over inline style churn; measure.
9. **Single listeners.** One window listener per concern (keys, modals), not one per component.
10. **No em dashes in user-facing strings.** A test (`no-em-dashes`) enforces it.

## Screens reference

| Screen | File | Backend area |
|---|---|---|
| Profile picker | `ProfilePicker.jsx` | `/account-profiles` |
| Chat, sidebar, settings shell | `App.jsx` | `/chats`, `/chat-*` |
| Memory (browse/edit) | `MemoryDialog.jsx`, `RelatedMemories.jsx` | `/memory*` |
| Research | `Research.jsx` | `/research` |
| Notes | `Notes.jsx` + `notes/` | `/notes` |
| Tasks, Calendar | `Tasks.jsx`, `Calendar.jsx`, `CalendarProposalCard.jsx`, `ReminderToast.jsx` | `/tasks`, `/calendar` |
| Projects | `Projects.jsx` | `/projects`, `/chat-projects` |
| Files, Repos, folder browsing | `Files.jsx`, `Repos.jsx`, `FolderBrowser.jsx`, `OpenFolderDialog.jsx` | `/files`, `/repos`, `/workspaces` |
| Gallery | `Gallery.jsx`, `GalleryImages.jsx`, `ImageLightbox.jsx` | `/gallery` |
| Agent settings, tools, skills, rules | `AgentSettings.jsx`, `ChatToolsPanel.jsx`, `SkillsPanel.jsx`, `RulesProfiles.jsx` | `/agents`, `/skills`, `/rules` |
| External agents | `ExternalAgents.jsx`, `ExternalAgentSetup.jsx` | `/external-agents` |
| Code tooling | `CodebaseIndex.jsx`, `SymbolAwareness.jsx`, `LspPanel.jsx`, `PatchApplications.jsx`, `GitCheckpoints.jsx`, `GitHub.jsx`, `TestRunner.jsx`, `CommandSandbox.jsx` | code index, symbols, lsp, patches, git, test runner, command sandbox |
| Connected accounts | `Integrations.jsx` | `/integrations` |
| Models | `LocalModels.jsx`, `CompareModels.jsx`, `ProviderRuntime.jsx`, `UsagePanel.jsx`, `EvaluationHarness.jsx` | `/local-models`, `/model-compare`, `/provider-runtime`, `/evaluation` |
| Memory health / retrieval / context | `MemoryRetrieval.jsx`, `ContextMemory.jsx`, `WorkspaceOrchestration.jsx`, `Continuity.jsx`, `Bundles.jsx` | matching routes |
| Preferences | `AppearanceSettings`, `BackgroundSettings`, `KeyboardSettings`, `VoiceSettings`, `SidebarItemsSettings`, `AccountSettings` | `/appearance`, `/keybindings`, `/voice`, `/sidebar-nav` |
| Web search | `WebSearch.jsx` | `/web-search` |

## Testing

`npm test` runs `node --test tests/*.test.mjs` with `tests/jsx-hooks.mjs`, which registers a module
hook so Node can import the app's real `.jsx` sources (esbuild, already shipped inside Vite, strips
JSX; `import.meta.env` is defined; `?raw` imports resolve to file text). There is no jsdom: tests
target pure modules and render-isolation rules rather than a simulated DOM.

Coverage areas (one file per topic): chat streaming, arrivals, effort, view and sidebar rules;
send guard and set writer races; CSRF header handling; keyboard (chord, dispatcher, engine, keymap,
mode, palette, capture); notes (controller, saver, crypto vectors, lock transitions, projection,
sketch, smart rules, dates); backgrounds and render budget; voice (machine, insertion, resampler,
composer); panel preloading; modal stack; markdown; mobile shell; profile-scoped state; the
no-em-dashes rule.

When you fix a race or a rule, add a test at the module level that fails without the fix.

## Benchmarks

`bench/` holds browser benchmarks driven over the Chrome DevTools Protocol against a **production
build** (`npm run build && npm run preview -- --port 4173 --strictPort`) with the backend on :8000.

- `interaction.mjs`: input-to-painted latency, Event Timing, long tasks / long animation frames,
  lazy-panel fallback flashes, entrance-animation counts; can delay or fail API calls through fetch
  interception to make optimistic updates and races observable. Seeds a disposable profile with
  `seed_profile.py`. Env: `BENCH_ORIGIN`, `BENCH_LABEL`, `BENCH_CPU` (throttle list),
  `BENCH_REPEATS`, `BENCH_CACHE`.
- `mobile-android.mjs`: real-input Chrome on an Android emulator.
- `markdown-browsers.mjs`, `notes-browsers.mjs`, `matrix.mjs`, `drive.mjs`, plus HTML/JSX pages
  (`transcript`, `loading`, `notes-browser`) that isolate a component for measurement.

Compare before/after labels at CPU throttle levels (e.g. `BENCH_CPU=1,4`) across browsers.

## Adding things

- **A new panel:** create `Foo.jsx`; add `const Foo = lazyPanel(() => import("./Foo.jsx"))` in
  `App.jsx`; wire its open/close state; for a SYSTEM entry add it to `systemNav.js` **and**
  `app/services/sidebar_nav.py`; mark its trigger with `data-preload="foo"`.
- **A new API call:** add a named function to the `api` object in `api.js`; never `fetch`
  directly.
- **A new keyboard command:** add an object to `keys/commands.js`; register its handler in the
  component that owns the state.
- **A new theme:** add an entry to `themes.js` and a `[data-theme="id"]` block in `index.css` with
  the anchor colours; the id is permanent.
- **A new background:** add an entry to `backgrounds/index.js`, an effect module in the same folder,
  and the id to `app/services/appearance.py`.
- **Anything with a loop, observer or timer:** make teardown idempotent; StrictMode will exercise it.

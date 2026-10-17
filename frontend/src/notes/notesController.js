/**
 * The Notes screen's state, outside React.
 *
 * One controller per profile, created on first use and kept while the app runs,
 * so leaving Notes never drops a save in progress. Components read it through
 * `useSyncExternalStore` with selectors; typing in the editor changes nothing
 * here except, at most four times a second, the open note's list row.
 *
 * Races it is built against:
 *   - Opening notes: every open is numbered and cancels the one before; only the
 *     newest may change what is shown. A note already in the bounded cache shows
 *     at once and is refreshed behind.
 *   - Loading the list and the sidebar: same rule, so a slow reply for an old
 *     view or search never replaces a newer one.
 *   - Saving content: see noteSaver.js. Replies update list rows and revisions.
 *   - Metadata (pin, archive, folder): one keyed writer per note (setWriter.js):
 *     optimistic, one request in flight, a reply repaints only fields nobody has
 *     touched since, and a failure restores only the last acknowledged value.
 *   - Deleting a note waits behind that note's pending saves, so its newest
 *     content is what goes to Recently Deleted.
 *   - Other tabs: a saved revision is announced on a BroadcastChannel. A tab with
 *     that note open and nothing unsaved reloads it; one with unsaved edits will
 *     meet a conflict on its next save, and both versions are kept.
 *
 * Locked notes: the keys live in this controller's key session (noteKeys.js),
 * in memory only, so a profile switch (which disposes the controller), a
 * reload or 15 idle minutes locks everything. A locked note's content is
 * decrypted into the open session alone; the cache holds its ciphertext, and
 * the saver seals every edit before the journal or the network sees it.
 */
import { api } from "../api.js";
import { createKeyedWriter } from "../setWriter.js";
import { lockingAvailable, newAid, openContent, openFile, sealContent, sealFile } from "./crypto.js";
import { createKeySession } from "./noteKeys.js";
import { createNoteSaver, replayJournal } from "./noteSaver.js";
import { localTzOffset, matchesRules } from "./smartRules.js";
import { liveRow, markdownManager, newNoteDoc, noteToDoc, projectNote } from "./projection.js";
import {
  createJournal,
  holdTabLock,
  indexedDbJournalAdapter,
  liveTabIds,
  memoryJournalAdapter,
  newTabId,
} from "./recoveryJournal.js";

const CACHE_LIMIT = 30;
const CACHE_BYTES = 8 * 1024 * 1024;
const ROW_THROTTLE = 250;
const LIST_PAGE = 200;
const SIDEBAR_DEBOUNCE = 400;
export const LAST_VIEW_KEY = "neo-notes-last-view";

const DEFAULT_SETTINGS = {
  sort_by: "edited",
  sort_order: "desc",
  group_by_date: true,
  new_note_style: "title",
  checked_items: "manual",
  view: "list",
};

const controllers = new Map();

// Locking and Remove Lock are loaded when first used: most visits never lock anything.
let lockModule = null;
const loadLock = () => (lockModule ??= import("./lockTransitions.js"));

export function notesController(profileId) {
  let controller = controllers.get(profileId);
  if (!controller) {
    controller = createNotesController(profileId);
    controllers.set(profileId, controller);
  }
  return controller;
}

/** Drop every profile's controller except `keepId`, saving first. */
export async function releaseOtherControllers(keepId) {
  for (const [id, controller] of controllers) {
    if (id === keepId) continue;
    controllers.delete(id);
    await controller.dispose();
  }
}

/** A document's title, Markdown body and tags, for sending. */
const projectDoc = (doc) => projectNote(doc, markdownManager());

function transportFor() {
  return {
    async create(payload) {
      const reply = await api.createNote(payload);
      return { note: reply.note, replayed: Boolean(reply.replayed) };
    },
    async write(id, payload) {
      return api.writeNoteContent(id, payload);
    },
  };
}

/** Whether a note belongs in a view, judged locally (after pin/move/archive/delete). */
export function matchesView(item, view) {
  if (!item) return false;
  if (view.q) return !item.deleted;
  switch (view.kind) {
    case "deleted":
      return Boolean(item.deleted) && Boolean(item.deleted_at);
    case "archived":
      return !item.deleted && Boolean(item.archived);
    case "folder":
      return !item.deleted && !item.archived && (item.folder_id ?? null) === (view.folderId === "default" ? null : view.folderId);
    case "smart":
      // The rules travel with the view; without them (still loading) the server's list stands.
      return view.rules ? matchesRules(view.rules, item) : !item.deleted && !item.archived;
    case "tags": {
      if (item.deleted || item.archived) return false;
      const tags = new Set(item.tags ?? []);
      return view.tagsMode === "all" ? view.tags.every((tag) => tags.has(tag)) : view.tags.some((tag) => tags.has(tag));
    }
    default:
      return !item.deleted && !item.archived;
  }
}

const META_FIELDS = new Set(["pinned", "archived", "folder_id", "deleted", "deleted_at", "meta_revision", "meta_updated_at"]);
const LIVE_FIELDS = new Set(["title", "preview"]);
const TRANSIENT_FIELDS = new Set(["doc", "body", "cipher"]);

/**
 * Server facts about a note merged over what this tab already shows. Content
 * and metadata are judged apart, each by its own revision, so a reply that left
 * the server before a newer change (a save reply racing a pin, a list fetched
 * before a delete) never puts back what that change replaced. Fields a
 * metadata write still has in flight (`pending`) stay as the person set them,
 * and with `live` the title and preview the editor is ahead on stay too.
 */
export function mergeServerNote(existing, incoming, { pending = {}, live = false } = {}) {
  if (!existing) {
    const fresh = { ...incoming };
    for (const field of TRANSIENT_FIELDS) delete fresh[field];
    return fresh;
  }
  const merged = { ...existing };
  const contentNewer = incoming.revision != null && incoming.revision >= (existing.revision ?? 0);
  const metaNewer = incoming.meta_revision != null && incoming.meta_revision >= (existing.meta_revision ?? 0);
  for (const [field, value] of Object.entries(incoming)) {
    if (value === undefined || TRANSIENT_FIELDS.has(field)) continue;
    if (META_FIELDS.has(field)) {
      if (metaNewer && !(field in pending)) merged[field] = value;
    } else if (contentNewer && !(live && LIVE_FIELDS.has(field))) {
      merged[field] = value;
    }
  }
  return merged;
}

export function viewQuery(view, settings) {
  const sort = { sort: settings.sort_by, order: settings.sort_order };
  if (view.q) return { q: view.q, includeArchived: true, ...sort };
  if (view.kind === "deleted") return { view: "deleted", pinnedFirst: false, ...sort };
  if (view.kind === "archived") return { view: "archived", ...sort };
  if (view.kind === "folder") return { view: "folder", folderId: view.folderId, ...sort };
  if (view.kind === "tags") return { view: "tag", tags: view.tags, tagsMode: view.tagsMode, ...sort };
  if (view.kind === "smart") return { view: "smart", smartId: view.smartId, tzOffset: localTzOffset(), ...sort };
  return sort;
}

function compareItems(settings) {
  const field = settings.sort_by === "created" ? "created_at" : settings.sort_by === "title" ? "title" : "updated_at";
  const direction = settings.sort_order === "asc" ? 1 : -1;
  return (a, b) => {
    if (Boolean(b.pinned) !== Boolean(a.pinned)) return b.pinned ? 1 : -1;
    const left = String(a[field] ?? "").toLowerCase();
    const right = String(b[field] ?? "").toLowerCase();
    return left < right ? -direction : left > right ? direction : 0;
  };
}

function readLastView() {
  try {
    const stored = JSON.parse(localStorage.getItem(LAST_VIEW_KEY) || "null");
    if (stored && typeof stored.kind === "string") return { tags: [], tagsMode: "any", q: "", ...stored };
  } catch {
    /* unavailable or malformed: start at All Notes */
  }
  return null;
}

export function createNotesController(profileId) {
  const listeners = new Set();
  let state = {
    profileId,
    ready: false,
    view: readLastView() ?? { kind: "all", folderId: null, tags: [], tagsMode: "any", q: "" },
    list: { status: "idle", ids: [], total: 0, error: "" },
    items: {},
    drafts: {},
    sidebar: { status: "idle", folders: [], counts: { all: 0, default: 0, archived: 0, deleted: 0 }, tags: [] },
    settings: DEFAULT_SETTINGS,
    selection: null,
    session: null,
    saveStatus: {},
    toast: null,
    notice: "",
    recovered: [],
    // Locked notes: whether this page can lock (secure context), whether this
    // profile can (guests can't), whether a key exists, and whether it is open.
    lock: { available: lockingAvailable(), guest: false, setUp: false, unlocked: false, unavailable: [], busy: null },
    // A password being asked for: { mode: "setup" | "unlock" | "earlier", then }.
    lockPrompt: null,
  };

  const tabId = newTabId();
  const releaseLock = holdTabLock(tabId);
  const cache = new Map();
  const summaries = new Map();
  const metaWriters = new Map();
  const editorTakes = new Map();
  const trashing = new Set();
  let journal = null;
  let saver = null;
  let channel = null;
  let openSeq = 0;
  let openAbort = null;
  let listSeq = 0;
  let listAbort = null;
  let sidebarSeq = 0;
  let sidebarAbort = null;
  let sidebarTimer = null;
  let rowTimer = null;
  let pendingRow = null;
  let readyPromise = null;
  let disposed = false;
  let toastTimer = null;
  let settingsWriter = null;

  // ------------------------------------------------------------ locked notes --

  const keys = createKeySession({
    transport: { unlock: api.unlockNoteKeys, setup: api.setupNoteKeys, rewrap: api.rewrapNoteKeys },
  });
  /** Each locked note's key version (part of what its ciphertext is bound to). */
  const lockVersions = new Map();
  /** Locked notes that still hold their original Markdown: their first edit keeps it as a version. */
  const keepOriginal = new Set();
  /** Decrypted attachments shown in the open locked note, by file id. */
  const blobUrls = new Map();

  function set(patch) {
    const before = state.items;
    state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
    for (const listener of listeners) listener();
    if (state.items !== before) announceTitles();
  }

  // ------------------------------------------------------------ note titles --
  // For links between notes: what a linked note is called now, and whether it
  // is still there. Known from the list, or asked of the server once.

  const titleListeners = new Set();
  const fetchedTitles = new Map();
  const fetchingTitles = new Set();
  let titlesQueued = false;

  function announceTitles() {
    if (titlesQueued || !titleListeners.size) return;
    titlesQueued = true;
    queueMicrotask(() => {
      titlesQueued = false;
      for (const listener of titleListeners) listener();
    });
  }

  function noteTitleOf(id) {
    if (!id) return { title: null, missing: true };
    const item = state.items[id];
    if (item) return { title: item.title || "New Note", missing: Boolean(item.deleted) };
    if (fetchedTitles.has(id)) return fetchedTitles.get(id);
    if (!fetchingTitles.has(id)) {
      fetchingTitles.add(id);
      api
        .note(id)
        .then(({ note }) => fetchedTitles.set(id, { title: note.title || "New Note", missing: false }))
        .catch((error) => {
          if (error?.status === 404 || error?.status === 410) fetchedTitles.set(id, { title: null, missing: true });
        })
        .finally(() => {
          fetchingTitles.delete(id);
          for (const listener of titleListeners) listener();
        });
    }
    return null;
  }

  /** Notes to link to, by title: what the list already holds, then the server. */
  async function searchNoteTitles(query, excludeId = null) {
    const wanted = query.trim().toLowerCase();
    const seen = new Set([excludeId]);
    const local = Object.values(state.items)
      .filter((item) => !item.deleted && !seen.has(item.id) && (!wanted || (item.title || "").toLowerCase().includes(wanted)))
      .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
      .slice(0, 8)
      .map((item) => ({ id: item.id, title: item.title || "New Note" }));
    local.forEach((item) => seen.add(item.id));
    if (local.length >= 8 || !wanted) return local;
    try {
      const { notes } = await api.notesList({ q: query, limit: 8, includeBody: false, includeArchived: true });
      return [...local, ...notes.filter((note) => !seen.has(note.id)).map((note) => ({ id: note.id, title: note.title || "New Note" }))].slice(0, 8);
    } catch {
      return local;
    }
  }

  function remember(note) {
    if (!note?.id) return;
    cache.delete(note.id);
    cache.set(note.id, note);
    let bytes = 0;
    for (const value of cache.values()) bytes += JSON.stringify(value.doc ?? value.body ?? "").length;
    while (cache.size > CACHE_LIMIT || (bytes > CACHE_BYTES && cache.size > 1)) {
      const [oldest, value] = cache.entries().next().value;
      bytes -= JSON.stringify(value.doc ?? value.body ?? "").length;
      cache.delete(oldest);
    }
  }

  /** Metadata fields this tab has asked for and the server has not yet acknowledged. */
  function pendingMeta(id) {
    const pending = { ...(metaWriters.get(id)?.pending() ?? {}) };
    if (trashing.has(id)) Object.assign(pending, { deleted: true, deleted_at: true });
    return pending;
  }

  /** Whether the editor holds newer text for this note than any save has carried. */
  function editingAhead(id) {
    const key = state.session?.id === id ? state.session.key : null;
    return Boolean(key && saver?.isDirty(key));
  }

  function mergeFor(existing, note) {
    return mergeServerNote(existing, note, { pending: pendingMeta(note.id), live: editingAhead(note.id) });
  }

  /** Merge server facts about a note into its list row; place it per the current sort. */
  function upsertItem(note) {
    if (!note?.id) return;
    set((current) => {
      const merged = mergeFor(current.items[note.id], note);
      const items = { ...current.items, [note.id]: merged };
      let ids = current.list.ids;
      if (matchesView(merged, current.view) && !ids.includes(note.id)) ids = [note.id, ...ids];
      const sorted = [...ids].sort((a, b) => compareItems(current.settings)(items[a], items[b]));
      return { items, list: { ...current.list, ids: sorted } };
    });
  }

  function patchItem(id, fields) {
    set((current) => {
      if (!current.items[id]) return {};
      return { items: { ...current.items, [id]: { ...current.items[id], ...fields } } };
    });
  }

  function showToast(toast) {
    clearTimeout(toastTimer);
    const next = { id: Date.now(), ...toast };
    set({ toast: next });
    toastTimer = setTimeout(() => {
      if (state.toast?.id === next.id) set({ toast: null });
    }, toast.action ? 6000 : 4000);
  }

  function onSaverEvent(event) {
    if (disposed) return;
    if (event.type === "status") {
      set((current) => ({ saveStatus: { ...current.saveStatus, [event.key]: event.status } }));
      if (["conflict", "gone", "locked", "invalid"].includes(event.status) && state.session?.key === event.key) {
        set((current) => ({
          session: {
            ...current.session,
            blocked: event.status,
            blockedDetail: event.detail ?? null,
            readOnly: event.status !== "invalid" ? true : current.session.readOnly,
          },
        }));
      }
      return;
    }
    if (event.type === "created") {
      set((current) => {
        const drafts = { ...current.drafts };
        delete drafts[event.key];
        const session = current.session?.key === event.key ? { ...current.session, id: event.note.id } : current.session;
        const selection = current.selection?.key === event.key ? { ...current.selection, id: event.note.id } : current.selection;
        return { drafts, session, selection };
      });
      upsertItem(event.note);
      scheduleSidebar();
      return;
    }
    if (event.type === "saved") {
      if (event.note.content_format === "encrypted") keepOriginal.delete(event.note.id);
      remember(event.note);
      upsertItem(event.note);
      if (state.session?.key === event.key) {
        set((current) => ({
          session: { ...current.session, note: { ...current.session.note, ...mergeFor(current.session.note, event.note) } },
        }));
      }
      channel?.postMessage({ type: "saved", id: event.note.id, revision: event.note.revision, tabId });
      return;
    }
    if (event.type === "journal-error") {
      set({ notice: "Unsaved changes can't be kept on this device right now. They are saved to Neo when the connection allows." });
    }
    if (event.type === "seal-error") {
      set({ notice: "A locked note's latest change couldn't be encrypted, so it wasn't kept. Unlock the note to keep editing." });
    }
  }

  /** A locked note's edit, sealed: what the journal keeps and the server receives. */
  async function sealSnapshot(noteId, snapshot) {
    const version = lockVersions.get(noteId) ?? keys.currentVersion();
    const key = version == null ? null : keys.keyFor(version);
    if (!key) throw new Error("The note is locked.");
    const cipher = await sealContent(key, noteId, version, { format: "rich", doc: snapshot.doc, summary: snapshot.summary ?? null });
    const { filesOfDoc } = await loadLock();
    return {
      title: liveRow(snapshot.doc).title.slice(0, 200),
      cipher,
      lock_key_version: version,
      attachment_ids: [...filesOfDoc(snapshot.doc).keys()],
      ...(keepOriginal.has(noteId) ? { keep_original: true } : {}),
    };
  }

  function syncLockState(patch = {}) {
    const keyState = keys.state();
    set((current) => ({ lock: { ...current.lock, unlocked: keyState.unlocked, unavailable: keyState.unavailable, ...patch } }));
  }

  function revokeBlobs() {
    for (const url of blobUrls.values()) URL.revokeObjectURL?.(url);
    blobUrls.clear();
  }

  // Before the keys go: the editor stops taking input, and what is unsaved is
  // encrypted into the journal and the save queue; then the plaintext goes.
  keys.onBeforeLock(async () => {
    if (state.session?.format === "encrypted") set((current) => ({ session: { ...current.session, readOnly: true } }));
    captureSession();
    await saver?.persistAll();
    for (const key of saver?.trackedKeys() ?? []) {
      if (saver.format(key) === "encrypted") saver.forgetPlaintext(key);
    }
    saver?.flushAll({ strict: false }).catch(() => {});
  });

  keys.subscribe(() => {
    syncLockState();
    if (keys.isUnlocked()) return;
    // Locked: no decrypted content stays anywhere this page can show it.
    revokeBlobs();
    const session = state.session;
    if (session?.format === "encrypted") {
      editorTakes.delete(session.key);
      summaries.delete(session.key);
      set({ session: { ...session, doc: null, locked: true, readOnly: true, token: `${session.id}:locked:${Date.now()}` } });
    }
  });


  async function start() {
    const adapter = (await indexedDbJournalAdapter(profileId)) ?? null;
    if (!adapter) {
      set({ notice: "This browser isn't keeping a local copy of unsaved notes. Changes are still saved to Neo." });
    }
    journal = createJournal(adapter ?? memoryJournalAdapter(), { tabId });
    saver = createNoteSaver({ transport: transportFor(), journal, tabId, onEvent: onSaverEvent, project: projectDoc, seal: sealSnapshot });
    api
      .noteKeyState()
      .then((keyState) => syncLockState({ setUp: Boolean(keyState.set_up), guest: Boolean(keyState.guest) }))
      .catch(() => {});
    settingsWriter = createKeyedWriter({
      send: async (changes) => api.updateNoteSettings(changes),
      onState: (fields) => set((current) => ({ settings: { ...current.settings, ...fields } })),
      onError: () => showToast({ text: "Couldn't save that setting." }),
    });
    if (typeof BroadcastChannel === "function") {
      channel = new BroadcastChannel(`neo-notes:${profileId}`);
      channel.onmessage = (message) => onBroadcast(message.data);
    }
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibility);
      window.addEventListener("pagehide", onPageHide);
      window.addEventListener("beforeunload", onBeforeUnload);
      window.addEventListener("online", onOnline);
      document.addEventListener("freeze", onPageHide);
    }
    try {
      const settings = await api.noteSettings();
      set({ settings: { ...DEFAULT_SETTINGS, ...settings } });
    } catch {
      /* defaults stand until the next load */
    }
    await recover();
    set({ ready: true });
  }

  async function recover() {
    if (!journal) return;
    try {
      const outcomes = await replayJournal({ journal, transport: transportFor(), live: await liveTabIds(), project: projectDoc });
      const recovered = outcomes.filter((item) => item.result !== "deferred");
      if (recovered.length) {
        set((current) => ({ recovered: [...current.recovered, ...recovered] }));
        showToast({ text: recovered.length === 1 ? "Recovered 1 unsaved change." : `Recovered ${recovered.length} unsaved changes.` });
      }
    } catch {
      /* replay is retried on the next start or when the connection returns */
    }
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") {
      captureSession();
      saver?.persistAll();
      saver?.flushAll({ strict: true }).catch(() => {});
    } else if (state.ready) {
      keys.check();
      // Notes made elsewhere (research, agents, another device) show up on return.
      scheduleSidebar(0);
      refreshList();
    }
  }

  function onPageHide() {
    captureSession();
    saver?.persistAll();
  }

  function onBeforeUnload(event) {
    if (saver?.hasUnsaved()) {
      event.preventDefault();
      event.returnValue = "";
    }
  }

  function onOnline() {
    recover();
    saver?.flushAll({ strict: false }).catch(() => {});
  }

  async function onBroadcast(message) {
    if (!message || message.tabId === tabId) return;
    if (message.type === "flush") {
      // Another tab is about to lock this note: whatever is unsaved here goes first.
      const key = state.session?.id === message.id ? state.session.key : message.id;
      if (saver?.isTracked(key)) {
        captureSession();
        saver.flush(key, { strict: true }).catch(() => {});
      }
      return;
    }
    if (message.type !== "saved") return;
    const session = state.session;
    if (!session || session.id !== message.id || session.note?.revision >= message.revision) return;
    if (saver?.isDirty(session.key)) return;
    cache.delete(message.id);
    openNote(message.id, { quiet: true });
  }

  function sessionFromNote(note, key = note.id) {
    if (note.content_format === "encrypted") {
      lockVersions.set(note.id, note.lock_key_version);
      // Shown locked; `showSession` opens it when the keys are there.
      return {
        key,
        id: note.id,
        note,
        doc: null,
        lossy: false,
        reasons: [],
        format: "encrypted",
        locked: true,
        readOnly: true,
        blocked: null,
        blockedDetail: null,
        token: `${note.id}:${note.revision}:locked`,
      };
    }
    const { doc, lossy, reasons } = noteToDoc(note, markdownManager());
    summaries.set(key, note.summary ?? null);
    return {
      key,
      id: note.id,
      note,
      doc,
      lossy,
      reasons,
      format: note.content_format,
      readOnly: lossy || note.deleted,
      blocked: null,
      blockedDetail: null,
      token: `${note.id}:${note.revision}:${Date.now()}`,
    };
  }

  /** Show a note; a locked one is decrypted into the session if its key is open. */
  function showSession(note) {
    const session = sessionFromNote(note);
    set({ session });
    if (session.format === "encrypted") revealLocked(session);
  }

  /**
   * The newest ciphertext of a locked note: an edit sealed here and not yet
   * acknowledged, else the latest the server returned (saves update the cache,
   * not the session's copy, which can be older).
   */
  function newestCipher(session) {
    const sealed = saver?.isTracked(session.key) ? saver.latestSnapshot(session.key) : null;
    if (sealed?.cipher && saver.isDirty(session.key)) return { cipher: sealed.cipher, version: sealed.lock_key_version };
    const cached = cache.get(session.id);
    const note = cached?.cipher && cached.revision >= (session.note.revision ?? 0) ? cached : session.note;
    return { cipher: note.cipher, version: note.lock_key_version };
  }

  async function revealLocked(session) {
    const { cipher, version } = newestCipher(session);
    const key = keys.keyFor(version);
    if (!key) return;
    try {
      const payload = await openContent(key, session.note.id, version, cipher);
      if (state.session?.token !== session.token) return;
      let doc = payload.doc;
      let lossy = false;
      let reasons = [];
      if (payload.format === "markdown") {
        ({ doc, lossy, reasons } = noteToDoc({ title: session.note.title, body: payload.body, tags: payload.tags ?? [] }, markdownManager()));
        keepOriginal.add(session.id);
      } else {
        keepOriginal.delete(session.id);
      }
      summaries.set(session.key, payload.summary ?? null);
      set({
        session: {
          ...session,
          doc,
          lossy,
          reasons,
          locked: false,
          readOnly: lossy || Boolean(session.note.deleted),
          note: { ...session.note, summary: payload.summary ?? null },
          token: `${session.id}:${session.note.revision}:open`,
        },
      });
    } catch {
      if (state.session?.token === session.token) {
        set({ session: { ...session, error: "This locked note couldn't be opened with this key." } });
      }
    }
  }

  function captureSession() {
    const key = state.session?.key;
    if (key && saver?.isTracked(key)) saver.capture(key);
  }

  function leaveSession() {
    const session = state.session;
    if (!session) return;
    captureSession();
    if (saver?.isTracked(session.key)) saver.flush(session.key, { strict: true }).catch(() => {});
    // A draft nobody typed into is not a note; it simply goes away.
    if (!session.id && !saver?.isDirty(session.key)) {
      saver?.release(session.key);
      set((current) => {
        const drafts = { ...current.drafts };
        delete drafts[session.key];
        return { drafts };
      });
    }
  }

  async function openNote(id, { quiet = false } = {}) {
    await ensureReady();
    if (state.session?.id === id && !quiet) return;
    const seq = ++openSeq;
    openAbort?.abort();
    const abort = new AbortController();
    openAbort = abort;
    if (!quiet) leaveSession();
    set({ selection: { key: id, id } });
    const cached = cache.get(id);
    if (cached) showSession(cached);
    else if (!quiet) set({ session: { key: id, id, loading: true, note: state.items[id] ?? null, token: `loading:${id}` } });
    try {
      const { note } = await api.note(id, { signal: abort.signal, includeDeleted: true });
      if (seq !== openSeq) return;
      remember(note);
      const current = state.session;
      if (current?.id === id && !current.loading && current.note?.revision === note.revision) return;
      if (current?.id === id && saver?.isDirty(current.key)) return;
      showSession(note);
    } catch (error) {
      if (error?.name === "AbortError" || seq !== openSeq) return;
      if (!cached) set({ session: { key: id, id, error: error.message || "Couldn't open this note.", token: `error:${id}` } });
    }
  }

  /** Open a note from elsewhere in Neo, showing it in the view it belongs to. */
  async function revealNote(id) {
    await openNote(id);
    const note = state.session?.id === id ? state.session.note : null;
    if (!note || state.view.q || matchesView(note, state.view)) return;
    const view = note.deleted
      ? { kind: "deleted" }
      : note.archived
        ? { kind: "archived" }
        : state.view.kind === "all"
          ? state.view
          : { kind: "folder", folderId: note.folder_id ?? "default" };
    setView(view);
  }

  function closeSession() {
    leaveSession();
    openSeq += 1;
    openAbort?.abort();
    set({ session: null, selection: null });
  }

  /** Reopen a draft whose first save is still pending, including while offline. */
  function openDraft(key) {
    if (state.session?.key === key) return true;
    const draft = state.drafts[key];
    if (!draft) {
      const savedId = saver?.serverId(key);
      if (savedId) return openNote(savedId);
      return false;
    }
    const snapshot = saver?.latestSnapshot(key);
    if (!snapshot?.doc) return false;
    leaveSession();
    const seq = ++openSeq;
    openAbort?.abort();
    set({
      selection: { key, id: null },
      session: {
        key,
        id: null,
        note: {
          id: null,
          title: draft.title,
          summary: snapshot.summary ?? summaries.get(key) ?? null,
          content_format: "rich",
          revision: 0,
          folder_id: draft.folder_id,
          created_at: draft.created_at,
          updated_at: draft.updated_at,
        },
        doc: snapshot.doc,
        lossy: false,
        reasons: [],
        format: "rich",
        readOnly: false,
        isNew: false,
        token: `draft:${key}:${seq}`,
      },
    });
    return true;
  }

  function newNote({ style, folderId, tag } = {}) {
    leaveSession();
    openSeq += 1;
    openAbort?.abort();
    const view = state.view;
    const folder = folderId !== undefined ? folderId : view.kind === "folder" && view.folderId !== "default" ? view.folderId : null;
    const tagged = tag ?? (view.kind === "tags" && view.tags.length === 1 ? view.tags[0] : null);
    const key = `draft-${globalThis.crypto?.randomUUID?.() ?? Date.now()}`;
    let doc = newNoteDoc(style ?? state.settings.new_note_style);
    if (tagged) doc = { ...doc, content: [...doc.content, { type: "paragraph", content: [{ type: "text", text: `#${tagged}` }] }] };
    const now = new Date().toISOString();
    saver.track(key, { folderId: folder });
    summaries.set(key, null);
    set((current) => ({
      drafts: { ...current.drafts, [key]: { key, title: "", preview: "", updated_at: now, created_at: now, folder_id: folder } },
      selection: { key, id: null },
      session: {
        key,
        id: null,
        note: { id: null, title: "", content_format: "rich", revision: 0, folder_id: folder, created_at: now, updated_at: now },
        doc,
        lossy: false,
        reasons: [],
        format: "rich",
        readOnly: false,
        isNew: true,
        token: `new:${key}`,
      },
    }));
    return key;
  }

  /** The editor shows `key`; `take` snapshots its document on demand. */
  function attachEditor(key, take) {
    // Only the note on screen can be read from the editor.
    editorTakes.clear();
    editorTakes.set(key, take);
  }

  /**
   * The editor reports an edit. Fields the editor does not hold (the summary)
   * travel with every snapshot, or a save would clear them.
   */
  function edit(key) {
    if (!saver) return;
    const take = editorTakes.get(key);
    if (!take) return;
    if (!saver.isTracked(key)) {
      const session = state.session;
      saver.track(key, { serverId: session?.id, revision: session?.note?.revision ?? 0, folderId: session?.note?.folder_id, format: session?.format === "encrypted" ? "encrypted" : "rich" });
    }
    if (state.session?.key === key && state.session.format === "encrypted") {
      if (state.session.locked) return;
      keys.touch();
    }
    if (!summaries.has(key)) summaries.set(key, state.session?.key === key ? state.session.note?.summary ?? null : null);
    if (state.session?.key === key && state.session.format === "markdown") {
      set((current) => ({ session: { ...current.session, format: "rich" } }));
    }
    saver.edit(key, () => ({ ...take(), summary: summaries.get(key) ?? null }));
  }

  function setSummary(key, summary) {
    summaries.set(key, summary?.trim() ? summary : null);
    set((current) =>
      current.session?.key === key ? { session: { ...current.session, note: { ...current.session.note, summary } } } : {},
    );
    edit(key);
  }

  function liveRowChanged(key, doc) {
    pendingRow = { key, doc };
    if (rowTimer) return;
    rowTimer = setTimeout(() => {
      rowTimer = null;
      const row = pendingRow;
      pendingRow = null;
      if (!row) return;
      const { title, preview } = liveRow(row.doc);
      const session = state.session;
      if (!session || session.key !== row.key) return;
      if (session.id && state.items[session.id]) {
        patchItem(session.id, { title: title || "New Note", preview });
      } else if (state.drafts[row.key]) {
        set((cur) => ({ drafts: { ...cur.drafts, [row.key]: { ...cur.drafts[row.key], title, preview } } }));
      }
    }, ROW_THROTTLE);
  }

  // ------------------------------------------------------------ list & views --

  async function refreshList() {
    await ensureReady();
    const seq = ++listSeq;
    listAbort?.abort();
    const abort = new AbortController();
    listAbort = abort;
    const view = state.view;
    set((cur) => ({ list: { ...cur.list, status: "loading", error: "" } }));
    try {
      const data = await api.notesList({ ...viewQuery(view, state.settings), includeBody: false, limit: LIST_PAGE }, { signal: abort.signal });
      if (seq !== listSeq) return;
      const items = { ...state.items };
      for (const note of data.notes) items[note.id] = mergeFor(items[note.id], note);
      set({ items, list: { status: "ready", ids: data.notes.map((note) => note.id), total: data.total, error: "" } });
    } catch (error) {
      if (error?.name === "AbortError" || seq !== listSeq) return;
      set((cur) => ({ list: { ...cur.list, status: "error", error: error.message || "Couldn't load notes." } }));
    }
  }

  function setView(next) {
    const view = { kind: "all", folderId: null, tags: [], tagsMode: "any", q: "", ...next };
    if (view.kind === "smart" && !view.rules) view.rules = state.sidebar.smart_folders?.find((folder) => folder.id === view.smartId)?.rules ?? null;
    // A note that does not belong to the new view is closed (and saved), as when
    // Apple Notes moves to another folder.
    const open = state.session;
    if (open && !open.isNew && open.note && !matchesView(open.note, view)) closeSession();
    set({ view, list: { ...state.list, ids: [] } });
    if (!view.q) {
      try {
        localStorage.setItem(LAST_VIEW_KEY, JSON.stringify({ kind: view.kind, folderId: view.folderId, smartId: view.smartId, tags: view.tags, tagsMode: view.tagsMode }));
      } catch {
        /* storage unavailable */
      }
    }
    refreshList();
  }

  function setSearch(q) {
    const view = { ...state.view, q };
    set({ view });
    refreshList();
  }

  function scheduleSidebar(delay = SIDEBAR_DEBOUNCE) {
    clearTimeout(sidebarTimer);
    sidebarTimer = setTimeout(loadSidebar, delay);
  }

  async function loadSidebar() {
    await ensureReady();
    const seq = ++sidebarSeq;
    sidebarAbort?.abort();
    const abort = new AbortController();
    sidebarAbort = abort;
    try {
      const data = await api.notesSidebar({ signal: abort.signal, tzOffset: localTzOffset() });
      if (seq !== sidebarSeq) return;
      set({ sidebar: { status: "ready", ...data } });
      // A folder that no longer exists cannot stay the view.
      const view = state.view;
      if (view.kind === "folder" && view.folderId !== "default" && !data.folders.some((folder) => folder.id === view.folderId)) {
        setView({ kind: "all" });
      }
      if (view.kind === "smart") {
        const smart = (data.smart_folders ?? []).find((folder) => folder.id === view.smartId);
        if (!smart) setView({ kind: "all" });
        else if (JSON.stringify(smart.rules) !== JSON.stringify(view.rules)) set((current) => ({ view: { ...current.view, rules: smart.rules } }));
      }
    } catch (error) {
      if (error?.name === "AbortError" || seq !== sidebarSeq) return;
      set((cur) => ({ sidebar: { ...cur.sidebar, status: "error" } }));
    }
  }

  // ---------------------------------------------------------------- metadata --

  function metaWriter(id) {
    let writer = metaWriters.get(id);
    if (!writer) {
      writer = createKeyedWriter({
        send: async (changes) => {
          let note = null;
          const acknowledge = (reply) => {
            note = reply.note;
            if (note?.meta_revision != null) noteMetaRevision(id, note.meta_revision);
          };
          if ("pinned" in changes) acknowledge(await api.pinNote(id, changes.pinned));
          if ("archived" in changes) acknowledge(await api.archiveNote(id, changes.archived));
          if ("folder_id" in changes) acknowledge(await api.moveNote(id, changes.folder_id));
          return note ? { pinned: note.pinned, archived: note.archived, folder_id: note.folder_id } : changes;
        },
        onState: (fields) => patchItem(id, fields),
        onError: (error) => showToast({ text: error?.message || "Couldn't update the note." }),
        onSettled: () => scheduleSidebar(),
      });
      metaWriters.set(id, writer);
    }
    return writer;
  }

  /** The server's meta revision after one of this tab's own metadata writes. */
  function noteMetaRevision(id, metaRevision) {
    set((current) => {
      const patch = {};
      const item = current.items[id];
      if (item && metaRevision > (item.meta_revision ?? 0)) patch.items = { ...current.items, [id]: { ...item, meta_revision: metaRevision } };
      const note = current.session?.id === id ? current.session.note : null;
      if (note && metaRevision > (note.meta_revision ?? 0)) patch.session = { ...current.session, note: { ...note, meta_revision: metaRevision } };
      return patch;
    });
  }

  function setMeta(id, fields) {
    // A note opened from elsewhere in Neo may not be in the loaded list yet.
    if (!state.items[id] && state.session?.id === id && state.session.note) upsertItem(state.session.note);
    const item = state.items[id];
    if (!item) return;
    const shown = Object.fromEntries(Object.keys(fields).map((key) => [key, item[key] ?? null]));
    metaWriter(id).set(fields, shown);
    if (state.session?.id === id) {
      set((current) => ({ session: { ...current.session, note: { ...current.session.note, ...fields } } }));
    }
  }

  const pin = (id, pinned) => setMeta(id, { pinned });
  const archive = (id, archived) => setMeta(id, { archived });
  const move = (id, folderId) => setMeta(id, { folder_id: folderId || null });

  async function trash(id) {
    const item = state.items[id];
    // Its newest content first, then the delete.
    const key = state.session?.id === id ? state.session.key : id;
    if (saver?.isTracked(key)) await saver.flush(key, { strict: true }).catch(() => {});
    if (state.session?.id === id) closeSession();
    trashing.add(id);
    patchItem(id, { deleted: true, deleted_at: new Date().toISOString() });
    try {
      const reply = await api.deleteNote(id);
      if (reply?.deleted_at) patchItem(id, { deleted_at: reply.deleted_at });
      if (reply?.meta_revision != null) noteMetaRevision(id, reply.meta_revision);
      trashing.delete(id);
      scheduleSidebar(0);
      showToast({ text: "Moved to Recently Deleted.", actionLabel: "Undo", action: () => restore(id) });
    } catch (error) {
      trashing.delete(id);
      if (item) patchItem(id, { deleted: item.deleted, deleted_at: item.deleted_at });
      showToast({ text: error.message || "Couldn't delete the note." });
    }
  }

  async function restore(id, folderId = null) {
    try {
      const { note } = await api.restoreNote(id, folderId);
      upsertItem(note);
      scheduleSidebar(0);
      if (state.session?.id === id) showSession(note);
      return note;
    } catch (error) {
      showToast({ text: error.message || "Couldn't recover the note." });
      return null;
    }
  }

  async function deleteForever(id) {
    if (state.session?.id === id) closeSession();
    try {
      await api.deleteNotePermanently(id);
    } catch (error) {
      if (error?.status !== 404) {
        showToast({ text: error.message || "Couldn't delete the note." });
        return;
      }
    }
    set((current) => {
      const items = { ...current.items };
      delete items[id];
      return { items, list: { ...current.list, ids: current.list.ids.filter((other) => other !== id) } };
    });
    scheduleSidebar(0);
  }

  async function emptyTrash() {
    if (state.view.kind === "deleted") closeSession();
    try {
      await api.emptyNotesTrash();
    } catch (error) {
      showToast({ text: error.message || "Couldn't empty Recently Deleted." });
    }
    scheduleSidebar(0);
    refreshList();
  }

  async function duplicate(id) {
    const key = state.session?.id === id ? state.session.key : id;
    if (saver?.isTracked(key)) await saver.flush(key, { strict: true }).catch(() => {});
    try {
      const { note } = await api.duplicateNote(id);
      upsertItem(note);
      scheduleSidebar();
      await openNote(note.id);
    } catch (error) {
      showToast({ text: error.message || "Couldn't duplicate the note." });
    }
  }

  // ----------------------------------------------------------------- folders --

  async function createFolder(name, parentId = null) {
    try {
      const folder = await api.createNoteFolder({ name, parent_id: parentId });
      await loadSidebar();
      return folder;
    } catch (error) {
      showToast({ text: error.message || "Couldn't create the folder." });
      return null;
    }
  }

  async function renameFolder(id, name) {
    try {
      await api.updateNoteFolder(id, { name });
    } catch (error) {
      showToast({ text: error.message || "Couldn't rename the folder." });
    }
    await loadSidebar();
  }

  async function moveFolder(id, parentId) {
    try {
      await api.updateNoteFolder(id, parentId ? { parent_id: parentId } : { move_to_root: true });
    } catch (error) {
      showToast({ text: error.message || "Couldn't move the folder." });
    }
    await loadSidebar();
  }

  async function deleteFolder(id) {
    try {
      await api.deleteNoteFolder(id);
    } catch (error) {
      showToast({ text: error.message || "Couldn't delete the folder." });
    }
    if (state.session?.note?.folder_id === id) closeSession();
    await loadSidebar();
    refreshList();
  }

  // ----------------------------------------------------------- smart folders --

  async function createSmartFolder(name, rules) {
    try {
      const folder = await api.createSmartFolder({ name, rules });
      await loadSidebar();
      return folder;
    } catch (error) {
      showToast({ text: error.message || "Couldn't create the smart folder." });
      return null;
    }
  }

  async function updateSmartFolder(id, changes) {
    try {
      const folder = await api.updateSmartFolder(id, changes);
      await loadSidebar();
      if (state.view.kind === "smart" && state.view.smartId === id) {
        set((current) => ({ view: { ...current.view, rules: folder.rules } }));
        refreshList();
      }
      return folder;
    } catch (error) {
      showToast({ text: error.message || "Couldn't change the smart folder." });
      return null;
    }
  }

  async function deleteSmartFolder(id) {
    try {
      await api.deleteSmartFolder(id);
    } catch (error) {
      if (error?.status !== 404) {
        showToast({ text: error.message || "Couldn't delete the smart folder." });
        return;
      }
    }
    if (state.view.kind === "smart" && state.view.smartId === id) setView({ kind: "all" });
    await loadSidebar();
  }

  // -------------------------------------------------------- tags across notes --

  /**
   * Rename a tag (`name`) or delete it (null) in every note. Everything typed is
   * saved first, so the rewrite starts from it; a note still being typed into
   * then meets a conflict on its next save, and both versions are kept.
   */
  async function retag(tag, name) {
    captureSession();
    try {
      await saver?.flushAll({ strict: true });
    } catch {
      /* the journal still has it; a conflict will keep both versions */
    }
    let changed;
    try {
      changed = (name ? await api.renameNoteTag(tag, name) : await api.deleteNoteTag(tag)).notes ?? [];
    } catch (error) {
      showToast({ text: error.message || (name ? "Couldn't rename the tag." : "Couldn't delete the tag.") });
      return false;
    }
    for (const { id } of changed) cache.delete(id);
    const session = state.session;
    if (session?.id && changed.some((note) => note.id === session.id) && !saver?.isDirty(session.key)) {
      await reloadSession().catch(() => {});
    }
    const view = state.view;
    if (view.kind === "tags" && view.tags.includes(tag)) {
      const tags = [...new Set(view.tags.map((each) => (each === tag ? name : each)).filter(Boolean))];
      setView(tags.length ? { kind: "tags", tags, tagsMode: view.tagsMode } : { kind: "all" });
    } else {
      refreshList();
    }
    await loadSidebar();
    const count = changed.length;
    showToast({ text: `${name ? `Renamed #${tag} to #${name}` : `Deleted #${tag}`} in ${count} note${count === 1 ? "" : "s"}.` });
    return true;
  }

  // ---------------------------------------------------------------- settings --

  function updateSettings(fields) {
    if (!settingsWriter) return;
    settingsWriter.set(fields, Object.fromEntries(Object.keys(fields).map((key) => [key, state.settings[key]])));
    if ("sort_by" in fields || "sort_order" in fields) refreshList();
  }

  // ------------------------------------------------------- versions/conflicts --

  async function reloadSession() {
    const session = state.session;
    if (!session?.id) return;
    cache.delete(session.id);
    const { note } = await api.note(session.id, { includeDeleted: true });
    remember(note);
    upsertItem(note);
    showSession(note);
  }

  async function restoreVersion(versionId) {
    const session = state.session;
    if (!session?.id) return;
    if (saver?.isTracked(session.key)) {
      saver.capture(session.key);
      await saver.flush(session.key, { strict: true }).catch(() => {});
    }
    try {
      const { note } = await api.note(session.id, { includeDeleted: true });
      const restored = await api.restoreNoteVersion(session.id, versionId, {
        base_revision: note.revision,
        write_id: `restore:${versionId}:${note.revision}`,
      });
      await saver?.discard(session.key);
      remember(restored.note);
      upsertItem(restored.note);
      showSession(restored.note);
      showToast({ text: "Version restored. The one it replaced is in Versions." });
    } catch (error) {
      showToast({ text: error.message || "Couldn't restore that version." });
    }
  }

  /** Settle a blocked note. `choice`: keep-mine | keep-theirs | keep-both | restore-and-keep | new-note. */
  async function resolve(choice) {
    const session = state.session;
    if (!session) return;
    const key = session.key;
    if (session.blocked === "gone" || choice === "new-note") {
      const snapshot = projectNoteForSave(key);
      const created = await api.createNote({
        ...snapshot,
        folder_id: session.note?.folder_id ?? null,
        write_id: `create:recovered-${key}-${Date.now()}`,
      });
      await saver.discard(key);
      upsertItem(created.note);
      await openNote(created.note.id, { quiet: true });
      return;
    }
    const conflicts = session.id ? await api.noteConflicts(session.id) : [];
    if (choice === "restore-and-keep") {
      await api.restoreNote(session.id);
      choice = "keep-mine";
    }
    if (choice === "keep-mine") {
      const latest = conflicts.at(-1);
      let note = (await api.note(session.id, { includeDeleted: true })).note;
      for (const conflict of conflicts.slice(0, -1)) {
        note = (await api.resolveNoteConflict(session.id, conflict.id, { resolution: "use_other" })).note;
      }
      if (latest) {
        note = (
          await api.resolveNoteConflict(session.id, latest.id, {
            resolution: "keep_mine",
            base_revision: note.revision,
            write_id: `resolve:${latest.id}`,
          })
        ).note;
      }
      saver.resume(key, note);
      upsertItem(note);
      set((cur) => ({ session: { ...cur.session, blocked: null, blockedDetail: null, readOnly: false, note: { ...cur.session.note, ...note } } }));
      return;
    }
    if (choice === "keep-both") {
      const latest = conflicts.at(-1);
      for (const conflict of conflicts.slice(0, -1)) {
        await api.resolveNoteConflict(session.id, conflict.id, { resolution: "use_other" });
      }
      if (latest) {
        const copy = (await api.resolveNoteConflict(session.id, latest.id, { resolution: "save_copy" })).note;
        upsertItem(copy);
      }
    } else {
      for (const conflict of conflicts) {
        await api.resolveNoteConflict(session.id, conflict.id, { resolution: "use_other" });
      }
    }
    await saver.discard(key);
    await reloadSession();
  }

  function projectNoteForSave(key) {
    const snapshot = saver?.latestSnapshot(key);
    const doc = snapshot?.doc ?? state.session?.doc ?? newNoteDoc("title");
    return { ...projectDoc(doc), doc };
  }

  /** "Edit anyway" on a note whose Markdown the editor can't hold exactly. */
  function editAnyway() {
    set((cur) => ({ session: cur.session ? { ...cur.session, readOnly: Boolean(cur.session.note?.deleted), lossyAccepted: true } : cur.session }));
  }

  // ------------------------------------------------------- locking actions --

  function lockRefusal() {
    if (!state.lock.available) return "Locking needs a secure connection (HTTPS, or Neo on this computer).";
    if (state.lock.guest) return "Guest sessions have no password, so they can't lock notes. Create a profile to lock notes.";
    return null;
  }

  /** Ask for the profile password, then carry on with `then` ("lock", "open" or "remove"). */
  function askPassword(then) {
    set({ lockPrompt: { mode: state.lock.setUp ? "unlock" : "setup", then, error: "" } });
  }

  function cancelLockPrompt() {
    set({ lockPrompt: null });
  }

  /** The password from the prompt: open (or make) the keys, then do what was asked. */
  async function submitLockPassword(password, earlierPassword = null) {
    const prompt = state.lockPrompt;
    if (!prompt) return;
    set({ lockPrompt: { ...prompt, busy: true, error: "" } });
    try {
      if (prompt.mode === "earlier") await keys.recover(password, earlierPassword);
      else if (prompt.mode === "setup") await keys.setup(password);
      else await keys.unlock(password);
      syncLockState({ setUp: true });
    } catch (error) {
      const message = error?.detail?.message || (typeof error?.detail === "string" ? error.detail : null) || error?.message || "Couldn't unlock.";
      set({ lockPrompt: { ...prompt, busy: false, error: message } });
      return;
    }
    set({ lockPrompt: null });
    const session = state.session;
    if (prompt.then === "lock") await lockCurrent();
    else if (prompt.then === "remove") await removeLockCurrent();
    else if (prompt.then === "keep-edit") await keepLockedEdit();
    else if (session?.format === "encrypted") {
      if (keys.keyFor(session.note.lock_key_version)) revealLocked(session);
      else set({ lockPrompt: { mode: "earlier", then: "open", error: "" } });
    }
  }

  /** "View Note" on a locked note. */
  function openLocked() {
    const session = state.session;
    if (!session || session.format !== "encrypted") return;
    if (keys.keyFor(session.note.lock_key_version)) revealLocked(session);
    else if (keys.isUnlocked() && state.lock.unavailable.includes(session.note.lock_key_version)) set({ lockPrompt: { mode: "earlier", then: "open", error: "" } });
    else askPassword("open");
  }

  /** Save what this tab has for the open note, and make sure nothing is left unsent. */
  async function settleForTransition(session) {
    const { LockError } = await loadLock();
    captureSession();
    if (saver?.isTracked(session.key)) await saver.flush(session.key, { strict: true });
    if (saver?.isDirty(session.key)) throw new LockError("This note has changes that haven't reached Neo yet. Try again when you're connected.");
    if (session.blocked) throw new LockError("Settle this note's conflict first.");
    // Other tabs with this note open save theirs too.
    channel?.postMessage({ type: "flush", id: session.id, tabId });
    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  async function lockCurrent() {
    const session = state.session;
    if (!session?.id || session.format === "encrypted" || state.lock.busy) return;
    const refusal = lockRefusal();
    if (refusal) return showToast({ text: refusal });
    const version = keys.currentVersion();
    if (version == null || !keys.keyFor(version)) return askPassword("lock");
    set((current) => ({ lock: { ...current.lock, busy: "Locking…" } }));
    try {
      await settleForTransition(session);
      const { lockNote } = await loadLock();
      const { note: fresh } = await api.note(session.id, { includeDeleted: true });
      const note = await lockNote({ api, noteId: session.id, baseRevision: fresh.revision, key: keys.keyFor(version), keyVersion: version });
      lockVersions.set(note.id, version);
      if (saver?.isTracked(session.key)) {
        saver.setFormat(session.key, "encrypted", note.revision);
        saver.forgetPlaintext(session.key);
      }
      remember(note);
      upsertItem(note);
      if (state.session?.id === note.id) showSession(note);
      channel?.postMessage({ type: "saved", id: note.id, revision: note.revision, tabId });
      showToast({ text: "Note locked." });
    } catch (error) {
      showToast({ text: `Couldn't lock this note. ${error?.message || ""}`.trim() });
      if (error?.code === "unknown" && state.session?.id === session.id) reloadSession().catch(() => {});
    } finally {
      set((current) => ({ lock: { ...current.lock, busy: null } }));
    }
  }

  /** Remove Lock (confirmed in the UI): the only way a locked note is stored as plaintext again. */
  async function removeLockCurrent() {
    const session = state.session;
    if (!session?.id || session.format !== "encrypted" || state.lock.busy) return;
    if (!keys.keyFor(session.note.lock_key_version)) return askPassword("remove");
    set((current) => ({ lock: { ...current.lock, busy: "Removing the lock…" } }));
    try {
      await settleForTransition(session);
      const { removeLock } = await loadLock();
      const { note: fresh } = await api.note(session.id, { includeDeleted: true });
      const note = await removeLock({ api, noteId: session.id, baseRevision: fresh.revision, keyFor: (v) => keys.keyFor(v), project: projectDoc });
      lockVersions.delete(note.id);
      if (saver?.isTracked(session.key)) saver.setFormat(session.key, "rich", note.revision);
      remember(note);
      upsertItem(note);
      if (state.session?.id === note.id) showSession(note);
      channel?.postMessage({ type: "saved", id: note.id, revision: note.revision, tabId });
      showToast({ text: "Lock removed." });
    } catch (error) {
      showToast({ text: `Couldn't remove the lock. ${error?.message || ""}`.trim() });
      if (error?.code === "unknown" && state.session?.id === session.id) reloadSession().catch(() => {});
    } finally {
      set((current) => ({ lock: { ...current.lock, busy: null } }));
    }
  }

  /** A picture or file added to an open locked note: encrypted here, stored as opaque bytes. */
  async function uploadEncrypted(file) {
    const session = state.session;
    const version = session?.id ? lockVersions.get(session.id) : null;
    const key = version == null ? null : keys.keyFor(version);
    if (!key) throw new Error("The note locked. Unlock it to add attachments.");
    const bytes = new Uint8Array(await file.arrayBuffer());
    const aid = newAid();
    const { attachment } = await api.uploadEncryptedNoteAttachment(new Blob([await sealFile(key, aid, bytes)]));
    const mime = file.type || "application/octet-stream";
    blobUrls.set(attachment.id, URL.createObjectURL(new Blob([bytes], { type: mime })));
    return {
      attachment: { id: attachment.id, kind: mime.startsWith("image/") ? "image" : "file", name: file.name || "attachment", size: bytes.length, mime_type: mime, url: attachment.url, aid },
    };
  }

  /** A locked note's attachment, decrypted in memory, as a blob URL (revoked when the note locks). */
  async function resolveFile(attrs) {
    if (blobUrls.has(attrs.fileId)) return blobUrls.get(attrs.fileId);
    const session = state.session;
    const version = session?.id ? lockVersions.get(session.id) : null;
    const key = version == null ? null : keys.keyFor(version);
    if (!key) throw new Error("locked");
    const bytes = await openFile(key, attrs.aid, await api.fileBytes(attrs.fileId));
    if (!keys.isUnlocked()) throw new Error("locked");
    const url = URL.createObjectURL(new Blob([bytes], { type: attrs.mime || "" }));
    blobUrls.set(attrs.fileId, url);
    return url;
  }

  /**
   * Another tab locked this note while this one had plaintext edits: the
   * server refused them. Unlocked, they are encrypted and kept as a conflict.
   */
  async function keepLockedEdit() {
    const session = state.session;
    if (!session || session.blocked !== "locked") return;
    const { note } = await api.note(session.id, { includeDeleted: true });
    lockVersions.set(note.id, note.lock_key_version);
    if (!keys.keyFor(note.lock_key_version)) return askPassword("keep-edit");
    const snapshot = saver.latestSnapshot(session.key);
    const doc = snapshot?.doc ?? session.doc;
    const sealed = await sealSnapshot(note.id, { doc, summary: summaries.get(session.key) ?? null });
    try {
      await api.writeNoteContent(note.id, { ...sealed, format: "encrypted", base_revision: saver.revision(session.key) ?? 0, write_id: `${tabId}:locked-edit:${Date.now()}` });
    } catch (error) {
      if (error?.status !== 409) throw error;
    }
    await saver.discard(session.key);
    await reloadSession();
  }

  function ensureReady() {
    if (!readyPromise) readyPromise = start();
    return readyPromise;
  }

  async function dispose() {
    captureSession();
    // A profile switch or sign-out locks: unsaved locked content is sealed first.
    await keys.dispose();
    disposed = true;
    try {
      await saver?.persistAll();
      await saver?.flushAll({ strict: true });
    } catch {
      /* the journal keeps whatever could not be sent */
    }
    channel?.close();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("beforeunload", onBeforeUnload);
      window.removeEventListener("online", onOnline);
      document.removeEventListener("freeze", onPageHide);
    }
    releaseLock();
    await journal?.close();
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getState: () => state,
    ensureReady,
    refreshList,
    loadSidebar,
    setView,
    setSearch,
    openNote,
    revealNote,
    closeSession,
    openDraft,
    newNote,
    attachEditor,
    edit,
    setSummary,
    liveRowChanged,
    leaveSession,
    captureSession,
    pin,
    archive,
    move,
    trash,
    restore,
    deleteForever,
    emptyTrash,
    duplicate,
    createFolder,
    renameFolder,
    moveFolder,
    deleteFolder,
    createSmartFolder,
    updateSmartFolder,
    deleteSmartFolder,
    renameTag: (tag, name) => retag(tag, name),
    deleteTag: (tag) => retag(tag, null),
    updateSettings,
    restoreVersion,
    resolve,
    editAnyway,
    reloadSession,
    dismissToast: () => set({ toast: null }),
    noteTitleOf,
    searchNoteTitles,
    subscribeTitles(listener) {
      titleListeners.add(listener);
      return () => titleListeners.delete(listener);
    },
    /** A short message at the bottom of Notes (an attachment that could not be added, say). */
    notify: (text) => showToast({ text }),
    /** The open note's document as the editor has it now (for counts in Info). */
    currentDoc() {
      const take = editorTakes.get(state.session?.key);
      return take ? take().doc : state.session?.doc ?? null;
    },
    captureKey(key) {
      if (key && saver?.isTracked(key)) saver.capture(key);
    },
    flushCurrent() {
      const key = state.session?.key;
      if (key && saver?.isTracked(key)) return saver.flush(key);
      return Promise.resolve();
    },
    statusOf: (key) => state.saveStatus[key] ?? "clean",
    lockNote: lockCurrent,
    removeLock: removeLockCurrent,
    lockNow: () => keys.lock("now"),
    openLocked,
    submitLockPassword,
    cancelLockPrompt,
    keepLockedEdit,
    uploadEncrypted,
    resolveFile,
    /** Input in Notes: keeps the keys open (15 idle minutes lock them). */
    touchKeys: () => keys.touch(),
    dispose,
  };
}

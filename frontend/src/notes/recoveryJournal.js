/**
 * Unsaved note content, kept on this device until the server has it.
 *
 * The network is not a promise: a tab can be killed, a laptop can sleep, the
 * backend can be down, `pagehide` may never fire. So every change is written here
 * first, and only what is here is ever sent. An entry is removed only when the
 * server has acknowledged a write at least as new as it.
 *
 * The rules that make this safe:
 *   - One database per profile, so profiles never see each other's drafts.
 *   - Entries are keyed by note *and* tab: two tabs editing one note each keep
 *     their own slot and can never overwrite each other.
 *   - Each write is one transaction that bumps the note's counter and stores the
 *     entry with the new number. Numbers therefore only rise, per note, across
 *     tabs and reloads.
 *   - Acknowledging is compare-and-delete in one transaction: the entry goes only
 *     if its number is not newer than the one acknowledged. A late or repeated
 *     acknowledgement can never remove newer content.
 *   - A tab holds a Web Lock for its lifetime. A slot whose lock is free belongs to
 *     a tab that is gone; that slot is what recovery replays.
 *
 * Writes during typing use relaxed durability; writes on lifecycle events (tab
 * hidden, page hide, note switch, lock) ask for strict durability. Losing at most
 * a quarter second of typing to a hard crash is the target, not a guarantee.
 */

const STORE_ENTRIES = "entries";
const STORE_COUNTERS = "counters";
const TAB_LOCK_PREFIX = "neo-notes-tab:";

export function journalName(profileId) {
  return `neo-notes-recovery:${profileId}`;
}

export function newTabId() {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `tab-${random}`;
}

/** The adapter contract, in memory. Used by tests, and when IndexedDB is unavailable. */
export function memoryJournalAdapter() {
  const entries = new Map();
  const counters = new Map();
  const slot = (noteKey, tabId) => `${noteKey}\u0000${tabId}`;
  return {
    kind: "memory",
    async write(noteKey, tabId, data) {
      const seq = (counters.get(noteKey) ?? 0) + 1;
      counters.set(noteKey, seq);
      entries.set(slot(noteKey, tabId), structuredClone({ ...data, noteKey, tabId, seq, updatedAt: Date.now() }));
      return seq;
    },
    async ack(noteKey, tabId, seq) {
      const key = slot(noteKey, tabId);
      const entry = entries.get(key);
      if (entry && entry.seq <= seq) {
        entries.delete(key);
        return true;
      }
      return false;
    },
    async list() {
      return [...entries.values()].map((entry) => structuredClone(entry));
    },
    async remove(noteKey, tabId, seq = Infinity) {
      return this.ack(noteKey, tabId, seq);
    },
    async close() {},
  };
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** The adapter contract over IndexedDB. Resolves to null when IndexedDB cannot be opened. */
export async function indexedDbJournalAdapter(profileId, idb = globalThis.indexedDB) {
  if (!idb) return null;
  let db;
  try {
    db = await new Promise((resolve, reject) => {
      const request = idb.open(journalName(profileId), 1);
      request.onupgradeneeded = () => {
        const upgrade = request.result;
        if (!upgrade.objectStoreNames.contains(STORE_ENTRIES)) {
          upgrade.createObjectStore(STORE_ENTRIES, { keyPath: ["noteKey", "tabId"] });
        }
        if (!upgrade.objectStoreNames.contains(STORE_COUNTERS)) {
          upgrade.createObjectStore(STORE_COUNTERS, { keyPath: "noteKey" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("journal_blocked"));
    });
  } catch {
    return null;
  }

  function transaction(stores, mode, strict) {
    // Browsers without the durability option ignore the third argument.
    return db.transaction(stores, mode, { durability: strict ? "strict" : "relaxed" });
  }

  function done(tx, value) {
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve(typeof value === "function" ? value() : value);
      tx.onabort = () => reject(tx.error ?? new Error("journal_transaction_aborted"));
      tx.onerror = () => reject(tx.error ?? new Error("journal_transaction_failed"));
    });
  }

  return {
    kind: "indexeddb",
    write(noteKey, tabId, data, { strict = false } = {}) {
      const tx = transaction([STORE_ENTRIES, STORE_COUNTERS], "readwrite", strict);
      let seq = 0;
      const counters = tx.objectStore(STORE_COUNTERS);
      const read = counters.get(noteKey);
      read.onsuccess = () => {
        seq = (read.result?.seq ?? 0) + 1;
        counters.put({ noteKey, seq });
        tx.objectStore(STORE_ENTRIES).put({ ...data, noteKey, tabId, seq, updatedAt: Date.now() });
      };
      return done(tx, () => seq);
    },
    ack(noteKey, tabId, seq, { strict = false } = {}) {
      const tx = transaction([STORE_ENTRIES], "readwrite", strict);
      const store = tx.objectStore(STORE_ENTRIES);
      let removed = false;
      const read = store.get([noteKey, tabId]);
      read.onsuccess = () => {
        if (read.result && read.result.seq <= seq) {
          store.delete([noteKey, tabId]);
          removed = true;
        }
      };
      return done(tx, () => removed);
    },
    async list() {
      const tx = transaction([STORE_ENTRIES], "readonly", false);
      const all = await requestResult(tx.objectStore(STORE_ENTRIES).getAll());
      return all ?? [];
    },
    remove(noteKey, tabId, seq = Number.MAX_SAFE_INTEGER) {
      return this.ack(noteKey, tabId, seq, { strict: true });
    },
    async close() {
      db.close();
    },
  };
}

/** Delete a profile's journal (when the profile itself is deleted). */
export function deleteJournal(profileId, idb = globalThis.indexedDB, timeoutMs = 5000) {
  if (!idb) return Promise.resolve(false);
  return new Promise((resolve) => {
    const request = idb.deleteDatabase(journalName(profileId));
    // "blocked" is a notice, not an outcome: the delete goes ahead once open
    // connections finish closing (Safari reports it even for just-closed ones).
    const timer = setTimeout(() => resolve(false), timeoutMs);
    request.onsuccess = () => {
      clearTimeout(timer);
      resolve(true);
    };
    request.onerror = () => {
      clearTimeout(timer);
      resolve(false);
    };
  });
}

/** Hold this tab's lock until the page goes away. Returns a release function. */
export function holdTabLock(tabId, locks = globalThis.navigator?.locks) {
  if (!locks?.request) return () => {};
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  locks.request(`${TAB_LOCK_PREFIX}${tabId}`, () => held).catch(() => {});
  return () => release?.();
}

/** Tabs alive right now, by their held locks; null when the browser cannot tell. */
export async function liveTabIds(locks = globalThis.navigator?.locks) {
  if (!locks?.query) return null;
  try {
    const { held = [] } = await locks.query();
    return new Set(
      held
        .map((lock) => lock.name)
        .filter((name) => name?.startsWith(TAB_LOCK_PREFIX))
        .map((name) => name.slice(TAB_LOCK_PREFIX.length)),
    );
  } catch {
    return null;
  }
}

const ORPHAN_AGE_WITHOUT_LOCKS = 24 * 60 * 60 * 1000;

export function createJournal(adapter, { tabId }) {
  return {
    tabId,
    kind: adapter.kind,
    write(noteKey, data, options) {
      return adapter.write(noteKey, tabId, data, options);
    },
    ack(noteKey, seq, options) {
      return adapter.ack(noteKey, tabId, seq, options);
    },
    entries() {
      return adapter.list();
    },
    /** Slots to replay: this tab's own, and those whose tab is gone. */
    async recoverable(live, now = Date.now()) {
      const all = await adapter.list();
      return all
        .filter((entry) => {
          if (entry.tabId === tabId) return true;
          if (live) return !live.has(entry.tabId);
          // Without Web Locks, only slots untouched for a day count as abandoned.
          return now - (entry.updatedAt ?? 0) > ORPHAN_AGE_WITHOUT_LOCKS;
        })
        .sort((a, b) => a.seq - b.seq);
    },
    remove(noteKey, slotTabId, seq) {
      return adapter.remove(noteKey, slotTabId, seq);
    },
    close() {
      return adapter.close();
    },
  };
}

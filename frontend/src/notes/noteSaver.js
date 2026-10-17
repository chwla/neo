/**
 * Saving notes: one queue per note, never more than one write in flight.
 *
 * Typing produces edits; edits produce snapshots only when needed (a quarter
 * second after typing for the local journal, 600 ms after typing -- or 3 s into a
 * long burst -- for the server). Everything sent has been written to the journal
 * first, so whatever happens to the request, the content is on this device.
 *
 * What a reply may and may not do:
 *   - It updates the note's revision and the list row. It never touches the
 *     editor: the person may have typed since it was sent.
 *   - It acknowledges exactly the journal entry it carried. Newer entries stay.
 *   - A conflict, a deleted or purged note, a locked note or a refused write stops
 *     this note's queue (`blocked`) until the person decides; the content stays in
 *     the journal and, for conflicts, on the server too.
 *   - Anything else (offline, server error) is retried with backoff.
 *
 * A note created here is a draft until its first save. Its create carries a fixed
 * write id, so a create retried after a lost reply returns the note it already
 * made; if that replayed create predates newer edits, the newer content follows
 * as an ordinary write.
 *
 * A locked note (format "encrypted") is sealed before anything keeps it: `seal`
 * turns the editor's snapshot into ciphertext and that, never the plaintext, is
 * what the journal stores and the server receives. When it can't be sealed (the
 * keys are gone), the edit is not kept anywhere new: there is no fallback to
 * plaintext.
 */

export const SAVE_DELAYS = {
  journal: 250,
  save: 600,
  maxWait: 3000,
  retry: [1000, 2000, 5000, 10000, 30000],
};

function errorKind(error) {
  const status = error?.status;
  const code = error?.detail?.code;
  if (status === 409 && code === "locked") return "locked";
  if (status === 409) return "conflict";
  if (status === 410) return "gone";
  if (status === 404) return "gone";
  if (status === 400 || status === 413 || status === 422) return "invalid";
  if (!status) return "offline";
  return "error";
}

/**
 * The payload for a snapshot. A snapshot from the editor carries only its
 * document: turning that into a title, Markdown body and tags costs a full
 * serialisation, so it is done once per send (`project`) rather than on every
 * journal write. Snapshots that already carry a body are sent as they are.
 */
export function payloadOf(snapshot, project) {
  if (snapshot.cipher) {
    // Sealed: the title (plaintext, as the list shows it) and the ciphertext only.
    return {
      title: snapshot.title ?? "",
      cipher: snapshot.cipher,
      lock_key_version: snapshot.lock_key_version,
      attachment_ids: snapshot.attachment_ids ?? [],
      ...(snapshot.keep_original ? { keep_original: true } : {}),
    };
  }
  const full = typeof snapshot.body !== "string" && project && snapshot.doc ? { ...project(snapshot.doc), ...snapshot } : snapshot;
  return {
    title: full.title ?? "",
    body: full.body ?? "",
    doc: full.doc,
    tags: full.tags ?? [],
    summary: full.summary ?? null,
  };
}

export function createNoteSaver({
  transport,
  journal = null,
  tabId,
  onEvent = () => {},
  timers = globalThis,
  delays = SAVE_DELAYS,
  now = () => Date.now(),
  project = null,
  seal = null,
}) {
  const notes = new Map();

  function state(key) {
    const s = notes.get(key);
    if (!s) throw new Error(`note ${key} is not tracked`);
    return s;
  }

  function setStatus(s, status, extra = {}) {
    if (s.status === status && !Object.keys(extra).length) return;
    s.status = status;
    onEvent({ type: "status", key: s.key, status, ...extra });
  }

  function clear(s, name) {
    if (s[name]) {
      timers.clearTimeout(s[name]);
      s[name] = null;
    }
  }

  function snapshotOf(s) {
    return s.frozen ?? s.take?.() ?? null;
  }

  /** Write the newest edit to the journal (serialised per note). Resolves when stored. */
  function persistLocal(s, { strict = false } = {}) {
    s.localChain = s.localChain.then(async () => {
      if (s.editVersion === s.journaledVersion) return;
      const version = s.editVersion;
      let snapshot = snapshotOf(s);
      if (!snapshot) return;
      if (s.format === "encrypted" && !snapshot.cipher) {
        try {
          if (!seal) throw new Error("no way to encrypt this note");
          snapshot = await seal(s.serverId, snapshot);
        } catch (error) {
          // Never kept as plaintext: this edit stays only in the editor.
          onEvent({ type: "seal-error", key: s.key, error });
          return;
        }
        // A newer edit may have arrived while sealing; it gets its own turn.
      }
      let seq;
      if (journal) {
        try {
          seq = await journal.write(
            s.key,
            {
              serverId: s.serverId,
              folderId: s.folderId,
              format: s.format,
              baseRevision: s.revision,
              snapshot,
            },
            { strict },
          );
          s.journalFailed = false;
        } catch (error) {
          // Storage full or unavailable: say so, and keep going from memory.
          s.journalFailed = true;
          onEvent({ type: "journal-error", key: s.key, error });
          seq = (s.journaledSeq ?? 0) + 1;
        }
      } else {
        seq = (s.journaledSeq ?? 0) + 1;
      }
      s.journaledVersion = version;
      s.journaledSeq = seq;
      s.lastSnapshot = snapshot;
    });
    return s.localChain;
  }

  function scheduleSave(s, delay) {
    clear(s, "saveTimer");
    s.saveTimer = timers.setTimeout(() => {
      s.saveTimer = null;
      pump(s);
    }, delay);
  }

  function retryLater(s) {
    const wait = delays.retry[Math.min(s.retries, delays.retry.length - 1)];
    s.retries += 1;
    clear(s, "retryTimer");
    s.retryTimer = timers.setTimeout(() => {
      s.retryTimer = null;
      pump(s);
    }, wait);
  }

  async function send(s, version, seq, snapshot) {
    const payload = payloadOf(snapshot, project);
    if (!s.serverId) {
      const { note, replayed } = await transport.create({
        ...payload,
        folder_id: s.folderId ?? null,
        write_id: `create:${s.key}`,
      });
      s.serverId = note.id;
      s.revision = note.revision;
      onEvent({ type: "created", key: s.key, note });
      if (!replayed) return note;
      // The note existed already, as an earlier create made it; this content follows.
    }
    const { note } = await transport.write(s.serverId, {
      ...payload,
      base_revision: s.revision,
      write_id: `${tabId}:${s.key}:${seq}`,
      format: payload.cipher ? "encrypted" : "rich",
    });
    return note;
  }

  async function pump(s) {
    if (s.inFlight || s.blocked || s.disposed) return;
    clear(s, "saveTimer");
    clear(s, "maxTimer");
    await persistLocal(s);
    if (s.inFlight || s.blocked || s.disposed) return;
    if (s.journaledVersion === s.ackedVersion) {
      if (s.editVersion === s.ackedVersion) {
        s.firstDirtyAt = null;
        setStatus(s, "clean");
      }
      return;
    }
    const version = s.journaledVersion;
    const seq = s.journaledSeq;
    const snapshot = s.lastSnapshot;
    if (!snapshot) return;
    setStatus(s, "saving");
    s.inFlight = (async () => {
      try {
        const note = await send(s, version, seq, snapshot);
        s.revision = note.revision;
        s.ackedVersion = Math.max(s.ackedVersion, version);
        s.retries = 0;
        if (journal && !s.journalFailed) {
          try {
            await journal.ack(s.key, seq);
          } catch {
            /* the entry is replayed later; its write id makes that harmless */
          }
        }
        onEvent({ type: "saved", key: s.key, note });
      } catch (error) {
        const kind = errorKind(error);
        if (kind === "offline" || kind === "error") {
          setStatus(s, kind, { error });
          retryLater(s);
        } else {
          s.blocked = kind;
          setStatus(s, kind, { error, detail: error?.detail });
        }
      } finally {
        s.inFlight = null;
      }
      if (!s.blocked && !s.retryTimer && !s.disposed) {
        if (s.editVersion > s.ackedVersion) scheduleSave(s, 0);
        else {
          s.firstDirtyAt = null;
          setStatus(s, "clean");
        }
      }
    })();
    return s.inFlight;
  }

  return {
    /** Start tracking a note: an existing one (serverId) or a draft (no serverId yet). */
    track(key, { serverId = null, revision = 0, folderId = null, format = "rich" } = {}) {
      if (notes.has(key)) return;
      notes.set(key, {
        key,
        serverId,
        revision,
        folderId,
        format,
        take: null,
        frozen: null,
        editVersion: 0,
        journaledVersion: 0,
        journaledSeq: 0,
        ackedVersion: 0,
        lastSnapshot: null,
        localChain: Promise.resolve(),
        inFlight: null,
        blocked: null,
        retries: 0,
        status: "clean",
        firstDirtyAt: null,
        saveTimer: null,
        maxTimer: null,
        journalTimer: null,
        retryTimer: null,
        disposed: false,
      });
    },

    isTracked(key) {
      return notes.has(key);
    },

    /** The content changed. `take` builds a snapshot of the current content on demand. */
    edit(key, take) {
      const s = state(key);
      s.take = take;
      s.frozen = null;
      s.editVersion += 1;
      // Typing is always kept on this device, even while the note is blocked
      // (a conflict being decided, say); only sending waits.
      clear(s, "journalTimer");
      s.journalTimer = timers.setTimeout(() => {
        s.journalTimer = null;
        persistLocal(s);
      }, delays.journal);
      if (s.blocked) return;
      if (!s.firstDirtyAt) s.firstDirtyAt = now();
      if (s.status === "clean") setStatus(s, "pending");
      if (!s.inFlight) {
        scheduleSave(s, delays.save);
        if (!s.maxTimer) {
          s.maxTimer = timers.setTimeout(() => {
            s.maxTimer = null;
            pump(s);
          }, delays.maxWait);
        }
      }
    },

    /** The editor is leaving this note: freeze its last content so it no longer needs the editor. */
    capture(key) {
      const s = notes.get(key);
      if (!s?.take) return;
      // After this the editor shows another note; a live `take` would read that
      // note's content into this one, so it is replaced by a frozen copy.
      s.frozen = s.take();
      s.take = null;
    },

    /** Write locally now and send now. */
    async flush(key, { strict = true } = {}) {
      const s = notes.get(key);
      if (!s) return;
      clear(s, "journalTimer");
      await persistLocal(s, { strict });
      clear(s, "retryTimer");
      await pump(s);
    },

    async flushAll(options) {
      await Promise.all([...notes.keys()].map((key) => this.flush(key, options)));
    },

    /** Write every note's newest content to the journal, without the network (page is going away). */
    persistAll() {
      return Promise.all(
        [...notes.values()].map((s) => {
          clear(s, "journalTimer");
          return persistLocal(s, { strict: true });
        }),
      );
    },

    /** The newest content of a note as the saver knows it (frozen, live, or last journaled). */
    latestSnapshot(key) {
      const s = notes.get(key);
      if (!s) return null;
      return s.frozen ?? s.take?.() ?? s.lastSnapshot ?? null;
    },

    status(key) {
      return notes.get(key)?.status ?? "clean";
    },
    revision(key) {
      return notes.get(key)?.revision;
    },
    serverId(key) {
      return notes.get(key)?.serverId ?? null;
    },
    isDirty(key) {
      const s = notes.get(key);
      return Boolean(s && s.editVersion > s.ackedVersion);
    },
    hasUnsaved() {
      return [...notes.values()].some((s) => s.editVersion > s.ackedVersion);
    },

    /**
     * The note changed form on the server (locked, or its lock removed): later
     * edits are kept and sent in the new form, from its new revision.
     */
    setFormat(key, format, revision) {
      const s = notes.get(key);
      if (!s) return;
      s.format = format;
      if (typeof revision === "number") s.revision = revision;
    },

    /**
     * Forget the plaintext a note's queue holds (its live reader and frozen
     * copy) once what it held is sealed into the journal: the note was locked.
     */
    forgetPlaintext(key) {
      const s = notes.get(key);
      if (!s) return;
      s.take = null;
      s.frozen = null;
      if (s.lastSnapshot && !s.lastSnapshot.cipher) s.lastSnapshot = null;
    },

    format: (key) => notes.get(key)?.format ?? null,
    trackedKeys: () => [...notes.keys()],

    /** After a conflict is settled on the server: continue from its revision. */
    resume(key, note) {
      const s = notes.get(key);
      if (!s) return;
      s.revision = note.revision;
      s.serverId = note.id;
      s.blocked = null;
      s.retries = 0;
      if (s.editVersion > s.ackedVersion) scheduleSave(s, 0);
      else setStatus(s, "clean");
    },

    /** Stop tracking, dropping local content (it was saved elsewhere or discarded). */
    async discard(key) {
      const s = notes.get(key);
      if (!s) return;
      s.disposed = true;
      for (const name of ["saveTimer", "maxTimer", "journalTimer", "retryTimer"]) clear(s, name);
      notes.delete(key);
      if (journal && s.journaledSeq) {
        try {
          await journal.ack(key, s.journaledSeq);
        } catch {
          /* nothing more to do */
        }
      }
    },

    /** Stop tracking a clean note (it stays tracked while it has unsaved content). */
    release(key) {
      const s = notes.get(key);
      if (!s || s.editVersion > s.ackedVersion || s.inFlight) return false;
      s.disposed = true;
      for (const name of ["saveTimer", "maxTimer", "journalTimer", "retryTimer"]) clear(s, name);
      notes.delete(key);
      return true;
    },
  };
}

/**
 * Replay journal slots left by tabs that are gone (or by this tab before a
 * reload). Each goes through the same compare-and-swap as a live save; a write
 * that already landed is recognised by its write id.
 */
export async function replayJournal({ journal, transport, live, onEvent = () => {}, project = null }) {
  const outcomes = [];
  for (const entry of await journal.recoverable(live)) {
    const writeId = `${entry.tabId}:${entry.noteKey}:${entry.seq}`;
    const payload = payloadOf(entry.snapshot ?? {}, project);
    try {
      let serverId = entry.serverId;
      let base = entry.baseRevision ?? 0;
      let note;
      if (!serverId) {
        const created = await transport.create({
          ...payload,
          folder_id: entry.folderId ?? null,
          write_id: `create:${entry.noteKey}`,
        });
        note = created.note;
        serverId = note.id;
        base = note.revision;
        if (created.replayed) {
          note = (await transport.write(serverId, { ...payload, base_revision: base, write_id: writeId, format: payload.cipher ? "encrypted" : "rich" })).note;
        }
      } else {
        note = (await transport.write(serverId, { ...payload, base_revision: base, write_id: writeId, format: payload.cipher ? "encrypted" : "rich" })).note;
      }
      await journal.remove(entry.noteKey, entry.tabId, entry.seq);
      outcomes.push({ entry, result: "saved", note });
      onEvent({ type: "recovered", entry, note });
    } catch (error) {
      const kind = errorKind(error);
      if (kind === "conflict") {
        // The server kept it as a conflict on the note; the journal copy is no longer needed.
        await journal.remove(entry.noteKey, entry.tabId, entry.seq);
        outcomes.push({ entry, result: "conflict", detail: error.detail });
        onEvent({ type: "recovered-conflict", entry, detail: error.detail });
      } else if (kind === "offline" || kind === "error") {
        outcomes.push({ entry, result: "deferred", error });
        break;
      } else {
        outcomes.push({ entry, result: kind, detail: error?.detail });
        onEvent({ type: `recover-${kind}`, entry, detail: error?.detail });
      }
    }
  }
  return outcomes;
}

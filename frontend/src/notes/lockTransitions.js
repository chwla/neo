/**
 * Lock and Remove Lock, the browser's half of the two-phase transition
 * (app/services/notes/transitions.py has the server's).
 *
 * Prepare: read the note, every version and every conflict (resolved ones
 * too: they still hold its words); encrypt (or decrypt) each, and every
 * attachment any of them refers to. Each attachment is uploaded as a staged
 * file, downloaded again and checked against the original; each piece of
 * content is opened again and compared with what it was made from. Only then
 * commit. Anything that fails before the commit aborts it: the note is as it
 * was, and the error says why. After the commit the note is read back, and
 * the lock is reported only if the server holds exactly what was sent.
 *
 * Inside a locked note, each piece of content is sealed as one of
 *   { format: "rich", doc, summary }      (attachments: `fileId` and `aid` in the document)
 *   { format: "markdown", body, tags, summary, files: { [fileId]: aid } }
 * so an older Markdown note keeps its exact text through a lock and back.
 */
import { newAid, openContent, openFile, sealContent, sealFile, sha256Hex } from "./crypto.js";

const FILE_URL = /\/api\/files\/([0-9a-fA-F-]{36})\/download/g;
const fileUrl = (id) => `/api/files/${id}/download`;

export class LockError extends Error {
  constructor(message, { cause, code = "failed" } = {}) {
    super(message);
    this.code = code;
    if (cause) this.cause = cause;
  }
}

/** Files a document refers to, with their attachment ids when it has them. */
export function filesOfDoc(doc) {
  const out = new Map();
  const walk = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.type === "image" || node.type === "fileAttachment") {
      const attrs = node.attrs ?? {};
      let id = attrs.fileId;
      if (!id && node.type === "image") id = String(attrs.src ?? "").match(/\/api\/files\/([0-9a-fA-F-]{36})\/download/)?.[1];
      if (id) out.set(id, { aid: attrs.aid ?? null, name: attrs.name ?? attrs.alt ?? null, mime: attrs.mime ?? null, image: node.type === "image" });
    }
    for (const child of node.content ?? []) walk(child);
  };
  walk(doc);
  return out;
}

export function filesOfMarkdown(body, aids = {}) {
  const out = new Map();
  for (const match of String(body ?? "").matchAll(FILE_URL)) out.set(match[1], { aid: aids[match[1]] ?? null, name: null, mime: null, image: false });
  return out;
}

function filesOfForm(form) {
  return form.format === "markdown" ? filesOfMarkdown(form.body, form.files) : filesOfDoc(form.doc);
}

/** The document with each file swapped for its new one (and its attachment id, or none). */
export function remapDoc(doc, mapping) {
  const walk = (node) => {
    if (!node || typeof node !== "object") return node;
    let next = node;
    if (node.type === "image" || node.type === "fileAttachment") {
      const attrs = node.attrs ?? {};
      const id = attrs.fileId || (node.type === "image" ? String(attrs.src ?? "").match(/\/api\/files\/([0-9a-fA-F-]{36})\/download/)?.[1] : null);
      const target = id ? mapping.get(id) : null;
      if (target) {
        const swapped = { ...attrs, fileId: target.fileId, aid: target.aid ?? null };
        if (node.type === "image") swapped.src = fileUrl(target.fileId);
        next = { ...node, attrs: swapped };
      }
    }
    if (Array.isArray(next.content)) next = { ...next, content: next.content.map(walk) };
    return next;
  };
  return walk(doc);
}

export function remapMarkdown(body, mapping) {
  return String(body ?? "").replace(FILE_URL, (whole, id) => (mapping.has(id) ? fileUrl(mapping.get(id).fileId) : whole));
}

function remapForm(form, mapping) {
  if (form.format === "markdown") {
    const body = remapMarkdown(form.body, mapping);
    const files = {};
    for (const [, target] of mapping) if (target.aid && body.includes(target.fileId)) files[target.fileId] = target.aid;
    return { ...form, body, files };
  }
  return { ...form, doc: remapDoc(form.doc, mapping) };
}

/** A note, version or conflict as plaintext, in the form a locked note seals. */
function plainForm(item) {
  if (item.content_format === "markdown") {
    return { format: "markdown", body: item.body ?? "", tags: item.tags ?? [], summary: item.summary ?? null, files: {} };
  }
  if (item.content_format === "rich") return { format: "rich", doc: item.doc, summary: item.summary ?? null };
  throw new LockError("Part of this note is already encrypted.");
}

async function allOf(api, noteId) {
  const versions = await Promise.all((await api.noteVersions(noteId)).map((v) => api.noteVersion(noteId, v.id)));
  const conflicts = await api.noteConflicts(noteId, { includeResolved: true });
  return { versions, conflicts };
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Run one transition: begin, prepare (which returns the commit payload), commit.
 * A failure before the commit aborts it; a commit whose reply was lost is
 * retried (it returns the first result) before anything is concluded.
 */
async function runTransition(api, noteId, kind, baseRevision, prepare) {
  let transition;
  try {
    transition = await api.beginNoteTransition(noteId, { kind, base_revision: baseRevision });
  } catch (error) {
    if (error?.status === 409) throw new LockError("This note changed. Try again.", { cause: error, code: "changed" });
    throw new LockError(error?.message || "Couldn't start.", { cause: error });
  }
  let payload;
  try {
    payload = await prepare(transition.id);
  } catch (error) {
    await api.abortNoteTransition(noteId, transition.id).catch(() => {});
    throw error instanceof LockError ? error : new LockError(error?.message || "Couldn't prepare the note.", { cause: error });
  }
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const result = await api.commitNoteTransition(noteId, transition.id, payload);
      return { result, payload };
    } catch (error) {
      lastError = error;
      // The server answered: the change is void (it says why) and nothing changed.
      if (error?.status) {
        const changed = error.status === 409;
        throw new LockError(changed ? "This note changed while it was being prepared. Try again." : error.message, { cause: error, code: changed ? "changed" : "failed" });
      }
    }
  }
  // No answer at all: the commit may or may not have happened. Ask the note.
  throw new LockError("Neo couldn't be reached to finish. Check the note's state before trying again.", { cause: lastError, code: "unknown" });
}

/**
 * Lock a note: everything it holds encrypted with `key` (key version
 * `keyVersion`), its plaintext attachments replaced by encrypted copies.
 * Returns the locked note as the server now has it.
 */
export async function lockNote({ api, noteId, baseRevision, key, keyVersion, onStep = () => {} }) {
  let sentCipher = null;
  const { result } = await runTransition(api, noteId, "lock", baseRevision, async (transitionId) => {
    onStep("reading");
    const { note } = await api.note(noteId, { includeDeleted: true });
    if (note.revision !== baseRevision) throw new LockError("This note changed. Try again.", { code: "changed" });
    const { versions, conflicts } = await allOf(api, noteId);
    const pieces = [
      { slot: "content", id: noteId, title: note.title, form: plainForm(note) },
      ...versions.map((v) => ({ slot: "versions", id: v.id, title: v.title, form: plainForm(v) })),
      ...conflicts.map((c) => ({ slot: "conflicts", id: c.id, title: c.title, form: plainForm(c) })),
    ];

    onStep("attachments");
    const files = new Map();
    for (const piece of pieces) for (const [id, info] of filesOfForm(piece.form)) files.set(id, info);
    const mapping = new Map();
    for (const [fileId] of files) {
      let bytes;
      try {
        bytes = await api.fileBytes(fileId);
      } catch (error) {
        // A file that is already gone has no plaintext left to protect.
        if (error?.status === 404) continue;
        throw new LockError("An attachment couldn't be read.", { cause: error });
      }
      const original = await sha256Hex(bytes);
      const aid = newAid();
      const blob = await sealFile(key, aid, bytes);
      const staged = await api.stageNoteTransitionFile(noteId, transitionId, new Blob([blob]), "encrypted");
      if (staged.sha256 !== (await sha256Hex(blob))) throw new LockError("An attachment wasn't stored as sent.");
      // Downloaded again and opened: what was stored is what will be read.
      const back = await openFile(key, aid, await api.fileBytes(staged.file_id));
      if ((await sha256Hex(back)) !== original) throw new LockError("An encrypted attachment didn't open as the original.");
      mapping.set(fileId, { fileId: staged.file_id, aid });
    }

    onStep("encrypting");
    const payload = { content: null, versions: {}, conflicts: {} };
    for (const piece of pieces) {
      const form = remapForm(piece.form, mapping);
      const cipher = await sealContent(key, noteId, keyVersion, form);
      // Opened again and compared: what is stored is what will be read.
      if (!same(await openContent(key, noteId, keyVersion, cipher), form)) throw new LockError("Encrypted content didn't open as the original.");
      const sealed = { title: piece.title, cipher, lock_key_version: keyVersion, attachment_ids: [...filesOfForm(form).keys()].filter((id) => [...mapping.values()].some((m) => m.fileId === id)) };
      if (piece.slot === "content") payload.content = sealed;
      else payload[piece.slot][piece.id] = sealed;
    }
    sentCipher = payload.content.cipher;
    return payload;
  });
  // Read back: locked, and holding exactly what was sent.
  const { note } = await api.note(noteId, { includeDeleted: true });
  if (note.content_format !== "encrypted" || note.cipher?.ct !== sentCipher.ct || note.revision !== result.revision) {
    throw new LockError("The note's lock couldn't be confirmed. Reopen it to check.", { code: "unknown" });
  }
  return note;
}

/**
 * Remove a note's lock: everything decrypted and stored as plaintext again,
 * its encrypted attachments replaced by plain copies. `keyFor(version)` gives
 * the key for each piece; `project(doc)` the title, Markdown body and tags of a
 * document. Returns the note as the server now has it.
 */
export async function removeLock({ api, noteId, baseRevision, keyFor, project, onStep = () => {} }) {
  const { result } = await runTransition(api, noteId, "unlock", baseRevision, async (transitionId) => {
    onStep("reading");
    const { note } = await api.note(noteId, { includeDeleted: true });
    if (note.revision !== baseRevision) throw new LockError("This note changed. Try again.", { code: "changed" });
    const { versions, conflicts } = await allOf(api, noteId);
    const open = async (item) => {
      if (item.content_format !== "encrypted") return plainForm(item);
      const key = keyFor(item.lock_key_version);
      if (!key) throw new LockError("Part of this note needs an earlier password.", { code: "key" });
      return { ...(await openContent(key, noteId, item.lock_key_version, item.cipher)), keyVersion: item.lock_key_version };
    };
    const pieces = [
      { slot: "content", id: noteId, title: note.title, form: await open(note) },
      ...(await Promise.all(versions.map(async (v) => ({ slot: "versions", id: v.id, title: v.title, form: await open(v) })))),
      ...(await Promise.all(conflicts.map(async (c) => ({ slot: "conflicts", id: c.id, title: c.title, form: await open(c) })))),
    ];

    onStep("attachments");
    const mapping = new Map();
    for (const piece of pieces) {
      for (const [fileId, info] of filesOfForm(piece.form)) {
        if (mapping.has(fileId) || !info.aid) continue;
        let blob;
        try {
          blob = await api.fileBytes(fileId);
        } catch (error) {
          if (error?.status === 404) continue;
          throw new LockError("An attachment couldn't be read.", { cause: error });
        }
        const bytes = await openFile(keyFor(piece.form.keyVersion), info.aid, blob);
        const digest = await sha256Hex(bytes);
        const staged = await api.stageNoteTransitionFile(noteId, transitionId, new Blob([bytes], { type: info.mime || "application/octet-stream" }), info.name || (info.image ? "image" : "attachment"));
        if (staged.sha256 !== digest) throw new LockError("An attachment wasn't stored as sent.");
        if ((await sha256Hex(await api.fileBytes(staged.file_id))) !== digest) throw new LockError("A decrypted attachment didn't read back as it should.");
        mapping.set(fileId, { fileId: staged.file_id, aid: null });
      }
    }

    onStep("decrypting");
    const payload = { content: null, versions: {}, conflicts: {} };
    for (const piece of pieces) {
      const { keyVersion: _ignored, ...form } = remapForm(piece.form, mapping);
      let plain;
      if (form.format === "markdown") {
        plain = { title: piece.title, format: "markdown", body: form.body, tags: form.tags ?? [], summary: form.summary ?? null };
      } else {
        const projected = project(form.doc);
        plain = { title: projected.title || piece.title, format: "rich", doc: form.doc, body: projected.body, tags: projected.tags, summary: form.summary ?? null };
      }
      if (piece.slot === "content") payload.content = plain;
      else payload[piece.slot][piece.id] = plain;
    }
    return payload;
  });
  const { note } = await api.note(noteId, { includeDeleted: true });
  if (note.content_format === "encrypted" || note.revision !== result.revision) {
    throw new LockError("Removing the lock couldn't be confirmed. Reopen the note to check.", { code: "unknown" });
  }
  return note;
}

/** A locked note's content, opened: its document (and the Markdown, for an older note). */
export async function openLockedNote(key, note) {
  return openContent(key, note.id, note.lock_key_version, note.cipher);
}

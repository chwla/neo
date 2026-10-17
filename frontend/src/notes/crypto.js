/**
 * Locked notes' cryptography, in the browser (WebCrypto).
 *
 * The server keeps each key version's data key (DEK) only wrapped (AES-KW) by
 * a key derived from the profile password: PBKDF2-SHA256 over the password,
 * with the purpose prefix "neo-notes-kek:v1:" before the wrap's own salt
 * (app/services/notes/keys.py). Here the same key is derived, the DEK
 * unwrapped as a non-extractable AES-GCM key, and every locked note's content
 * and attachments are encrypted and decrypted with it. Plaintext never leaves
 * this page.
 *
 * Content: AES-GCM-256, a fresh 96-bit IV for every save, the associated data
 * "neo-note:v1:<noteId>:<keyVersion>" (so a note's ciphertext opens as that
 * note only), stored as { v: 1, iv, ct } in base64.
 * Attachments: the same, with associated data "neo-note-file:v1:<aid>" (a
 * random id kept beside the file's reference inside the encrypted note), and
 * stored as one byte 0x01, the IV, then the ciphertext.
 *
 * tests/fixtures/notes_key_vectors.json holds values the server's Python and
 * this code must both reproduce.
 */

export const PURPOSE = "neo-notes-kek:v1:";
const FILE_FORMAT = 1;
const encoder = new TextEncoder();
const decoder = new TextDecoder();

function subtle() {
  const value = globalThis.crypto?.subtle;
  if (!value) throw new Error("This browser can't lock notes here: it needs a secure connection (HTTPS or this computer).");
  return value;
}

/** Whether this page can lock notes at all (WebCrypto exists only in secure contexts). */
export function lockingAvailable() {
  return Boolean(globalThis.isSecureContext !== false && globalThis.crypto?.subtle);
}

export function toBase64(bytes) {
  let text = "";
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < view.length; i += 0x8000) text += String.fromCharCode(...view.subarray(i, i + 0x8000));
  return btoa(text);
}

export function fromBase64(text) {
  const raw = atob(text);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

function concat(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/** The key-encryption key's bytes (exported for the shared vectors; nothing else needs them). */
export async function deriveKekBytes(password, salt, iterations) {
  const base = await subtle().importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await subtle().deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: concat(encoder.encode(PURPOSE), salt), iterations },
    base,
    256,
  );
  return new Uint8Array(bits);
}

/** One key version's data key, opened with the password: an AES-GCM key nobody can export. */
export async function openWrap(password, wrap) {
  if (wrap.kdf !== "pbkdf2-sha256") throw new Error(`Unknown key derivation ${wrap.kdf}`);
  const kekBytes = await deriveKekBytes(password, fromBase64(wrap.salt), wrap.iterations);
  const kek = await subtle().importKey("raw", kekBytes, "AES-KW", false, ["unwrapKey"]);
  kekBytes.fill(0);
  return subtle().unwrapKey("raw", fromBase64(wrap.wrapped_dek), kek, "AES-KW", { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

/** Every key version the server returned, opened: Map(version, key). */
export async function openWraps(password, wraps) {
  const keys = new Map();
  for (const wrap of wraps) keys.set(wrap.key_version, await openWrap(password, wrap));
  return keys;
}

export const contentAad = (noteId, keyVersion) => `neo-note:v1:${noteId}:${keyVersion}`;
export const fileAad = (aid) => `neo-note-file:v1:${aid}`;

/** AES-GCM with a given IV (the shared vectors use fixed ones; everything else uses `seal`). */
export async function encryptWith(key, iv, aad, bytes) {
  return new Uint8Array(await subtle().encrypt({ name: "AES-GCM", iv, additionalData: encoder.encode(aad) }, key, bytes));
}

export async function decryptWith(key, iv, aad, bytes) {
  return new Uint8Array(await subtle().decrypt({ name: "AES-GCM", iv, additionalData: encoder.encode(aad) }, key, bytes));
}

const freshIv = () => globalThis.crypto.getRandomValues(new Uint8Array(12));

/** A locked note's content (any JSON value), sealed to that note and key version. */
export async function sealContent(key, noteId, keyVersion, value) {
  const iv = freshIv();
  const ct = await encryptWith(key, iv, contentAad(noteId, keyVersion), encoder.encode(JSON.stringify(value)));
  return { v: 1, iv: toBase64(iv), ct: toBase64(ct) };
}

/** The content back, or an error if it was changed, moved to another note, or sealed with another key. */
export async function openContent(key, noteId, keyVersion, cipher) {
  if (cipher?.v !== 1) throw new Error("Unknown encrypted note format.");
  const plain = await decryptWith(key, fromBase64(cipher.iv), contentAad(noteId, keyVersion), fromBase64(cipher.ct));
  return JSON.parse(decoder.decode(plain));
}

export async function sealFile(key, aid, bytes) {
  const iv = freshIv();
  const ct = await encryptWith(key, iv, fileAad(aid), bytes);
  const out = new Uint8Array(1 + iv.length + ct.length);
  out[0] = FILE_FORMAT;
  out.set(iv, 1);
  out.set(ct, 1 + iv.length);
  return out;
}

export async function openFile(key, aid, blob) {
  const bytes = blob instanceof Uint8Array ? blob : new Uint8Array(blob);
  if (bytes[0] !== FILE_FORMAT || bytes.length < 13 + 16) throw new Error("Unknown encrypted file format.");
  return decryptWith(key, bytes.subarray(1, 13), fileAad(aid), bytes.subarray(13));
}

export async function sha256Hex(bytes) {
  const digest = new Uint8Array(await subtle().digest("SHA-256", bytes));
  return [...digest].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export const newAid = () => globalThis.crypto.randomUUID();

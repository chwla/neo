/**
 * Locked notes' cryptography in the browser's WebCrypto (Node has the same API).
 *
 * What must hold: the derivation and unwrap agree with the server's Python,
 * value for value (tests/fixtures/notes_key_vectors.json, also read by
 * tests/test_notes_lock.py); a wrong password opens nothing; ciphertext opens
 * only as the note (or attachment) and key version it was sealed for, and not
 * at all once changed; no two saves share an IV.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import {
  PURPOSE,
  decryptWith,
  deriveKekBytes,
  encryptWith,
  fromBase64,
  openContent,
  openFile,
  openWrap,
  sealContent,
  sealFile,
  toBase64,
} from "../src/notes/crypto.js";

const VECTORS = JSON.parse(readFileSync(new URL("../../tests/fixtures/notes_key_vectors.json", import.meta.url), "utf8"));
const wrap = { kdf: "pbkdf2-sha256", salt: VECTORS.kek.salt, iterations: VECTORS.kek.iterations, wrapped_dek: VECTORS.wrap.wrapped_dek, key_version: 1 };

describe("the same values as the server", () => {
  test("the key-encryption key", async () => {
    assert.equal(PURPOSE, VECTORS.kek.purpose);
    const kek = await deriveKekBytes(VECTORS.kek.password, fromBase64(VECTORS.kek.salt), VECTORS.kek.iterations);
    assert.equal(toBase64(kek), VECTORS.kek.kek);
  });

  test("the unwrapped data key seals exactly what the server sealed", async () => {
    const key = await openWrap(VECTORS.kek.password, wrap);
    const c = VECTORS.content;
    const ct = await encryptWith(key, fromBase64(c.iv), c.aad, new TextEncoder().encode(c.plaintext));
    assert.equal(toBase64(ct), c.ct);
    const opened = await openContent(key, c.note_id, c.key_version, { v: 1, iv: c.iv, ct: c.ct });
    assert.deepEqual(opened, JSON.parse(c.plaintext));
  });

  test("an attachment the server sealed opens here", async () => {
    const key = await openWrap(VECTORS.kek.password, wrap);
    const f = VECTORS.file;
    assert.deepEqual(await openFile(key, f.aid, fromBase64(f.blob)), fromBase64(f.bytes));
  });
});

describe("what a key opens", () => {
  test("a wrong password opens nothing", async () => {
    await assert.rejects(openWrap("not the password", wrap));
  });

  test("ciphertext opens only as its own note and key version", async () => {
    const key = await openWrap(VECTORS.kek.password, wrap);
    const sealed = await sealContent(key, "note-a", 1, { doc: { type: "doc" }, summary: "s" });
    assert.deepEqual(await openContent(key, "note-a", 1, sealed), { doc: { type: "doc" }, summary: "s" });
    await assert.rejects(openContent(key, "note-b", 1, sealed), "moved to another note");
    await assert.rejects(openContent(key, "note-a", 2, sealed), "claimed for another key version");
    const bytes = fromBase64(sealed.ct);
    bytes[3] ^= 1;
    await assert.rejects(openContent(key, "note-a", 1, { ...sealed, ct: toBase64(bytes) }), "changed");
  });

  test("an attachment opens only as itself", async () => {
    const key = await openWrap(VECTORS.kek.password, wrap);
    const blob = await sealFile(key, "aid-1", new Uint8Array([1, 2, 3]));
    assert.deepEqual([...(await openFile(key, "aid-1", blob))], [1, 2, 3]);
    await assert.rejects(openFile(key, "aid-2", blob));
  });

  test("every save has its own IV", async () => {
    const key = await openWrap(VECTORS.kek.password, wrap);
    const ivs = new Set();
    for (let i = 0; i < 200; i += 1) ivs.add((await sealContent(key, "n", 1, { i: 0 })).iv);
    assert.equal(ivs.size, 200);
  });

  test("the data key can't be read out of the page", async () => {
    const key = await openWrap(VECTORS.kek.password, wrap);
    assert.equal(key.extractable, false);
    await assert.rejects(globalThis.crypto.subtle.exportKey("raw", key));
    // Used only for what it is for.
    assert.deepEqual([...key.usages].sort(), ["decrypt", "encrypt"]);
    await assert.rejects(decryptWith(key, new Uint8Array(12), "x", new Uint8Array(32)));
  });
});

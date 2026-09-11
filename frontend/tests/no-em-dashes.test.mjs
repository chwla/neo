/**
 * Neo does not use em dashes. Anywhere.
 *
 * The rule already existed, but only as far as one screen could see it:
 * `compare-models.test.mjs` checks that its own markup and its own generated
 * sentences carry none. That catches a sentence built at runtime and nothing
 * else, so two of them arrived in copy on other screens and no test noticed.
 *
 * This scans the source instead, which is the half that catches a new file. The
 * two together are worth keeping: a string assembled from fragments can hold an
 * em dash that no single source file does.
 *
 * Three spellings count, because all three reach the reader as the same
 * character: the literal, the HTML entity, and the JavaScript escape. Escapes
 * are the awkward one, since a test that asserts an em dash is *absent* has to
 * name it somehow. So `src/` may not contain any spelling at all, while the
 * test tree is held only to the literal, which is what would ship.
 *
 * Replacing one is a rewrite, not a substitution. An em dash is doing a job in
 * the sentence, and swapping in a hyphen leaves prose that reads as a typo. In
 * both of the cases this caught, a full stop was the answer.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, test } from "node:test";

//: Built from its code point rather than typed, so this file does not break the
//: rule it exists to enforce. Its own first run caught it doing exactly that.
const EM_DASH = String.fromCharCode(0x2014);
const ENTITIES = ["&mdash;", "&#8212;", "&#x2014;"];
const ESCAPES = ["\\u2014", "\\x{2014}"];

const REPO = fileURLToPath(new URL("../../", import.meta.url));
const TEXT = /\.(jsx?|mjs|css|py|md|json|html)$/;
const SKIP = new Set(["node_modules", ".venv", ".git", "dist", "build", "data", "__pycache__"]);

function sourceFiles(dir) {
  const found = [];
  for (const entry of readdirSync(dir)) {
    if (SKIP.has(entry)) continue;
    const path = `${dir}/${entry}`;
    if (statSync(path).isDirectory()) found.push(...sourceFiles(path));
    else if (TEXT.test(entry)) found.push(path);
  }
  return found;
}

function hits(roots, spellings) {
  const found = [];
  for (const root of roots) {
    for (const path of sourceFiles(`${REPO}${root}`)) {
      const text = readFileSync(path, "utf-8");
      for (const spelling of spellings) {
        if (!text.includes(spelling)) continue;
        const line = text.slice(0, text.indexOf(spelling)).split("\n").length;
        found.push(`${path.slice(REPO.length)}:${line}`);
      }
    }
  }
  return found;
}

describe("no em dashes anywhere in Neo", () => {
  test("the scan reaches real files", () => {
    // Without this the whole suite passes by walking an empty tree, which is the
    // one way a test shaped like this fails silently.
    const scanned = ["frontend/src", "app"].flatMap((root) => sourceFiles(`${REPO}${root}`));
    assert.ok(scanned.length > 50, `only ${scanned.length} files scanned; the roots have moved`);
  });

  test("the interface source carries none, in any spelling", () => {
    assert.deepEqual(hits(["frontend/src"], [EM_DASH, ...ENTITIES, ...ESCAPES]), []);
  });

  test("nor does the backend", () => {
    assert.deepEqual(hits(["app"], [EM_DASH, ...ENTITIES]), []);
  });

  test("nor do the tests or the docs", () => {
    // The literal only: a test is allowed to name the escape in order to assert
    // that the character is absent, which is what `compare-models` does.
    assert.deepEqual(hits(["frontend/tests", "tests", "docs"], [EM_DASH]), []);
  });
});

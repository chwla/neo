/**
 * Where dictated text lands, and what happens when the user types while it is landing.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { spaceFor, replaceRange, wasEditedByUser } from "../src/voice/insertion.js";

test("a space is added between words and nowhere else", () => {
  assert.equal(spaceFor("world", "hello", ""), " world");
  assert.equal(spaceFor("world", "hello ", ""), "world");
  assert.equal(spaceFor("world", "", ""), "world");
  assert.equal(spaceFor("world", "(", ""), "world", "not after an opening bracket");
  assert.equal(spaceFor(", then", "hello", ""), ", then", "not before punctuation");
  assert.equal(spaceFor("hello", "", "."), "hello", "not before a full stop");
  assert.equal(spaceFor("hello", "", "world"), "hello ", "trailing space when text follows");
});

test("empty or whitespace-only text inserts nothing", () => {
  assert.equal(spaceFor("   ", "hello", ""), "");
  assert.equal(replaceRange("hello", { start: 5, end: 5 }, "  ").value, "hello");
});

test("text lands at the caret without clobbering what is around it", () => {
  const result = replaceRange("Please review ", { start: 14, end: 14 }, "the composer");
  assert.equal(result.value, "Please review the composer");
  assert.equal(result.caret, result.value.length);
});

test("a caret in the middle of existing text inserts rather than overwrites", () => {
  const result = replaceRange("Please  now", { start: 7, end: 7 }, "review this");
  assert.equal(result.value, "Please review this now");
});

test("successive partials replace the span rather than appending to it", () => {
  let value = "Note: ";
  let range = { start: 6, end: 6 };

  for (const partial of ["open", "open the", "open the composer"]) {
    const step = replaceRange(value, range, partial);
    value = step.value;
    range = step.range;
  }

  assert.equal(value, "Note: open the composer", "no duplicated prefixes");
});

test("a stale range pointing past the end is clamped, not thrown", () => {
  // A partial can arrive after the user has shortened the box.
  const result = replaceRange("hi", { start: 40, end: 90 }, "there");
  assert.equal(result.value, "hi there");
});

test("an edit by the user is distinguishable from our own write", () => {
  assert.equal(wasEditedByUser("open the", "open the"), false);
  assert.equal(wasEditedByUser("open the!", "open the"), true);
  assert.equal(wasEditedByUser("", null), false);
});

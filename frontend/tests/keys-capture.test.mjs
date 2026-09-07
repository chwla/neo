/**
 * Recording a key without recording the reach for it.
 *
 * The first test is the one that matters: pressing Cmd+K sends two keydowns, and
 * a recorder that takes the first one stores "meta" and stops listening before
 * the K ever arrives. Everything else here is about knowing when a multi-chord
 * recording has ended, which no timeout answers reliably, so the caller says how
 * many it wants and the buffer says when it has them.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { MAX_CAPTURE_CHORDS, createCaptureBuffer } from "../src/keys/capture.js";

const press = (key, flags = {}) => ({
  key, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...flags,
});

describe("recording one chord", () => {
  test("holding a modifier records nothing until the other key lands", () => {
    const buffer = createCaptureBuffer();

    assert.equal(buffer.push(press("Meta", { metaKey: true })), "ignored");
    assert.equal(buffer.done, false);
    assert.equal(buffer.push(press("k", { metaKey: true })), "full");
    assert.equal(buffer.sequence, "meta+k");
  });

  test("finishes after the first real chord", () => {
    const buffer = createCaptureBuffer();
    buffer.push(press("j"));
    assert.equal(buffer.done, true);
    assert.deepEqual(buffer.chords, ["j"]);
  });

  test("keys after it has finished are ignored, not appended", () => {
    const buffer = createCaptureBuffer();
    buffer.push(press("j"));
    assert.equal(buffer.push(press("k")), "ignored");
    assert.deepEqual(buffer.chords, ["j"]);
  });

  test("normalizes exactly the way dispatch does", () => {
    // Otherwise a key records as one thing and is looked up as another.
    const buffer = createCaptureBuffer();
    buffer.push(press("G", { shiftKey: true }));
    assert.equal(buffer.sequence, "G");
  });
});

describe("recording a sequence", () => {
  test("collects chords until it has as many as it was asked for", () => {
    const buffer = createCaptureBuffer({ maxChords: 2 });

    assert.equal(buffer.push(press("g")), "recorded");
    assert.equal(buffer.done, false);
    assert.equal(buffer.push(press("c")), "full");
    assert.equal(buffer.sequence, "g c");
  });

  test("a modifier reached for mid-sequence does not count as a chord", () => {
    const buffer = createCaptureBuffer({ maxChords: 2 });
    buffer.push(press("g"));
    buffer.push(press("Shift", { shiftKey: true }));
    buffer.push(press("C", { shiftKey: true }));
    assert.equal(buffer.sequence, "g C");
  });

  test("never records more than a person would remember", () => {
    const buffer = createCaptureBuffer({ maxChords: 99 });
    for (const key of "abcdefg") buffer.push(press(key));
    assert.equal(buffer.chords.length, MAX_CAPTURE_CHORDS);
  });

  test("asking for none still records one", () => {
    const buffer = createCaptureBuffer({ maxChords: 0 });
    assert.equal(buffer.push(press("j")), "full");
  });
});

describe("the difference the settings screen depends on", () => {
  test("a single key is finished by the key itself, with nothing to confirm", () => {
    // Almost every rebinding is one key. Making somebody press Done afterwards
    // turned the common case into two steps and, before this, into none at all:
    // the recording just never committed.
    const buffer = createCaptureBuffer({ maxChords: 1 });
    assert.equal(buffer.push(press("z")), "full", "\"full\" is what commits it");
    assert.equal(buffer.sequence, "z");
  });

  test("a sequence does not finish early, so Done is what ends it", () => {
    const buffer = createCaptureBuffer({ maxChords: MAX_CAPTURE_CHORDS });
    assert.equal(buffer.push(press("g")), "recorded");
    assert.equal(buffer.done, false, "one chord is not a finished sequence");
    assert.equal(buffer.push(press("z")), "recorded");
    assert.equal(buffer.done, false);
    // Whatever it has when Done is pressed is what gets stored.
    assert.equal(buffer.sequence, "g z");
  });
});

describe("escape", () => {
  test("cancels rather than being recorded", () => {
    // It is not bindable, and it is the only way out of a field that is eating
    // every key on purpose.
    const buffer = createCaptureBuffer();

    assert.equal(buffer.push(press("Escape")), "cancelled");
    assert.equal(buffer.cancelled, true);
    assert.equal(buffer.done, true);
    assert.deepEqual(buffer.chords, []);
  });

  test("cancels a sequence part-way through", () => {
    const buffer = createCaptureBuffer({ maxChords: 3 });
    buffer.push(press("g"));
    buffer.push(press("Escape"));
    assert.equal(buffer.cancelled, true);
    assert.deepEqual(buffer.chords, []);
  });

  test("nothing is recorded after a cancel", () => {
    const buffer = createCaptureBuffer({ maxChords: 3 });
    buffer.push(press("Escape"));
    assert.equal(buffer.push(press("g")), "ignored");
  });
});

describe("reset", () => {
  test("makes the buffer usable again after a cancel", () => {
    const buffer = createCaptureBuffer();
    buffer.push(press("Escape"));
    buffer.reset();
    assert.equal(buffer.cancelled, false);
    assert.equal(buffer.push(press("j")), "full");
  });

  test("the chords it hands out cannot be used to change it", () => {
    const buffer = createCaptureBuffer({ maxChords: 2 });
    buffer.push(press("g"));
    buffer.chords.push("nonsense");
    assert.deepEqual(buffer.chords, ["g"]);
  });
});

describe("a malformed event", () => {
  test("is ignored rather than thrown on", () => {
    const buffer = createCaptureBuffer();
    assert.equal(buffer.push(undefined), "ignored");
    assert.equal(buffer.push({}), "ignored");
    assert.equal(buffer.done, false);
  });
});

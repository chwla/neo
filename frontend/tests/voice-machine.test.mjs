/**
 * What dictation is allowed to do from each state.
 *
 * These pin the rules that stop the failure modes that are otherwise only reachable by
 * clicking fast: two recordings at once, a cancelled transcript arriving anyway, a
 * microphone left open after an error.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  IDLE, STARTING, RECORDING, PROCESSING, ERROR,
  next, can, isCapturing, isBusy,
} from "../src/voice/dictationMachine.js";

describe("the dictation machine", () => {
  test("walks the happy path", () => {
    assert.equal(next(IDLE, "start"), STARTING);
    assert.equal(next(STARTING, "ready"), RECORDING);
    assert.equal(next(RECORDING, "stop"), PROCESSING);
    assert.equal(next(PROCESSING, "done"), IDLE);
  });

  test("refuses a second start while already going", () => {
    for (const state of [STARTING, RECORDING, PROCESSING]) {
      assert.equal(can(state, "start"), false, `${state} must not restart`);
    }
  });

  test("refuses a second stop", () => {
    assert.equal(can(PROCESSING, "stop"), false);
    assert.equal(can(IDLE, "stop"), false);
  });

  test("cancel works from every state that has something to cancel", () => {
    // Including PROCESSING: that is what stops a transcript the user gave up on from
    // arriving in the composer afterwards.
    for (const state of [STARTING, RECORDING, PROCESSING, ERROR]) {
      assert.equal(can(state, "cancel"), true, `${state} must be cancellable`);
    }
  });

  test("an error is recoverable without a reload", () => {
    assert.equal(next(ERROR, "start"), STARTING);
    assert.equal(next(ERROR, "dismiss"), IDLE);
  });

  test("knows when the microphone is open", () => {
    assert.equal(isCapturing(STARTING), true);
    assert.equal(isCapturing(RECORDING), true);
    assert.equal(isCapturing(PROCESSING), false, "the stream is closed before decoding");
    assert.equal(isCapturing(IDLE), false);
  });

  test("knows when to show the user something is happening", () => {
    assert.deepEqual(
      [IDLE, STARTING, RECORDING, PROCESSING, ERROR].map(isBusy),
      [false, true, false, true, false],
    );
  });
});

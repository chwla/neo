import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

import {
  dispatchEscape,
  openModalCount,
  registerModal,
  resetModalStack,
} from "../src/modalStack.js";

const escape = { key: "Escape" };

describe("modal escape stack", () => {
  beforeEach(() => resetModalStack());

  test("escape with nothing open is not handled", () => {
    assert.equal(dispatchEscape(escape), false);
  });

  test("escape closes the only open dialog", () => {
    let closed = 0;
    registerModal(() => { closed += 1; });

    assert.equal(dispatchEscape(escape), true);
    assert.equal(closed, 1);
  });

  test("escape reaches only the top-most dialog", () => {
    const closed = [];
    registerModal(() => closed.push("settings"));
    registerModal(() => closed.push("confirm"));

    dispatchEscape(escape);

    assert.deepEqual(closed, ["confirm"], "the dialog underneath must stay open");
  });

  test("closing the top dialog hands escape back to the one below", () => {
    const closed = [];
    registerModal(() => closed.push("settings"));
    const releaseConfirm = registerModal(() => closed.push("confirm"));

    dispatchEscape(escape);
    releaseConfirm();
    dispatchEscape(escape);

    assert.deepEqual(closed, ["confirm", "settings"]);
  });

  test("a dialog that unmounts out of order is removed from the middle", () => {
    const closed = [];
    registerModal(() => closed.push("a"));
    const releaseB = registerModal(() => closed.push("b"));
    registerModal(() => closed.push("c"));

    releaseB();

    assert.equal(openModalCount(), 2);
    dispatchEscape(escape);
    assert.deepEqual(closed, ["c"]);
  });

  test("unregistering twice does not disturb the rest of the stack", () => {
    const closed = [];
    const releaseA = registerModal(() => closed.push("a"));
    registerModal(() => closed.push("b"));

    releaseA();
    releaseA();

    assert.equal(openModalCount(), 1);
    dispatchEscape(escape);
    assert.deepEqual(closed, ["b"]);
  });

  test("keys other than escape are ignored", () => {
    let closed = 0;
    registerModal(() => { closed += 1; });

    for (const key of ["Enter", "Tab", "esc", "Esc", "a", ""]) {
      assert.equal(dispatchEscape({ key }), false);
    }
    assert.equal(closed, 0);
  });

  test("a missing or malformed event is ignored rather than thrown on", () => {
    registerModal(() => { throw new Error("must not be called"); });

    assert.equal(dispatchEscape(undefined), false);
    assert.equal(dispatchEscape({}), false);
  });

  test("the count tracks mounts and unmounts", () => {
    assert.equal(openModalCount(), 0);
    const release = registerModal(() => {});
    assert.equal(openModalCount(), 1);
    release();
    assert.equal(openModalCount(), 0);
  });

  test("a dialog with no handler is still popped without throwing", () => {
    registerModal(undefined);

    assert.equal(dispatchEscape(escape), true);
    assert.equal(openModalCount(), 1);
  });
});

import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

import {
  dispatchEscape,
  openModalCount,
  registerCover,
  registerModal,
  resetModalStack,
} from "../src/modalStack.js";

const escape = { key: "Escape" };

describe("modal escape stack", () => {
  beforeEach(() => resetModalStack());

  test("the document is flagged while anything is over the app", () => {
    // The background engine and the stylesheet both read this to stop painting a
    // field that is covered -- and, since the settings panel became glass, to
    // stop the pane above it re-blurring a moving backdrop sixty times a second.
    // The flag is about whether the app is covered at all, so it has to outlast
    // the dialogs stacked on top of the first one.
    const saved = globalThis.document;
    globalThis.document = { documentElement: { dataset: {} } };
    try {
      const closeSettings = registerModal(() => {});
      assert.equal(document.documentElement.dataset.modalOpen, "");

      const closeConfirm = registerModal(() => {});
      closeConfirm();
      assert.equal(
        document.documentElement.dataset.modalOpen,
        "",
        "the dialog underneath is still covering the field",
      );

      closeSettings();
      assert.equal(document.documentElement.dataset.modalOpen, undefined);
    } finally {
      if (saved === undefined) delete globalThis.document;
      else globalThis.document = saved;
    }
  });

  test("covering the app is not the same as claiming escape", () => {
    // The settings pages that hand-roll their backdrop need the field stopped,
    // and they have never closed on Escape. Handing them the stack to get the
    // first would have given them the second, which closes a half-filled form on
    // a keypress that did nothing yesterday.
    const saved = globalThis.document;
    globalThis.document = { documentElement: { dataset: {} } };
    try {
      const uncover = registerCover();

      assert.equal(document.documentElement.dataset.modalOpen, "", "the field is still running");
      assert.equal(openModalCount(), 0, "a cover took a place in the escape stack");
      assert.equal(dispatchEscape(escape), false, "escape was swallowed by a dialog that ignores it");

      uncover();
      uncover();
      assert.equal(document.documentElement.dataset.modalOpen, undefined, "releasing twice double-counted");
    } finally {
      if (saved === undefined) delete globalThis.document;
      else globalThis.document = saved;
    }
  });

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

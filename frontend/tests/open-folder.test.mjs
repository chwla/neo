/**
 * The path trail in the Open-a-folder browser.
 *
 * The one property that matters is where it stops. Segments above the
 * configured root name directories the server will refuse to list, so putting
 * one on screen builds a control whose only possible outcome is an error --
 * and it invites the user to try leaving a boundary that exists on purpose.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { breadcrumbSegments } from "../src/OpenFolderDialog.jsx";

const ROOTS = ["/workspace"];

describe("breadcrumb segments", () => {
  test("the root alone is a single segment", () => {
    assert.deepEqual(breadcrumbSegments("/workspace", ROOTS), [
      { label: "/workspace", path: "/workspace" },
    ]);
  });

  test("nesting adds one clickable segment per level", () => {
    assert.deepEqual(breadcrumbSegments("/workspace/demo/src", ROOTS), [
      { label: "/workspace", path: "/workspace" },
      { label: "demo", path: "/workspace/demo" },
      { label: "src", path: "/workspace/demo/src" },
    ]);
  });

  test("no segment is ever above the root", () => {
    const trail = breadcrumbSegments("/workspace/demo", ROOTS);

    // "/" and "/workspace/.." are both directories the browse endpoint refuses.
    assert.ok(trail.every((segment) => segment.path.startsWith("/workspace")));
  });

  test("the deepest segment is the folder you are in", () => {
    const trail = breadcrumbSegments("/workspace/demo/src", ROOTS);

    assert.equal(trail.at(-1).path, "/workspace/demo/src");
  });

  test("nothing to show before the first listing arrives", () => {
    assert.deepEqual(breadcrumbSegments(null, ROOTS), []);
    assert.deepEqual(breadcrumbSegments("", ROOTS), []);
  });

  test("the longest matching root wins when roots nest", () => {
    const trail = breadcrumbSegments("/workspace/inner/demo", ["/workspace", "/workspace/inner"]);

    assert.deepEqual(trail[0], { label: "/workspace/inner", path: "/workspace/inner" });
    assert.equal(trail.length, 2);
  });

  test("a sibling that merely shares a prefix is not treated as inside the root", () => {
    // "/workspace-old" starts with "/workspace" as a string but is a different
    // directory; matching on the raw prefix would mis-root the whole trail.
    assert.deepEqual(breadcrumbSegments("/workspace-old/demo", ROOTS), [
      { label: "/workspace-old/demo", path: "/workspace-old/demo" },
    ]);
  });

  test("a path outside every root still renders as itself rather than vanishing", () => {
    assert.deepEqual(breadcrumbSegments("/elsewhere", ROOTS), [
      { label: "/elsewhere", path: "/elsewhere" },
    ]);
  });

  test("windows paths split on their own separator", () => {
    assert.deepEqual(breadcrumbSegments("C:\\code\\demo", ["C:\\code"]), [
      { label: "C:\\code", path: "C:\\code" },
      { label: "demo", path: "C:\\code\\demo" },
    ]);
  });
});

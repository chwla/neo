/**
 * Every profile picture in Neo is a circle.
 *
 * There are four places one appears -- the sidebar footer, the profile picker's
 * cards, the picture chosen while creating a profile, and Account settings --
 * and they are styled by three rules in three parts of the stylesheet. Rounding
 * them was four separate edits, which is exactly the shape of change that gets
 * three quarters done: the sidebar was rounded first and the picker stayed
 * square, and nothing said so until somebody opened the picker.
 *
 * So the rule is asserted rather than remembered. Any selector whose name says
 * it holds a person's picture has to be round, and a new one is caught here
 * rather than on screen.
 *
 * The two tiles that stand in an avatar's place on their own cards -- "New
 * profile" and "Guest" -- are held to the same rule. They are the same size and
 * the same slot, and a square among circles reads as one of them having failed
 * to load.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

const CSS = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

/** Every declaration block, comments stripped so prose cannot match a selector. */
const RULES = [...CSS.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]*)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.split(";").pop().trim(), body }));

//: Names that mean "a person's picture goes here", and the two tiles that sit in
//: that slot. Matched against each comma-separated part, so a rule that dresses
//: an avatar alongside something else is still caught.
const AVATAR = /(^|[\s.])(sidebar-profile|profile-avatar|account-avatar|profile-add-icon|profile-guest-icon)\b/;

function avatarRules() {
  return RULES.filter((rule) =>
    rule.selector.split(",").some((part) => AVATAR.test(part.trim())),
  );
}

describe("profile pictures", () => {
  test("the selectors this guards still exist", () => {
    // If the names are ever refactored this file would pass by matching nothing,
    // which is the one way a test like this fails silently.
    const found = avatarRules();
    assert.ok(found.length >= 3, `only ${found.length} avatar rules found; the names have drifted`);
  });

  test("none of them is square", () => {
    //: Read the value out and compare it, rather than asking a regex whether it
    //: is absent -- `\s*` before a lookahead will match nothing and let the
    //: lookahead pass on the space, which says every rule is square.
    const square = [];
    for (const rule of avatarRules()) {
      for (const [, value] of rule.body.matchAll(/border-radius:\s*([^;]+)/g)) {
        if (value.trim() !== "50%") square.push(`${rule.selector} -> ${value.trim()}`);
      }
    }
    assert.deepEqual(square, [], "an avatar is not a circle");
  });

  test("each one is round somewhere in its own rule", () => {
    // A radius inherited from nowhere is a square: these all set their own.
    const shaped = new Set();
    for (const rule of avatarRules()) {
      if (/border-radius:\s*50%/.test(rule.body)) {
        for (const part of rule.selector.split(",")) shaped.add(part.trim());
      }
    }
    for (const name of ["sidebar-profile", "profile-avatar", "account-avatar"]) {
      assert.ok(
        [...shaped].some((selector) => selector.includes(name)),
        `.${name} is never given a radius of its own`,
      );
    }
  });

  test("a picture is cropped to the circle rather than overflowing it", () => {
    // The initials are centred text and fit whatever the shape is; an uploaded
    // photo is a square image and needs clipping, or it corners the circle.
    for (const name of ["sidebar-profile", "profile-avatar", "account-avatar"]) {
      const rule = avatarRules().find(
        (candidate) =>
          candidate.selector.split(",").some((part) => part.trim().endsWith(`.${name}`)) &&
          /border-radius:\s*50%/.test(candidate.body),
      );
      assert.ok(rule, `.${name} has no rounded rule to check`);
      assert.match(rule.body, /overflow:\s*hidden/, `.${name} would let a photo escape its circle`);
    }
  });
});

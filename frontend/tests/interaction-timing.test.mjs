/**
 * Feedback timing in the stylesheet.
 *
 * What a pointer, focus or click turns on has to appear on the next frame; only
 * the way back may fade. A transition runs with the timing of the state being
 * entered, so the convention is: the base rule keeps its `transition` (the fade
 * out) and every on-state rule sets `transition-duration`. These tests hold the
 * convention in place for the controls that have it, so a new hover rule cannot
 * quietly bring the slow fade-in back.
 *
 * The press dim is checked the same way: one shared rule, scoped to the app's
 * own controls, built on `filter: opacity()` so it multiplies with an existing
 * opacity instead of replacing it, and covering every control that has a hover
 * style.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

const CSS = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const SOURCE = CSS.replace(/\/\*[\s\S]*?\*\//g, "");

//: Innermost declaration blocks, as in backgrounds.test.mjs.
const RULES = [...SOURCE.matchAll(/([^{}]*)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.split(";").pop().trim().replace(/\s+/g, " "), body }));

const COLOR_PROPS = new Set([
  "background", "background-color", "color", "border-color", "border-bottom-color", "box-shadow", "opacity",
]);
const STATE = /:hover|:focus-visible|:focus-within|\.active\b|\.is-active\b|\.on\b|\.selected\b|\.is-selected\b|\.is-open\b/;

function transitioned(body) {
  const match = body.match(/(?:^|;)\s*transition\s*:\s*([^;]+)/);
  if (!match) return [];
  return match[1].split(",").map((item) => item.trim().split(/\s+/)[0]);
}

function declares(body, prop) {
  return new RegExp(`(?:^|;)\\s*${prop.replace("-", "\\-")}\\s*:`).test(body);
}

//: "Same element, in a state": state tokens and :not() groups removed.
function stripStates(selector) {
  return selector
    .replace(/:not\([^()]*(?:\([^()]*\)[^()]*)*\)/g, "")
    .replace(/:hover|:focus-visible|:focus-within/g, "")
    .replace(/\.(?:active|is-active|on|selected|is-selected|is-open)\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

const BASES = RULES
  .map((rule) => ({ ...rule, props: transitioned(rule.body).filter((prop) => COLOR_PROPS.has(prop)) }))
  .filter((rule) => rule.props.length && !STATE.test(rule.selector))
  .flatMap((rule) => rule.selector.split(",").map((selector) => ({ selector: selector.trim(), props: rule.props })));

describe("instant in, soft fade out", () => {
  test("there are transitioned controls to check", () => {
    assert.ok(BASES.length >= 40, `only ${BASES.length} bases found; the parser is probably broken`);
  });

  test("every on-state of a transitioned control lands without a fade", () => {
    const missing = [];
    for (const rule of RULES) {
      if (!STATE.test(rule.selector)) continue;
      for (const part of rule.selector.split(",").map((s) => s.trim())) {
        if (!STATE.test(part)) continue;
        const element = stripStates(part);
        const base = BASES.find((b) => element === b.selector || element.endsWith(` ${b.selector}`));
        if (!base) continue;
        if (!base.props.some((prop) => declares(rule.body, prop) || (prop === "background" && declares(rule.body, "background-color")))) continue;
        if (!declares(rule.body, "transition-duration")) missing.push(part);
      }
    }
    assert.deepEqual(missing, [], `on-state rules that would fade in: ${missing.join(", ")}`);
  });

  test("no hover rule resets timing with the transition shorthand", () => {
    const offenders = RULES.filter((rule) => rule.selector.includes(":hover") && /(?:^|;)\s*transition\s*:/.test(rule.body));
    assert.deepEqual(offenders.map((r) => r.selector), []);
  });

  test("lifts keep easing their movement while the colour lands at once", () => {
    for (const selector of [".gallery-strip-button:hover", ".gal-card-shot:hover"]) {
      const rule = RULES.find((r) => r.selector === selector);
      assert.ok(rule, `${selector} is missing`);
      assert.match(rule.body, /transition-duration:\s*0s,\s*\.14s,\s*\.14s/);
    }
  });

  test("only arriving rows slide in, and reduced motion stills them", () => {
    const row = RULES.find((r) => r.selector === ".neo-chat-message");
    assert.ok(row, ".neo-chat-message is missing");
    assert.doesNotMatch(row.body, /animation/, "saved rows must mount still");
    const arriving = RULES.filter((r) => r.selector === ".neo-chat-message.is-arriving");
    assert.equal(arriving.length, 2, "one entrance rule and one reduced-motion override");
    assert.match(arriving[0].body, /animation:\s*neo-slide-in/);
    assert.match(arriving[1].body, /animation:\s*none/);
  });

  test("the composer menu appears on the frame it opens", () => {
    const menu = RULES.find((r) => r.selector === ".composer-menu");
    assert.ok(menu, ".composer-menu is missing");
    assert.doesNotMatch(menu.body, /animation/);
  });
});

describe("press feedback", () => {
  const press = RULES.find((r) => r.selector.endsWith(':not(:disabled):not([aria-disabled="true"]):active'));
  const headers = RULES.find((r) => r.selector.includes(":active:not(:has("));
  //: The selector list inside the leading :is( ... ), split on top-level commas.
  const items = (rule) => {
    const text = rule.selector;
    assert.ok(text.startsWith(":is("), `${text.slice(0, 40)} does not open with :is(`);
    const out = [];
    let depth = 0;
    let current = "";
    for (const char of text.slice(4)) {
      if (char === "(") depth += 1;
      if (char === ")") {
        if (depth === 0) break;
        depth -= 1;
      }
      if (char === "," && depth === 0) { out.push(current.trim()); current = ""; continue; }
      current += char;
    }
    out.push(current.trim());
    return out.filter(Boolean);
  };

  test("one shared token, opacity-based, switched off for high contrast", () => {
    assert.match(SOURCE, /html\s*\{\s*--neo-press:\s*opacity\(\.75\);\s*\}/);
    assert.match(SOURCE, /@media \(prefers-contrast: more\), \(forced-colors: active\)\s*\{\s*html\s*\{\s*--neo-press:\s*none;/);
  });

  test("controls dim through filter: opacity(), never by replacing opacity", () => {
    assert.ok(press, "the control press rule is missing");
    assert.match(press.body, /filter:\s*var\(--neo-press\)/);
    assert.doesNotMatch(press.body, /(?:^|;)\s*opacity\s*:/);
    assert.ok(headers, "the disclosure-header press rule is missing");
    assert.match(headers.body, /filter:\s*var\(--neo-press\)/);
  });

  test("the list is scoped to the app's own controls", () => {
    for (const item of [...items(press), ...items(headers)]) {
      assert.match(item, /\./, `${item} is a bare element selector`);
    }
  });

  test("every control with a hover style also has press feedback", () => {
    const covered = new Set([...items(press), ...items(headers)]);
    const FORM_FIELDS = new Set(["select", ".gal .gal-controls select", ".cmp-input", ".cmp-slot-select", ".chat-llm-picker", ".agent-chip", ".agent-chip:has(select)"]);
    const REPLACE = new Map([
      [".chat-item", ".chat-item-title"], [".chat-item.is-archived", null],
      [".project-chat-item", ".project-chat-link"], [".project-folder", null], [".gal-card", null],
      [".ws-tile", "button.ws-tile"], [".gal-card .gal-card-pin", ".gal-card-pin"],
      [".settings-section li > button", ".settings-section li button"],
      [".row-actions-menu .row-actions-danger", ".row-actions-menu button"],
    ]);
    const uncovered = new Set();
    for (const rule of RULES) {
      for (let selector of rule.selector.split(",").map((s) => s.trim())) {
        if (!selector.includes(":hover") || selector.includes("scrollbar")) continue;
        const parts = selector.split(" ");
        const index = parts.findIndex((p) => p.includes(":hover"));
        const compound = parts[index].replace(/:hover/g, "").replace(/:not\([^)]*\)/g, "").replace(/::?[a-z-]+$/, "");
        const prefix = parts.slice(0, index).map((p) => p.replace(/:hover/g, "").replace(/:has\([^)]*\)/g, ""));
        let subject = [...prefix, compound].join(" ");
        if (FORM_FIELDS.has(subject)) continue;
        if (REPLACE.has(subject)) { subject = REPLACE.get(subject); if (!subject) continue; }
        subject = subject.replace(/\.(danger|primary|is-broken|is-recording|is-archived)\b/g, "").replace(/\.chat-dictate-button$/, "").trim();
        if (REPLACE.has(subject)) subject = REPLACE.get(subject);
        if (subject && !covered.has(subject)) uncovered.add(subject);
      }
    }
    assert.deepEqual([...uncovered], [], `hover-styled controls with no press feedback: ${[...uncovered].join(", ")}`);
  });
});

/**
 * The usage panel, and chiefly its stamps.
 *
 * Neither CLI answers "how much have I got left?" on demand, so every figure the
 * panel shows was true at some earlier moment. That makes "as of 4h ago" load
 * bearing rather than decorative: without it a reading from last week is
 * indistinguishable from one from a minute ago, and someone plans a long run
 * against a number that has already expired. So these pin the stamp as hard as
 * they pin the percentages.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { EngineUsage, observedPhrase, untilPhrase } from "../src/UsagePanel.jsx";

const seconds = (offset) => Date.now() / 1000 + offset;

const CLAUDE = {
  id: "claude_code",
  name: "Claude Code",
  available: true,
  account: {
    auth_method: "Claude AI",
    email: "someone@example.com",
    organization: "someone@example.com's Organization",
    plan: "pro",
  },
  windows: [
    { key: "five_hour", title: "Session (5h)", used_percent: 59, resets_at: seconds(3 * 3600), severity: "normal" },
    { key: "seven_day", title: "Weekly (7 day)", used_percent: 6, resets_at: seconds(6 * 86400), severity: "normal" },
  ],
  observed_at: seconds(-30),
  source: "cli_cache",
  reason: null,
};

const render = (engine) => renderToStaticMarkup(createElement(EngineUsage, { engine }));

describe("usage phrasing", () => {
  test("a reading always says how old it is", () => {
    assert.equal(observedPhrase(seconds(-10)), "as of just now");
    assert.equal(observedPhrase(seconds(-600)), "as of 10m ago");
    assert.equal(observedPhrase(seconds(-4 * 3600)), "as of 4h ago");
    assert.equal(observedPhrase(seconds(-3 * 86400)), "as of 3d ago");
  });

  // A source that never reported has nothing to stamp, and inventing "as of now"
  // for it would be the exact lie the stamp exists to prevent.
  test("an unknown observation time says nothing rather than guessing", () => {
    assert.equal(observedPhrase(null), "");
    assert.equal(observedPhrase(undefined), "");
  });

  test("resets read forwards", () => {
    assert.equal(untilPhrase(seconds(1800)), "Resets in 30m");
    assert.equal(untilPhrase(seconds(3 * 3600)), "Resets in 3h");
    assert.equal(untilPhrase(seconds(6 * 86400)), "Resets in 6d");
    assert.equal(untilPhrase(seconds(-60)), "Resets now");
  });
});

describe("an engine block", () => {
  test("shows the account, the bars and the stamp", () => {
    const html = render(CLAUDE);

    assert.ok(html.includes("Claude Code"));
    assert.ok(html.includes("someone@example.com"));
    assert.ok(html.includes(">pro<"));
    assert.ok(html.includes(">59%<"), "the percentage is the accessible copy of the bar");
    assert.ok(html.includes("Resets in 3h"));
    // Engine-neutral on purpose: the token is what selects this prose, and more
    // than one engine can return it.
    assert.ok(html.includes("as of just now · from the CLI&#x27;s own usage cache"));
  });

  test("the bar is decoration over a number already stated", () => {
    const html = render(CLAUDE);

    assert.ok(/class="usage-track severity-normal" aria-hidden="true"/.test(html));
    assert.ok(html.includes('style="width:59%"'));
  });

  test("severity reaches the track so colour and number agree", () => {
    const html = render({
      ...CLAUDE,
      windows: [{ key: "five_hour", title: "Session (5h)", used_percent: 100, resets_at: null, severity: "exhausted" }],
    });

    assert.ok(html.includes("severity-exhausted"));
    assert.ok(html.includes(">100%<"));
  });

  test("an engine with no reading explains itself instead of showing zero", () => {
    const html = render({
      ...CLAUDE,
      account: null,
      windows: [],
      observed_at: null,
      source: null,
      reason: "Codex reports usage only after a completed run",
    });

    assert.ok(html.includes("Codex reports usage only after a completed run"));
    assert.ok(!html.includes("usage-track"), "no bar may be drawn for a reading that does not exist");
    assert.ok(!html.includes("as of"));
  });
});

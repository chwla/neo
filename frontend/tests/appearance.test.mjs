/**
 * The theme catalogue and its picker.
 *
 * Three things can go wrong when a theme is added, and none of them shows up as
 * a crash: a card whose id has no `[data-theme]` block renders the default and
 * looks like nothing happened; the default growing a block of its own means two
 * places define one palette; and `applyTheme` writing an unknown id leaves the
 * interface with no colours at all. The tests are mostly about those.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import AppearanceSettings from "../src/AppearanceSettings.jsx";
import { DEFAULT_THEME_ID, THEMES, applyTheme, isThemeId, themeById } from "../src/themes.js";

const CSS = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

function render(theme) {
  return renderToStaticMarkup(
    createElement(AppearanceSettings, { theme, onThemeChange() {}, onClose() {} }),
  );
}

describe("the theme catalogue", () => {
  test("every theme has a block in the stylesheet", () => {
    for (const theme of THEMES) {
      if (theme.id === DEFAULT_THEME_ID) continue;
      assert.ok(
        CSS.includes(`[data-theme="${theme.id}"]`),
        `${theme.id} has a card but no palette`,
      );
    }
  });

  test("the default is :root itself and has no block of its own", () => {
    // Two places defining one palette is how the default drifts from what a
    // profile with no preference actually sees.
    assert.ok(!CSS.includes(`[data-theme="${DEFAULT_THEME_ID}"]`));
    assert.ok(THEMES.some((theme) => theme.id === DEFAULT_THEME_ID));
  });

  test("every theme declares the anchors the derived tokens read", () => {
    const anchors = ["--neo-accent", "--neo-ink", "--neo-bg", "--neo-line", "--neo-danger"];
    for (const theme of THEMES) {
      if (theme.id === DEFAULT_THEME_ID) continue;
      const block = CSS.split(`[data-theme="${theme.id}"] {`)[1].split("\n}")[0];
      for (const anchor of anchors) {
        assert.ok(block.includes(`${anchor}:`), `${theme.id} never sets ${anchor}`);
      }
    }
  });

  test("ids are unique, and every card has a name, a sentence and a swatch", () => {
    assert.equal(new Set(THEMES.map((t) => t.id)).size, THEMES.length);
    for (const theme of THEMES) {
      assert.ok(theme.name && theme.description, `${theme.id} is missing its copy`);
      assert.equal(theme.swatch.length, 5, `${theme.id} needs five chips`);
    }
  });

  test("a stale id looks up as undefined rather than throwing", () => {
    // A profile can hold the id of a theme a later release removed.
    assert.equal(themeById("vaporwave"), undefined);
    assert.equal(isThemeId("vaporwave"), false);
    assert.ok(themeById(DEFAULT_THEME_ID));
  });
});

describe("applying a theme", () => {
  test("sets the attribute the stylesheet selects on", () => {
    const root = { dataset: {} };
    applyTheme("ice", root);
    assert.equal(root.dataset.theme, "ice");
  });

  test("the default removes the attribute rather than naming itself", () => {
    const root = { dataset: { theme: "ice" } };
    applyTheme(DEFAULT_THEME_ID, root);
    assert.equal(root.dataset.theme, undefined);
  });

  test("an unknown id falls back to the default instead of unstyling the app", () => {
    const root = { dataset: { theme: "ice" } };
    applyTheme("vaporwave", root);
    assert.equal(root.dataset.theme, undefined, "no palette is worse than the old one");
  });
});

describe("the picker", () => {
  test("offers every theme", () => {
    const html = render(DEFAULT_THEME_ID);
    for (const theme of THEMES) {
      assert.ok(html.includes(theme.name), `${theme.name} is not offered`);
    }
  });

  test("marks the current theme as the chosen radio", () => {
    const html = render("amber");
    const cards = html.split("<button").filter((chunk) => chunk.includes('role="radio"'));
    const checked = cards.filter((chunk) => chunk.includes('aria-checked="true"'));

    assert.equal(checked.length, 1, "exactly one theme is current");
    assert.ok(checked[0].includes("Amber CRT"));
  });

  test("is a radiogroup, so arrow keys move between themes", () => {
    assert.ok(render(DEFAULT_THEME_ID).includes('role="radiogroup"'));
  });

  test("says the change is not only the accent", () => {
    // The one thing worth explaining: people expect a theme to retint a button.
    assert.ok(render(DEFAULT_THEME_ID).includes("whole"));
  });
});

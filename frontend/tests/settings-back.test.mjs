/**
 * Getting back out of a settings page.
 *
 * Opening one is a hand-off: the menu closes and the page opens, so the menu is
 * not underneath waiting to be uncovered. Without a way back, the only exits are
 * the corner close and Escape, and both of those leave settings altogether --
 * so changing two things meant reopening the menu and finding your place again.
 *
 * The risk this guards is drift rather than breakage. A page is added by writing
 * the open handler and the render site, and neither of them looks wrong on its
 * own when the back path is missing; the page simply has no way back and nothing
 * says so. `fromSettings` mints both directions together, so these tests check
 * that every page is minted rather than hand-wired, and that the affordance the
 * shared `Modal` renders is actually dressed.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

const APP = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const CSS = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

/** Every page the settings menu hands off to, as [name, state setter]. */
function settingsPages() {
  return [
    ...APP.matchAll(/onOpen(\w+)=\{\(\) => \{ setShowSettings\(false\); (setShow\w+)\(true\); \}\}/g),
  ].map(([, name, setter]) => ({ name, setter }));
}

describe("leaving a settings page", () => {
  test("the menu hands off to pages, so there are pages to come back from", () => {
    // If this ever reads zero the pattern below has changed and every other
    // test in this file would pass by matching nothing.
    assert.ok(settingsPages().length > 15, "found almost no settings pages; the pattern has drifted");
  });

  test("every page the menu opens can get back to it", () => {
    const unwired = settingsPages().filter(({ setter }) => !APP.includes(`fromSettings(${setter})`));
    assert.deepEqual(
      unwired.map(({ name }) => name),
      [],
      "these settings pages have no way back to the menu",
    );
  });

  test("the way back is minted with the way in, not written out again", () => {
    // A hand-wired `onBack` is the drift this is about: it works on the day it
    // is written and is the thing that gets forgotten next time.
    const helper = APP.match(/const fromSettings = useCallback\(\s*\(setter\) => \(\{([\s\S]*?)\}\),/);
    assert.ok(helper, "fromSettings is gone; the pair is being written out per page");
    assert.match(helper[1], /backLabel:/, "the labelled exit has no label");
    assert.match(helper[1], /onBack:/, "there is no back direction");
    assert.match(helper[1], /onClose:/, "leaving altogether has to stay possible");
  });

  test("back returns to the menu and close still leaves entirely", () => {
    // The two exits mean different things everywhere else in the interface, and
    // collapsing them here would make the corner close reopen settings.
    const helper = APP.match(/const fromSettings = useCallback\(\s*\(setter\) => \(\{([\s\S]*?)\}\),/)[1];
    const back = helper.match(/onBack: \(\) => \{([\s\S]*?)\},/)[1];
    assert.match(back, /setter\(false\)/, "back does not close the page");
    assert.match(back, /setShowSettings\(true\)/, "back does not reopen the menu");
    const close = helper.match(/onClose: ([^\n]*)/)[1];
    assert.match(close, /setter\(false\)/);
    assert.doesNotMatch(close, /setShowSettings\(true\)/, "the corner close should not reopen settings");
  });

  test("the affordance the shared dialog renders is styled", () => {
    // `Modal` has taken a `backLabel` for a long time and nothing passed one, so
    // the button it renders had no rule at all -- it would have shipped as
    // unstyled text next to the title.
    for (const selector of [".dialog-title-main", ".dialog-back"]) {
      assert.ok(CSS.includes(`${selector} {`), `${selector} has no styling`);
    }
    const back = CSS.split(".dialog-back {")[1].split("}")[0];
    // Quieter than the title beside it, and it has to respond to the keyboard
    // as well as the mouse.
    assert.match(back, /color: var\(--neo-fg-1[0-9]\)/);
    assert.match(CSS, /\.dialog-back:hover, \.dialog-back:focus-visible/);
  });

  test("a dialog that hand-rolls its chrome uses the same markup", () => {
    // Three settings pages build their own title row instead of using `Modal`.
    // They are the ones that would grow a second look for going back.
    for (const file of ["AccountSettings.jsx", "RulesProfiles.jsx", "AgentSettings.jsx"]) {
      const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
      assert.match(source, /backLabel/, `${file} cannot be given a way back`);
      assert.match(source, /className="dialog-back"/, `${file} draws its own back affordance`);
      assert.match(source, /className="dialog-title-main"/, `${file} does not group title and back`);
    }
  });
});

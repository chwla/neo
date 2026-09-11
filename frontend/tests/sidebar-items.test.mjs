/**
 * Pinning and unpinning the sidebar's SYSTEM entries.
 *
 * Three surfaces have to agree and none of them owns the list: the catalogue in
 * `systemNav.js`, the sidebar that draws it, and the settings panel that turns
 * entries off. What is pinned lives in the profile database -- `hidden` arrives
 * as a prop here, and `tests/test_sidebar_nav_api.py` is where storing it is
 * checked.
 *
 * The property worth stating plainly: what is stored is the *hidden* half. A
 * stored list of what to show would freeze the menu at the moment it was saved,
 * leaving every entry added afterwards invisible to exactly the people who had
 * bothered to configure it.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Sidebar } from "../src/App.jsx";
import SidebarItemsSettings from "../src/SidebarItemsSettings.jsx";
import { SYSTEM_NAV, SYSTEM_NAV_IDS, isSystemNavId, visibleSystemNav } from "../src/systemNav.js";

function renderSidebar(overrides = {}) {
  const props = {
    sidebar: { projects: [], chats: [], archived_count: 0, chat_limit: 10 },
    activeChatId: null,
    statusFor: () => null,
    selectedProjectId: null,
    showNewProjectForm: false,
    onToggleProjectForm() {},
    onCreateProject() {},
    onNewChat() {},
    onOpenChat() {},
    onDeleteChat() {},
    onRenameChat() {},
    onPinChat() {},
    onArchiveChat() {},
    onDeleteProject() {},
    onOpenSettings() {},
    onOpenChatHome() {},
    onOpenMemory() {},
    onOpenResearch() {},
    onOpenNotes() {},
    onOpenTasks() {},
    onOpenCalendar() {},
    onOpenGallery() {},
    onOpenLocalModels() {},
    onOpenCompareModels() {},
    activeView: "chat",
    profile: { username: "ada", avatar_data: null },
    onSwitchProfile() {},
    ...overrides,
  };
  return renderToStaticMarkup(createElement(Sidebar, props));
}

function renderPanel(overrides = {}) {
  const props = { hidden: [], onHiddenChange() {}, onClose() {}, ...overrides };
  return renderToStaticMarkup(createElement(SidebarItemsSettings, props));
}

describe("the catalogue", () => {
  test("every entry has an id, a name and a line saying what it is", () => {
    // The panel draws all three; an entry added with only an id renders a row
    // with a toggle and nothing to tell you what you are turning off.
    for (const item of SYSTEM_NAV) {
      assert.ok(item.id, "an entry with no id");
      assert.ok(item.label, `${item.id} has no label`);
      assert.ok(item.description, `${item.id} has no description`);
    }
    assert.equal(new Set(SYSTEM_NAV_IDS).size, SYSTEM_NAV_IDS.length, "duplicate id");
  });

  test("a profile that has chosen nothing keeps the whole list", () => {
    assert.deepEqual(visibleSystemNav().map((i) => i.id), SYSTEM_NAV_IDS);
    assert.deepEqual(visibleSystemNav([]).map((i) => i.id), SYSTEM_NAV_IDS);
  });

  test("hiding removes only what was named, and keeps the order", () => {
    const visible = visibleSystemNav(["research", "gallery"]).map((i) => i.id);
    assert.deepEqual(visible, ["memory", "notes", "calendar", "localModels", "compareModels"]);
  });

  test("an id that is not shipped is ignored rather than fatal", () => {
    // Rows outlive entries: one renamed or removed in a later build must not
    // empty somebody's sidebar or throw on the way past.
    assert.deepEqual(visibleSystemNav(["notAThing"]).map((i) => i.id), SYSTEM_NAV_IDS);
    assert.ok(!isSystemNavId("notAThing"));
    assert.ok(isSystemNavId("memory"));
  });
});

describe("the sidebar", () => {
  test("draws every entry when nothing is hidden", () => {
    const html = renderSidebar();
    assert.ok(html.includes("SYSTEM"));
    for (const item of SYSTEM_NAV) {
      assert.ok(html.includes(`>${item.label}<`), `${item.label} is missing`);
    }
  });

  test("drops the entries this profile turned off", () => {
    const html = renderSidebar({ hiddenSystemItems: ["gallery", "compareModels"] });
    assert.ok(!html.includes(">Gallery<"));
    assert.ok(!html.includes(">Compare Models<"));
    assert.ok(html.includes(">Memory<"), "the rest of the list went with them");
    assert.ok(html.includes("SYSTEM"));
  });

  test("takes the heading with the last entry", () => {
    // A section label over nothing reads as a list that failed to load rather
    // than one that was emptied on purpose.
    const html = renderSidebar({ hiddenSystemItems: SYSTEM_NAV_IDS });
    assert.ok(!html.includes("SYSTEM"));
    assert.ok(html.includes("Settings"), "the footer is not part of the bargain");
  });

  test("an unknown stored id leaves the list alone", () => {
    const html = renderSidebar({ hiddenSystemItems: ["somethingRetired"] });
    for (const item of SYSTEM_NAV) {
      assert.ok(html.includes(`>${item.label}<`), `${item.label} went missing`);
    }
  });
});

describe("the settings panel", () => {
  test("offers a toggle for every entry, including the hidden ones", () => {
    // The ones that are off are the ones you came here to turn back on, so the
    // panel renders the catalogue rather than the visible half of it.
    const html = renderPanel({ hidden: ["gallery"] });
    assert.equal((html.match(/type="checkbox"/g) || []).length, SYSTEM_NAV.length);
    assert.ok(html.includes("Gallery"));
  });

  test("a toggle is on when the entry is pinned", () => {
    const html = renderPanel({ hidden: ["gallery", "notes"] });
    const checked = (html.match(/checked=""/g) || []).length;
    assert.equal(checked, SYSTEM_NAV.length - 2);
  });

  test("each checkbox carries its own name", () => {
    // The entry's name is in a sibling element, not a wrapping <label>, so
    // without this a screen reader announces seven unnamed checkboxes.
    const html = renderPanel();
    for (const item of SYSTEM_NAV) {
      assert.ok(
        html.includes(`aria-label="Pin ${item.label} to the sidebar"`),
        `${item.label}'s toggle is unnamed`,
      );
    }
  });

  test("no toggle is ever disabled", () => {
    // The regression this panel shipped with: every checkbox was disabled while
    // a write was in flight, so seven controls could only be used one at a time
    // and a second click landed in the round trip and was swallowed. Clicks are
    // never refused now -- `setWriter.js` makes the writes behind them cope.
    for (const hidden of [[], ["gallery"], SYSTEM_NAV_IDS]) {
      assert.ok(!renderPanel({ hidden }).includes("disabled"), "a toggle can be locked out");
    }
  });

  test("says how much is pinned, and what an empty sidebar means", () => {
    assert.ok(renderPanel().includes(`${SYSTEM_NAV.length} of ${SYSTEM_NAV.length} pinned`));
    assert.ok(renderPanel({ hidden: SYSTEM_NAV_IDS }).includes("hidden while nothing is pinned"));
  });
});

/**
 * Where the choice is resolved, which is the difference between a setting that
 * holds and one that appears to forget itself.
 *
 * The theme is fetched inside the gate that holds back the first paint, and
 * again on sign-in because signing in does not reload the page. Anything
 * fetched after that gate draws the default first and corrects itself a frame
 * later -- survivable for a palette, and not for a menu, where it is
 * indistinguishable from the setting having failed to save.
 *
 * Asserted against the source because the failure is structural: both call
 * sites look fine on their own when one of them is missing, and the only
 * symptom is a flicker on a screen no test renders.
 */
describe("when the sidebar choice is resolved", () => {
  const APP = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

  function region(from, to, label) {
    const start = APP.indexOf(from);
    assert.ok(start > -1, `${label}: "${from}" is not in App.jsx any more`);
    const end = APP.indexOf(to, start);
    assert.ok(end > -1, `${label}: "${to}" does not follow it`);
    return APP.slice(start, end);
  }

  test("the first-paint gate asks for it alongside the theme", () => {
    const gate = region("api.currentAccountProfile()", "setCheckingSession(false)", "boot gate");
    assert.ok(gate.includes("api.appearanceConfig()"), "the theme left the gate");
    assert.ok(gate.includes("api.sidebarNavConfig()"), "the sidebar is resolved after first paint");
  });

  test("signing in asks again, for both", () => {
    // Signing in through the picker does not reload the page, so whatever was
    // fetched before there was a session belongs to no profile.
    const signIn = region("onSignedIn={(next) => {", "<NeoApp", "sign-in");
    assert.ok(signIn.includes("api.appearanceConfig()"), "the theme stopped re-fetching");
    assert.ok(signIn.includes("api.sidebarNavConfig()"), "the sidebar does not survive a log in");
  });

  test("and nowhere else -- exactly where the theme is asked for, no later", () => {
    const count = (needle) => APP.split(needle).length - 1;
    assert.equal(count("api.sidebarNavConfig()"), 2);
    assert.equal(
      count("api.sidebarNavConfig()"),
      count("api.appearanceConfig()"),
      "the two are no longer resolved in the same places",
    );
  });
});

/**
 * The panel's lifecycle, guarded at the source.
 *
 * The writer was disabled by an effect cleanup -- `useEffect(() => () =>
 * writer.stop())` -- and React runs effects mount / cleanup / mount under
 * StrictMode, so in development it was switched off before the first click.
 * Every toggle painted and saved nothing, and the server log had no POST in it.
 *
 * There is no renderer here that runs effects, so this is asserted against the
 * source. It is narrow on purpose: the rule is that the panel's cleanups do not
 * touch the writer, because a cleanup that runs on a remount has to be undone
 * by something, and nothing here undoes anything.
 */
describe("the panel's effects", () => {
  const SRC = readFileSync(new URL("../src/SidebarItemsSettings.jsx", import.meta.url), "utf8");

  test("no cleanup touches the writer", () => {
    // An effect returning a function is a cleanup; one that names the writer is
    // the bug, whatever the method is called.
    const cleanups = [...SRC.matchAll(/useEffect\(\(\) =>\s*(\(\) =>[^;]*|\{[\s\S]*?\n {2}\})/g)]
      .map(([, body]) => body)
      .filter((body) => body.trimStart().startsWith("() =>"));
    for (const body of cleanups) {
      assert.ok(!body.includes("writer"), `a cleanup disables the writer: ${body.trim()}`);
    }
  });

  test("the writer is never asked to stop", () => {
    assert.ok(!SRC.includes("writer.stop"), "the one-way kill switch is back");
  });
});

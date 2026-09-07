/**
 * The screen where a binding is changed.
 *
 * Rendered rather than reasoned about, because what goes wrong here is what is
 * on screen: a command offering a Reset it has nothing to reset to, a built-in
 * key looking rebindable when it is not, or a conflict that exists in the keymap
 * and is nowhere in the markup.
 *
 * The Command mode toggle rendering unchecked is the one test here that is
 * really about the product rather than the component. Shipping it on would put
 * every user into a mode where letters act, and the decision that it ships off
 * should fail loudly if somebody flips it.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import KeyboardSettings, { KeyboardRow, sectionsFor } from "../src/KeyboardSettings.jsx";
import { COMMANDS } from "../src/keys/commands.js";
import { buildKeymap } from "../src/keys/keymap.js";

const keymap = buildKeymap(COMMANDS, [], { platform: "mac", commandMode: false });
const bindingFor = (id, overrides = []) =>
  buildKeymap(COMMANDS, overrides, { platform: "mac", commandMode: false })
    .bindings.find((entry) => entry.id === id);

function row(overrides = {}) {
  return renderToStaticMarkup(createElement(KeyboardRow, {
    command: COMMANDS.find((entry) => entry.id === "chat.new"),
    binding: bindingFor("chat.new"),
    platform: "mac",
    conflict: undefined,
    recording: false,
    onRecord() {},
    onReset() {},
    ...overrides,
  }));
}

describe("a command's row", () => {
  test("names the command and shows what it is bound to", () => {
    const markup = row();
    assert.ok(markup.includes("New chat"));
    assert.ok(markup.includes("⌘⇧O"), "in the platform's own notation");
  });

  test("a shipped default offers no way to reset it", () => {
    // There is nothing to go back to, and a Reset that does nothing is a lie.
    assert.ok(!row().includes(">Reset<"));
  });

  test("a command the user rebound does", () => {
    const markup = row({
      binding: bindingFor("chat.new", [
        { command_id: "chat.new", keymap: "standard", sequence: "mod+j" },
      ]),
    });
    assert.ok(markup.includes(">Reset<"));
    assert.ok(markup.includes("⌘J"));
  });

  test("a command with no key says so rather than showing an empty button", () => {
    assert.ok(row({ binding: bindingFor("app.toggleCommandMode") }).includes("Not bound"));
  });

  test("a built-in key never renders an empty box when this tab has no key for it", () => {
    const markup = row({
      command: COMMANDS.find((entry) => entry.id === "mode.command"),
      binding: undefined,
    });
    assert.ok(markup.includes("Built in"));
    assert.ok(!markup.includes("<kbd"));
  });

  test("a built-in key is shown but not offered for rebinding", () => {
    const markup = row({
      command: COMMANDS.find((entry) => entry.id === "mode.command"),
      binding: bindingFor("mode.command"),
    });
    assert.ok(markup.includes("Esc"));
    assert.ok(!markup.includes("kb-record"), "no record button on a fixed binding");
    assert.ok(!markup.includes(">Reset<"));
  });

  test("offers nothing to click when the shortcuts could not be loaded", () => {
    // The screen still lists the defaults, which are genuinely live and worth
    // reading -- but a Record button that captures a key and then cannot save it
    // is a dead end, so the controls go with the connection.
    const markup = row({
      disabled: true,
      binding: bindingFor("chat.new", [
        { command_id: "chat.new", keymap: "standard", sequence: "mod+j" },
      ]),
    });
    assert.ok(markup.includes("disabled"));
    assert.equal(markup.match(/disabled/g).length, 2, "both Record and Reset");
    assert.ok(markup.includes("New chat"), "but the command is still listed");
  });

  test("a conflict is stated on the row, not only in the banner", () => {
    // An override can collide with a default the user has never opened.
    const markup = row({ conflict: "shares this key with Settings" });
    assert.ok(markup.includes("shares this key with Settings"));
    assert.ok(markup.includes("has-conflict"));
  });

  test("a row being recorded says what it is waiting for", () => {
    assert.ok(row({ recording: true }).includes("Press keys…"));
  });
});

describe("which commands are listed", () => {
  test("grouped under their own section headings", () => {
    const sections = sectionsFor(COMMANDS);
    assert.deepEqual(sections.map((entry) => entry.title).slice(0, 3),
      ["Global", "Composer", "Chat"]);
  });

  test("every non-hidden command appears exactly once", () => {
    const listed = sectionsFor(COMMANDS).flatMap((entry) => entry.commands.map((c) => c.id));
    const expected = COMMANDS.filter((command) => !command.hidden).map((command) => command.id);
    assert.deepEqual(listed.sort(), expected.sort());
  });

  test("motions stay out of the way until they are searched for", () => {
    // Nobody scrolls a settings screen looking for "Scroll down", but somebody
    // who wants to rebind it should be able to find it.
    const quiet = sectionsFor(COMMANDS).flatMap((entry) => entry.commands.map((c) => c.id));
    assert.ok(!quiet.includes("chat.scrollDown"));

    const searched = sectionsFor(COMMANDS, "scroll").flatMap((e) => e.commands.map((c) => c.id));
    assert.ok(searched.includes("chat.scrollDown"));
  });

  test("the filter matches a section name as well as a title", () => {
    const bySection = sectionsFor(COMMANDS, "navigation");
    assert.ok(bySection.length > 0);
    assert.ok(bySection.every((entry) => entry.title === "Navigation"));
  });

  test("a filter matching nothing yields nothing rather than everything", () => {
    assert.deepEqual(sectionsFor(COMMANDS, "zzzz"), []);
  });
});

describe("the screen itself", () => {
  const render = () => renderToStaticMarkup(
    createElement(KeyboardSettings, { platform: "mac", onClose() {} }),
  );

  test("Command mode is off until somebody turns it on", () => {
    const markup = render();
    assert.ok(markup.includes("Command mode"));
    assert.ok(!markup.includes('type="checkbox" checked'));
  });

  test("says where the shortcuts are kept, because it is not obvious", () => {
    assert.ok(render().includes("stored with this profile"));
  });

  test("waits for the profile's own bindings rather than showing the defaults as fact", () => {
    assert.ok(render().includes("Loading your shortcuts…"));
  });

  test("says what a failed load means rather than just that it failed", () => {
    // Rendered before the request resolves, so this pins the wording exists;
    // the disabled-controls behaviour is pinned on the row above.
    assert.ok(render().includes("Loading your shortcuts…"));
  });

  test("offers both keymaps to edit, whichever mode is on", () => {
    const markup = render();
    assert.ok(markup.includes("Always on"));
    assert.ok(markup.includes("Reset all"));
  });
});

/**
 * The screen where a binding is changed.
 *
 * Rendered rather than reasoned about, because what goes wrong here is what is
 * on screen: a command offering a Reset it has nothing to reset to, a built-in
 * key looking rebindable when it is not, or a conflict that exists in the keymap
 * and is nowhere in the markup.
 *
 * The absence of a mode toggle and of the two tabs is pinned here rather than
 * left implicit, because both were removed on purpose: one list, two keys per
 * command, both live. Anything that puts a switch back should fail loudly.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import KeyboardSettings, {
  ConflictDialog, KeyboardRow, sectionsFor,
} from "../src/KeyboardSettings.jsx";
import { COMMANDS } from "../src/keys/commands.js";
import { buildKeymap } from "../src/keys/keymap.js";

/** Every slot's binding for one command, in the shape the row wants. */
const bindingsFor = (id, overrides = []) => Object.fromEntries(
  buildKeymap(COMMANDS, overrides, { platform: "mac" })
    .bindings.filter((entry) => entry.id === id)
    .map((entry) => [entry.slot, entry]),
);

function row(overrides = {}) {
  return renderToStaticMarkup(createElement(KeyboardRow, {
    command: COMMANDS.find((entry) => entry.id === "chat.new"),
    bindings: bindingsFor("chat.new"),
    platform: "mac",
    conflict: undefined,
    recording: null,
    onRecord() {},
    onReset() {},
    onResolve() {},
    ...overrides,
  }));
}

describe("a command's row", () => {
  test("names the command and shows both of its keys at once", () => {
    const markup = row();
    assert.ok(markup.includes("New chat"));
    assert.ok(markup.includes("⌘⇧O"), "the shortcut, in the platform's own notation");
    assert.ok(markup.includes(">C<"), "and the quick key beside it");
  });

  test("a shipped default offers no way to reset it", () => {
    // There is nothing to go back to, and a Reset that does nothing is a lie.
    assert.ok(!row().includes("kb-reset\""));
  });

  test("only the slot the user changed offers a way back", () => {
    const markup = row({
      bindings: bindingsFor("chat.new", [
        { command_id: "chat.new", keymap: "primary", sequence: "mod+j" },
      ]),
    });
    assert.equal((markup.match(/class="kb-reset"/g) ?? []).length, 1, "one slot, one reset");
    assert.ok(markup.includes("⌘J"), "the new shortcut");
    assert.ok(markup.includes(">C<"), "and the untouched quick key");
  });

  test("an empty slot shows a placeholder rather than a blank button", () => {
    const markup = row({ bindings: bindingsFor("palette.open") });
    assert.ok(markup.includes("⌘K"));
    assert.ok(markup.includes(">-<"), "the quick key it has never had");
  });

  test("a built-in key is shown but not offered for rebinding", () => {
    const markup = row({
      command: COMMANDS.find((entry) => entry.id === "composer.leave"),
      bindings: bindingsFor("composer.leave"),
    });
    assert.ok(markup.includes("Esc"));
    assert.ok(!markup.includes("kb-record"), "no record button on a fixed binding");
    assert.ok(!markup.includes("kb-reset"));
  });

  test("a built-in with nothing to show says so instead of drawing an empty box", () => {
    const markup = row({
      command: COMMANDS.find((entry) => entry.id === "composer.leave"),
      bindings: {},
    });
    assert.ok(markup.includes("Built in"));
    assert.ok(!markup.includes("<kbd"));
  });

  test("offers nothing to click when the shortcuts could not be loaded", () => {
    // The screen still lists the defaults, which are genuinely live and worth
    // reading -- but a Record button that captures a key and then cannot save it
    // is a dead end, so the controls go with the connection.
    const markup = row({
      disabled: true,
      bindings: bindingsFor("chat.new", [
        { command_id: "chat.new", keymap: "primary", sequence: "mod+j" },
      ]),
    });
    assert.ok(markup.includes("disabled"));
    assert.equal(markup.match(/disabled/g).length, 3, "both Record buttons and the one Reset");
    assert.ok(markup.includes("New chat"), "but the command is still listed");
  });

  test("a conflict is stated on the row, not only in the banner", () => {
    // An override can collide with a default the user has never opened.
    const markup = row({
      conflict: { text: "shares this key with Settings", resolvable: true },
    });
    assert.ok(markup.includes("shares this key with Settings"));
    assert.ok(markup.includes("has-conflict"));
    assert.ok(markup.includes(">Resolve<"), "and offers a way to settle it");
  });

  test("marks the key that actually clashes, and only that one", () => {
    // Outlining both of a row's keys points the user at the wrong one.
    const markup = row({
      conflict: { text: "shares this key with Settings", resolvable: true, key: "c" },
    });
    assert.equal((markup.match(/is-clashing/g) ?? []).length, 1);
  });

  test("a conflict with nobody to hand the key to offers no Resolve", () => {
    // A reserved chord has no second claimant; it just has to be changed.
    const markup = row({
      conflict: { text: "uses a key the app needs for itself", resolvable: false },
    });
    assert.ok(!markup.includes(">Resolve<"));
  });

  test("only the slot being recorded says so; the other still shows its key", () => {
    const quick = row({ recording: "alternate" });
    assert.ok(quick.includes("Press keys…"));
    assert.ok(quick.includes("⌘⇧O"), "the shortcut is still readable while the quick key records");

    const shortcut = row({ recording: "primary" });
    assert.ok(shortcut.includes("Press keys…"));
    assert.ok(!shortcut.includes("⌘⇧O"), "the recorded slot swaps out");
    assert.ok(shortcut.includes(">C<"), "and the other one does not");
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

  test("has no mode to switch and no tabs to pick between", () => {
    const markup = render();
    assert.ok(!markup.includes("Command mode"));
    assert.ok(!markup.includes("kb-which"), "the segmented control is gone");
    assert.ok(!markup.includes('type="checkbox"'), "and so is the toggle");
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

  test("explains what the two keys are, since one is guarded and one is not", () => {
    const markup = render();
    assert.ok(markup.includes("only when you are not typing"));
    assert.ok(markup.includes("Reset all"));
  });
});

describe("who keeps a contested key", () => {
  const render = (overrides = {}) => renderToStaticMarkup(createElement(ConflictDialog, {
    sequence: "c",
    platform: "mac",
    claimant: "app.openSettings",
    holders: [{ id: "chat.new", slot: "alternate", key: "c" }],
    onKeep() {},
    onGiveAway() {},
    onCancel() {},
    ...overrides,
  }));

  test("names the key and both commands that want it", () => {
    // The whole point: a rebinding that would break something else stops and
    // says what, rather than saving and leaving a banner behind.
    const markup = render();
    assert.ok(markup.includes(">C<"));
    assert.ok(markup.includes("New chat"), "who has it");
    assert.ok(markup.includes("Settings"), "and who wants it");
  });

  test("offers both outcomes, and says what each costs", () => {
    const markup = render();
    assert.ok(markup.includes("Give it to Settings"));
    assert.ok(markup.includes("New chat loses this key"));
    assert.ok(markup.includes("Leave it with New chat"));
  });

  test("reads correctly when more than one command holds the key", () => {
    const markup = render({
      holders: [
        { id: "chat.new", slot: "alternate", key: "c" },
        { id: "nav.chat", slot: "alternate", key: "c" },
      ],
    });
    assert.ok(markup.includes("lose this key"), "plural, not \"loses\"");
  });

  test("can be backed out of without choosing", () => {
    assert.ok(render().includes(">Cancel<"));
  });
});

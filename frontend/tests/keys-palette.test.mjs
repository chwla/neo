/**
 * What the palette offers, and in what order.
 *
 * The palette is how somebody finds a command they could not have guessed the
 * name of, so the ranking matters more than the matching: the tests below are
 * mostly about which of several plausible answers comes first. The two filters --
 * commands whose screen is not showing, and commands nothing can carry out -- are
 * here because a palette that lists dead rows is worse than a shorter one.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import CommandPalette from "../src/CommandPalette.jsx";
import { COMMANDS } from "../src/keys/commands.js";
import { buildKeymap } from "../src/keys/keymap.js";
import { rankCommands } from "../src/keys/paletteSearch.js";

const command = (id, title, extra = {}) => ({ id, title, section: "Test", keys: "", ...extra });
const titles = (list) => list.map((entry) => entry.title);

const CATALOGUE = [
  command("a.new", "New chat", { keywords: "start begin" }),
  command("a.notes", "Go to Notes"),
  command("a.gallery", "Go to Gallery", { when: ["gallery"] }),
  command("a.settings", "Settings"),
  command("a.scroll", "Scroll down", { hidden: true }),
  command("a.escape", "Leave the composer", { fixed: true }),
];

describe("with no query", () => {
  test("everything in scope is offered, in catalogue order", () => {
    assert.deepEqual(titles(rankCommands(CATALOGUE, "", new Set())),
      ["New chat", "Go to Notes", "Settings"]);
  });

  test("whitespace is not a query", () => {
    assert.equal(rankCommands(CATALOGUE, "   ", new Set()).length, 3);
  });
});

describe("what never appears", () => {
  test("a motion, which is rebindable but not worth running from a list", () => {
    assert.ok(!titles(rankCommands(CATALOGUE, "scroll", new Set())).includes("Scroll down"));
  });

  test("a command the engine implements directly rather than through the keymap", () => {
    assert.ok(!titles(rankCommands(CATALOGUE, "leave", new Set())).includes("Leave the composer"));
  });

  test("a command whose screen is not showing", () => {
    assert.ok(!titles(rankCommands(CATALOGUE, "go", new Set())).includes("Go to Gallery"));
    assert.ok(titles(rankCommands(CATALOGUE, "go", new Set(["gallery"]))).includes("Go to Gallery"));
  });

  test("a command nothing has registered a handler for", () => {
    const offered = rankCommands(CATALOGUE, "", new Set(), {
      isAvailable: (id) => id !== "a.settings",
    });
    assert.ok(!titles(offered).includes("Settings"));
  });
});

describe("ranking", () => {
  test("an exact title beats everything", () => {
    const list = [command("a.one", "New"), command("a.two", "New chat")];
    assert.deepEqual(titles(rankCommands(list, "new", new Set())), ["New", "New chat"]);
  });

  test("a title that starts with the query beats one that merely contains it", () => {
    const list = [command("a.one", "Open the notes drawer"), command("a.two", "Notes settings")];
    assert.deepEqual(titles(rankCommands(list, "notes", new Set())),
      ["Notes settings", "Open the notes drawer"]);
  });

  test("a word inside the title counts as a start", () => {
    // "Go to Notes" should be found by typing "notes", not only by typing "go".
    assert.deepEqual(titles(rankCommands(CATALOGUE, "notes", new Set())), ["Go to Notes"]);
  });

  test("a keyword finds a command the title would not", () => {
    assert.deepEqual(titles(rankCommands(CATALOGUE, "begin", new Set())), ["New chat"]);
  });

  test("initials find a multi-word title", () => {
    assert.ok(titles(rankCommands(CATALOGUE, "gtn", new Set())).includes("Go to Notes"));
  });

  test("a tighter subsequence outranks a looser one", () => {
    const list = [
      command("a.loose", "Go absolutely nowhere at all"),
      command("a.tight", "Gn"),
    ];
    assert.deepEqual(titles(rankCommands(list, "gn", new Set())), ["Gn", "Go absolutely nowhere at all"]);
  });

  test("a query that matches nothing returns nothing", () => {
    assert.deepEqual(rankCommands(CATALOGUE, "zzzz", new Set()), []);
  });
});

describe("over the real catalogue", () => {
  test("the obvious query puts the obvious command first", () => {
    const scopes = new Set(["chat"]);
    assert.equal(rankCommands(COMMANDS, "new chat", scopes)[0]?.id, "chat.new");
    assert.equal(rankCommands(COMMANDS, "settings", scopes)[0]?.id, "app.openSettings");
    assert.equal(rankCommands(COMMANDS, "palette", scopes)[0]?.id, "palette.open");
  });

  test("the keyboard settings are findable from the palette", () => {
    // Where every binding is changed, so it has to be reachable without knowing
    // a binding.
    assert.ok(rankCommands(COMMANDS, "shortcuts", new Set())
      .some((entry) => entry.id === "app.showKeyboardHelp"));
  });

  test("no motion leaks into the list from any screen", () => {
    for (const view of ["chat", "notes", "gallery"]) {
      for (const entry of rankCommands(COMMANDS, "", new Set([view]))) {
        assert.ok(!entry.hidden, `${entry.id} is hidden but was offered`);
      }
    }
  });
});

describe("the palette on screen", () => {
  function render(overrides = {}) {
    return renderToStaticMarkup(createElement(CommandPalette, {
      keymap: buildKeymap(COMMANDS, [], { platform: "mac" }),
      scopes: new Set(["chat"]),
      platform: "mac",
      isAvailable: () => true,
      onClose() {},
      ...overrides,
    }));
  }

  test("opens with a search box and the commands for this screen", () => {
    const markup = render();
    assert.ok(markup.includes('aria-label="Search commands"'));
    assert.ok(markup.includes("New chat"));
    assert.ok(markup.includes("Command palette"));
  });

  test("shows each command's key next to it", () => {
    // The palette is where people learn the bindings exist at all.
    const markup = render();
    assert.ok(markup.includes("<kbd"));
    assert.ok(markup.includes("⌘K"), "and in the platform's own notation");
  });

  test("a command with no key gets a row but no kbd", () => {
    const markup = render({ commands: [
      { id: "a.unbound", title: "Something unbound", section: "Global", keys: "" },
    ] });
    assert.ok(markup.includes("Something unbound"));
    assert.ok(!markup.includes("<kbd"));
  });

  test("the first row starts selected, so Enter runs something sensible", () => {
    assert.ok(render().includes('aria-selected="true"'));
  });

  test("says so rather than showing an empty box when nothing is available", () => {
    const markup = render({ isAvailable: () => false });
    assert.ok(markup.includes("No command matches that."));
    assert.ok(!markup.includes('role="listbox"'));
  });

  test("another screen's commands are not offered", () => {
    const markup = render({ scopes: new Set(["notes"]) });
    assert.ok(markup.includes("Save note"));
    assert.ok(!markup.includes("New chat"));
  });
});

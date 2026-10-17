/**
 * Neo on a phone: below 640px the sidebar is a drawer opened from a bar on
 * top. What must hold is that nothing the desktop sidebar reaches becomes
 * unreachable: the drawer is the whole sidebar (every SYSTEM entry, Settings,
 * which leads to the workspace screens, and the profile), even for someone
 * who minimised the sidebar on a wider screen.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Sidebar } from "../src/App.jsx";
import { MobileNavBar, NARROW_SHELL_QUERY, shellTitle } from "../src/MobileShell.jsx";
import { SYSTEM_NAV } from "../src/systemNav.js";

const noop = () => {};
function renderSidebar(overrides = {}) {
  const props = {
    sidebar: { projects: [{ id: "p1", name: "Launch", chats: [] }], chats: [{ id: 1, title: "Hello" }], archived_count: 0 },
    activeChatId: null,
    statusFor: () => null,
    selectedProjectId: null,
    showNewProjectForm: false,
    activeView: "chat",
    profile: { username: "ada", avatar_data: null },
    ...Object.fromEntries(
      ["onToggleProjectForm", "onCreateProject", "onNewChat", "onOpenChat", "onDeleteChat", "onRenameChat", "onPinChat", "onArchiveChat",
        "onDeleteProject", "onOpenSettings", "onOpenChatHome", "onOpenMemory", "onOpenResearch", "onOpenNotes", "onOpenTasks",
        "onOpenCalendar", "onOpenGallery", "onOpenLocalModels", "onOpenCompareModels", "onSwitchProfile", "onToggleCollapsed"].map((name) => [name, noop]),
    ),
    ...overrides,
  };
  return renderToStaticMarkup(createElement(Sidebar, props));
}

describe("the phone drawer", () => {
  test("is the whole sidebar: every SYSTEM entry, Settings, the profile, chats and projects", () => {
    const html = renderSidebar({ drawer: { open: true, onClose: noop } });
    for (const item of SYSTEM_NAV) assert.ok(html.includes(`>${item.label}<`), item.label);
    assert.ok(html.includes(">Settings<"));
    assert.ok(html.includes('aria-label="Log out and choose another profile"'));
    assert.ok(html.includes(">Hello<") && html.includes(">Launch<"));
    assert.ok(html.includes('id="neo-nav-drawer"'));
    assert.ok(html.includes('aria-label="Close navigation"'));
    assert.ok(!html.includes("Minimise sidebar"), "a drawer offers closing, not minimising");
  });

  test("stays whole for someone who minimised the sidebar on a wider screen", () => {
    const html = renderSidebar({ collapsed: true, drawer: { open: false, onClose: noop } });
    assert.ok(!html.includes("neo-sidebar-rail"));
    for (const item of SYSTEM_NAV) assert.ok(html.includes(`>${item.label}<`), item.label);
  });

  test("beside a screen, the sidebar is unchanged", () => {
    const html = renderSidebar();
    assert.ok(!html.includes("neo-nav-drawer"));
    assert.ok(html.includes("Minimise sidebar"));
    assert.ok(renderSidebar({ collapsed: true }).includes("neo-sidebar-rail"));
  });
});

describe("the phone bar", () => {
  test("names where you are and controls the drawer", () => {
    const html = renderToStaticMarkup(createElement(MobileNavBar, { title: "Notes", open: false, onToggle: noop }));
    assert.ok(html.includes('aria-controls="neo-nav-drawer"'));
    assert.ok(html.includes('aria-expanded="false"'));
    assert.ok(html.includes(">Notes<"));
    assert.equal(shellTitle("notes"), "Notes");
    assert.equal(shellTitle("repos"), "Repositories");
    assert.equal(shellTitle("chat", "Trip plan"), "Trip plan");
    assert.equal(shellTitle("chat"), "Neo");
  });

  test("the CSS switches at the same width the code does, and no longer hides navigation", () => {
    const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
    const width = NARROW_SHELL_QUERY.match(/max-width: (\d+)px/)[1];
    assert.ok(css.includes(`@media (max-width: ${width}px) {\n  .neo-app { flex-direction: column;`));
    assert.ok(!/\.system-nav, \.sidebar-footer[^}]*display: none/.test(css), "the SYSTEM nav and footer stay reachable");
  });
});

describe("touch screens wide enough for the sidebar", () => {
  test("the sidebar offers the palette (shown by CSS on coarse pointers only)", () => {
    const html = renderSidebar({ onOpenPalette: noop });
    assert.ok(html.includes("sidebar-palette-button"));
    assert.ok(renderSidebar({ onOpenPalette: noop, collapsed: true }).includes("sidebar-palette-button"));
    // In the phone drawer the bar already has it.
    assert.ok(!renderSidebar({ onOpenPalette: noop, drawer: { open: true, onClose: noop } }).includes("sidebar-palette-button"));
    const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
    assert.ok(/\.sidebar-palette-button \{ display: none; \}\n@media \(pointer: coarse\) and \(min-width: 641px\)/.test(css));
  });
});

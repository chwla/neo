/**
 * The SYSTEM section of the sidebar: every place it can take you, and which of
 * them this profile has asked to keep.
 *
 * The list lived inline in `Sidebar` until it became something a person could
 * edit. Two screens need it now -- the sidebar that renders it and the settings
 * panel that turns entries off -- and a catalogue that exists twice is a
 * catalogue that disagrees with itself the first time an entry is added.
 *
 * The ids are the frontend's own, carried through to the server unchanged
 * rather than mapped to snake_case on the way. A mapping layer between two
 * lists that must already agree is one more place for them to stop agreeing,
 * and these ids are opaque to everything that stores them.
 *
 * `app/services/sidebar_nav.py` holds the same ids so a write can be refused
 * before it is stored; `tests/test_sidebar_nav_api.py` holds the two together.
 */

export const SYSTEM_NAV = [
  { id: "memory", label: "Memory", description: "Durable personal context" },
  { id: "research", label: "Research", description: "Sources and research sessions" },
  { id: "notes", label: "Notes", description: "Saved working notes" },
  { id: "calendar", label: "Calendar", description: "Your schedule, and what Neo has added to it" },
  { id: "gallery", label: "Gallery", description: "Images Neo has seen" },
  { id: "localModels", label: "Local Models", description: "Models installed on this computer" },
  {
    id: "compareModels",
    label: "Compare Models",
    description: "Run one workload through several models side by side",
  },
];

export const SYSTEM_NAV_IDS = SYSTEM_NAV.map((item) => item.id);

export function isSystemNavId(id) {
  return SYSTEM_NAV_IDS.includes(id);
}

/**
 * The entries to draw, given the ones this profile has turned off.
 *
 * Hidden is the stored half rather than visible, and that decision is the whole
 * forward-compatibility story. A stored list of what to *show* freezes the menu
 * at the moment it was saved: every entry added afterwards is missing from that
 * list, so it would be invisible to everyone who had ever opened this panel,
 * and visible only to people who never touched it. Storing what to *hide* makes
 * "pinned" the default that new entries inherit, which is also what the panel
 * says it means -- everything is on until you turn it off.
 *
 * An unknown id in the stored list is ignored rather than dropped, because it
 * is almost always an entry that has been renamed or is not shipped in this
 * build. Leaving the row alone means putting the entry back restores the
 * choice, the same way a retired theme id does.
 */
export function visibleSystemNav(hidden = []) {
  const off = new Set(hidden);
  return SYSTEM_NAV.filter((item) => !off.has(item.id));
}

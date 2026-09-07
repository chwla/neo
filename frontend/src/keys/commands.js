/**
 * Every command the keyboard can reach, and what it is bound to out of the box.
 *
 * This file is deliberately data and nothing else. Adding a command is one object
 * literal here -- no handler wiring, no new prop, no change to the API -- because
 * the handler is registered by whichever component owns the state, and the backend
 * stores whatever ids it is given without knowing this list.
 *
 * There are two keymaps. The standard one is what everybody gets: modifier chords
 * only, because a bare letter that acts is a surprise to someone who did not ask
 * for one. The command keymap is what Command mode adds on top, and it is where
 * single keys and the `g` namespace live. A command with no `commandKeys` keeps
 * its standard binding in both.
 *
 * `keys: ""` means unbound by default. Several commands ship that way on purpose:
 * they are worth having in the palette and worth being rebindable, but not worth
 * spending a chord on before anyone asks.
 */

/**
 * Contexts a binding can require. The view tokens are exactly the values the
 * app's own `activeView` already produces, so a command scopes itself to a screen
 * by naming that screen and nothing has to be derived twice.
 *
 * The view tokens are mutually exclusive -- one screen is showing at a time -- and
 * the keymap relies on that to tell a real conflict from two screens each binding
 * "/" to their own search box.
 */
export const VIEW_SCOPES = new Set([
  "chat", "settings", "memory", "research", "notes", "projects", "tasks",
  "calendar", "files", "gallery", "repos", "localModels", "compareModels",
]);

/** Non-view conditions a binding can additionally require. */
export const STATE_SCOPES = new Set(["generating", "hasChat"]);

/** Every token a `when` may contain. "global" means no condition at all. */
export const SCOPES = new Set(["global", ...VIEW_SCOPES, ...STATE_SCOPES]);

/**
 * The catalogue.
 *
 * A few bindings are inherited from modal text editors -- "d d" to delete, "y y"
 * to copy, "g g" for the top. They are kept because the people who turn Command
 * mode on already have them in their fingers, and every one of them is rebindable.
 * The labels stay plain English regardless: the key is borrowed, the vocabulary is
 * not.
 */
export const COMMANDS = [
  // -- Global ---------------------------------------------------------------
  {
    id: "palette.open",
    title: "Command palette",
    section: "Global",
    keys: "mod+k",
    keywords: "search run find action",
  },
  {
    id: "app.toggleSidebar",
    title: "Toggle sidebar",
    section: "Global",
    keys: "mod+b",
    commandKeys: "\\",
    keywords: "hide show collapse",
  },
  {
    id: "app.openSettings",
    title: "Settings",
    section: "Global",
    keys: "mod+,",
    commandKeys: "g s",
    keywords: "preferences options config",
  },
  {
    id: "app.showKeyboardHelp",
    title: "Keyboard shortcuts",
    section: "Global",
    keys: "mod+/",
    commandKeys: "?",
    keywords: "keys bindings help cheatsheet",
  },
  {
    // Deliberately unbound. A key that switches Command mode off is a key that
    // switches it off by accident, and the palette is reachable from either mode.
    id: "app.toggleCommandMode",
    title: "Toggle Command mode",
    section: "Global",
    keys: "",
    keywords: "keyboard mode keys fast",
  },

  // -- Composer -------------------------------------------------------------
  // Only meaningful where the composer is, so all of these are scoped to chat.
  {
    id: "mode.type",
    title: "Type in composer",
    section: "Composer",
    when: ["chat"],
    keys: "",
    commandKeys: "i",
    hidden: true,
  },
  {
    id: "mode.typeAfter",
    title: "Type after the cursor",
    section: "Composer",
    when: ["chat"],
    keys: "",
    commandKeys: "a",
    hidden: true,
  },
  {
    id: "mode.typeEnd",
    title: "Type at the end",
    section: "Composer",
    when: ["chat"],
    keys: "",
    commandKeys: "A",
    hidden: true,
  },
  {
    id: "mode.typeStart",
    title: "Type at the start",
    section: "Composer",
    when: ["chat"],
    keys: "",
    commandKeys: "I",
    hidden: true,
  },
  {
    id: "mode.typeNewLine",
    title: "Type on a new line",
    section: "Composer",
    when: ["chat"],
    keys: "",
    commandKeys: "o",
    hidden: true,
  },
  {
    // Implemented by the engine rather than by a keymap lookup, because Escape has
    // to keep working when everything else is suspended. Listed so it appears in
    // the shortcut list; `fixed` keeps it out of the rebinding flow.
    id: "mode.command",
    title: "Leave the composer",
    section: "Composer",
    when: ["chat"],
    keys: "escape",
    fixed: true,
  },

  // -- Chat -----------------------------------------------------------------
  {
    id: "chat.new",
    // Not mod+n: Chrome and Safari open a browser window before the page is told.
    title: "New chat",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+o",
    commandKeys: "c",
    keywords: "start begin conversation",
  },
  {
    id: "chat.stop",
    title: "Stop generating",
    section: "Chat",
    when: ["chat"],
    keys: "mod+.",
    keywords: "cancel halt abort",
  },
  {
    id: "chat.focusComposer",
    title: "Focus the composer",
    section: "Chat",
    when: ["chat"],
    keys: "mod+i",
    keywords: "message input write",
  },
  {
    id: "chat.nextChat",
    title: "Next chat",
    section: "Chat",
    keys: "mod+alt+down",
    commandKeys: "] c",
    keywords: "switch newer",
  },
  {
    id: "chat.prevChat",
    title: "Previous chat",
    section: "Chat",
    keys: "mod+alt+up",
    commandKeys: "[ c",
    keywords: "switch older",
  },
  {
    id: "chat.copyLast",
    title: "Copy last reply",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+c",
    commandKeys: "y y",
    keywords: "clipboard yank",
  },
  {
    id: "chat.editLast",
    title: "Edit last message",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+e",
    commandKeys: "g e",
    keywords: "change revise",
  },
  {
    id: "chat.regenerate",
    // "g r" would have shadowed navigation to Research from inside the chat view,
    // which is where you spend the day. Doubled instead, like the other two.
    title: "Regenerate reply",
    section: "Chat",
    when: ["chat"],
    keys: "mod+alt+r",
    commandKeys: "r r",
    keywords: "rerun retry again",
  },
  {
    id: "chat.compact",
    title: "Compact conversation",
    section: "Chat",
    when: ["chat"],
    keys: "",
    commandKeys: "g k",
    keywords: "summarize shrink context",
  },
  {
    id: "chat.attach",
    title: "Attach a file",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+u",
    commandKeys: "g u",
    keywords: "upload image document",
  },
  {
    id: "chat.toggleAgentMode",
    title: "Switch between Chat and Agent",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+m",
    commandKeys: "g a",
    keywords: "agent mode",
  },
  {
    id: "chat.deleteCurrent",
    // Safe to double-tap: it opens the existing confirmation rather than deleting.
    title: "Delete this chat",
    section: "Chat",
    when: ["chat"],
    keys: "",
    commandKeys: "d d",
    keywords: "remove discard",
  },
  {
    id: "chat.openFolder",
    title: "Open a folder",
    section: "Chat",
    when: ["chat"],
    keys: "",
    keywords: "workspace directory repo",
  },

  // -- Transcript -----------------------------------------------------------
  // The handler declines when there is nothing to scroll, which leaves the
  // browser's own scrolling untouched rather than replacing it with a worse one.
  {
    id: "chat.scrollTop",
    title: "Top of transcript",
    section: "Transcript",
    when: ["chat"],
    keys: "home",
    commandKeys: "g g",
    hidden: true,
  },
  {
    id: "chat.scrollBottom",
    title: "Bottom of transcript",
    section: "Transcript",
    when: ["chat"],
    keys: "end",
    commandKeys: "G",
    hidden: true,
  },
  {
    id: "chat.scrollDown",
    title: "Scroll down",
    section: "Transcript",
    when: ["chat"],
    keys: "down",
    commandKeys: "j",
    repeatable: true,
    hidden: true,
  },
  {
    id: "chat.scrollUp",
    title: "Scroll up",
    section: "Transcript",
    when: ["chat"],
    keys: "up",
    commandKeys: "k",
    repeatable: true,
    hidden: true,
  },
  {
    id: "chat.pageDown",
    title: "Page down",
    section: "Transcript",
    when: ["chat"],
    keys: "pagedown",
    commandKeys: "ctrl+d",
    repeatable: true,
    hidden: true,
  },
  {
    id: "chat.pageUp",
    title: "Page up",
    section: "Transcript",
    when: ["chat"],
    keys: "pageup",
    commandKeys: "ctrl+u",
    repeatable: true,
    hidden: true,
  },

  // -- Navigation -----------------------------------------------------------
  // The `g` namespace, and the reason Command mode is worth turning on. Nothing
  // here gets a standard binding: these screens are one sidebar click away, and
  // spending twelve chords on them would crowd out the ones people actually press.
  { id: "nav.chat", title: "Go to Chat", section: "Navigation", keys: "", commandKeys: "g c" },
  { id: "nav.notes", title: "Go to Notes", section: "Navigation", keys: "", commandKeys: "g n" },
  { id: "nav.tasks", title: "Go to Tasks", section: "Navigation", keys: "", commandKeys: "g t" },
  { id: "nav.projects", title: "Go to Projects", section: "Navigation", keys: "", commandKeys: "g p" },
  { id: "nav.research", title: "Go to Research", section: "Navigation", keys: "", commandKeys: "g r" },
  { id: "nav.gallery", title: "Go to Gallery", section: "Navigation", keys: "", commandKeys: "g i" },
  { id: "nav.files", title: "Go to Files", section: "Navigation", keys: "", commandKeys: "g f" },
  { id: "nav.calendar", title: "Go to Calendar", section: "Navigation", keys: "", commandKeys: "g d" },
  { id: "nav.memory", title: "Go to Memory", section: "Navigation", keys: "", commandKeys: "g m" },
  { id: "nav.repos", title: "Go to Repositories", section: "Navigation", keys: "", commandKeys: "g v" },
  { id: "nav.localModels", title: "Go to Local models", section: "Navigation", keys: "", commandKeys: "g l" },
  { id: "nav.compareModels", title: "Go to Compare models", section: "Navigation", keys: "", commandKeys: "g x" },

  // -- Screens that own their own search or save ----------------------------
  // Each registered by the component that holds the state, so none of this
  // required lifting a useState out of Notes, Gallery or the sidebar.
  {
    id: "sidebar.focusSearch",
    title: "Search conversations",
    section: "Sidebar",
    when: ["chat"],
    keys: "/",
    keywords: "filter find chats",
  },
  {
    id: "notes.save",
    title: "Save note",
    section: "Notes",
    when: ["notes"],
    keys: "mod+s",
  },
  {
    id: "notes.new",
    // Was mod+n, which has never once fired on macOS Chrome or Safari.
    title: "New note",
    section: "Notes",
    when: ["notes"],
    keys: "mod+shift+o",
  },
  {
    id: "notes.focusSearch",
    title: "Search notes",
    section: "Notes",
    when: ["notes"],
    keys: "/",
    keywords: "filter find",
  },
  {
    id: "gallery.focusSearch",
    title: "Search images",
    section: "Gallery",
    when: ["gallery"],
    keys: "/",
    keywords: "filter find",
  },
];

const BY_ID = new Map(COMMANDS.map((command) => [command.id, command]));

/** The command with this id, or undefined. Ids from storage may be stale. */
export function commandById(id) {
  return BY_ID.get(id);
}

/** The `when` a command declares, defaulted. Kept here so callers need not. */
export function scopesOf(command) {
  return command.when && command.when.length > 0 ? command.when : ["global"];
}

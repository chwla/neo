/**
 * Every command the keyboard can reach, and what it is bound to out of the box.
 *
 * This file is deliberately data and nothing else. Adding a command is one object
 * literal here -- no handler wiring, no new prop, no change to the API -- because
 * the handler is registered by whichever component owns the state, and the backend
 * stores whatever ids it is given without knowing this list.
 *
 * A command may have two keys, and both are always live. `keys` is the modifier
 * chord, which works wherever you are; `altKeys` is the fast one -- a single
 * letter or a `g` sequence -- which only fires when you are not typing in a field.
 * That guard is what makes a bare letter safe to ship on: it can never eat a
 * keystroke meant for the composer.
 *
 * There is no mode. Both keys are simply bound, and either can be rebound or
 * cleared in the settings screen.
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
    altKeys: "\\",
    keywords: "hide show collapse",
  },
  {
    id: "app.openSettings",
    title: "Settings",
    section: "Global",
    keys: "mod+,",
    altKeys: "g s",
    keywords: "preferences options config",
  },
  {
    id: "app.openAppearance",
    title: "Change theme",
    section: "Global",
    keys: "",
    altKeys: "",
    keywords: "theme appearance colour color palette dark light cyberpunk paper",
  },
  {
    id: "app.showKeyboardHelp",
    title: "Keyboard shortcuts",
    section: "Global",
    keys: "mod+/",
    altKeys: "?",
    keywords: "keys bindings help cheatsheet customize rebind",
  },

  // -- Composer -------------------------------------------------------------
  {
    // Implemented by the engine rather than by a keymap lookup, because Escape has
    // to keep working when everything else is suspended. Listed so it appears in
    // the shortcut list; `fixed` keeps it out of the rebinding flow.
    id: "composer.leave",
    title: "Leave the composer",
    section: "Composer",
    when: ["chat"],
    keys: "escape",
    fixed: true,
  },
  {
    // A modifier chord and no alternate, deliberately. allowsBareKeys() means a bare
    // key never fires while a textarea has focus, and the composer is exactly where
    // somebody starts dictating from -- an alternate would be dead in the only place
    // it is wanted. mod+shift+v is avoided because that is paste-as-plain-text.
    //
    // A toggle rather than hold-to-talk: the engine listens on keydown only, so there
    // is no key-up to release on.
    id: "chat.dictate",
    title: "Start or stop dictation",
    section: "Composer",
    when: ["chat"],
    keys: "mod+shift+d",
    keywords: "voice speech mic microphone dictate talk transcribe say",
  },

  // -- Chat -----------------------------------------------------------------
  {
    id: "chat.new",
    // Not mod+n: Chrome and Safari open a browser window before the page is told.
    title: "New chat",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+o",
    altKeys: "c",
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
    // The caret survives a blur, so this puts you back exactly where you were.
    altKeys: "i",
    keywords: "message input write type",
  },
  {
    id: "chat.nextChat",
    title: "Next chat",
    section: "Chat",
    keys: "mod+alt+down",
    altKeys: "] c",
    keywords: "switch newer",
  },
  {
    id: "chat.prevChat",
    title: "Previous chat",
    section: "Chat",
    keys: "mod+alt+up",
    altKeys: "[ c",
    keywords: "switch older",
  },
  {
    id: "chat.copyLast",
    title: "Copy last reply",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+c",
    altKeys: "y y",
    keywords: "clipboard yank",
  },
  {
    id: "chat.editLast",
    title: "Edit last message",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+e",
    altKeys: "g e",
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
    altKeys: "r r",
    keywords: "rerun retry again",
  },
  {
    id: "chat.compact",
    title: "Compact conversation",
    section: "Chat",
    when: ["chat"],
    keys: "",
    altKeys: "g k",
    keywords: "summarize shrink context",
  },
  {
    id: "chat.attach",
    title: "Attach a file",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+u",
    altKeys: "g u",
    keywords: "upload image document",
  },
  {
    id: "chat.toggleAgentMode",
    title: "Switch between Chat and Agent",
    section: "Chat",
    when: ["chat"],
    keys: "mod+shift+m",
    altKeys: "g a",
    keywords: "agent mode",
  },
  {
    id: "chat.deleteCurrent",
    // Safe to double-tap: it opens the existing confirmation rather than deleting.
    title: "Delete this chat",
    section: "Chat",
    when: ["chat"],
    keys: "",
    altKeys: "d d",
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
    altKeys: "g g",
    hidden: true,
  },
  {
    id: "chat.scrollBottom",
    title: "Bottom of transcript",
    section: "Transcript",
    when: ["chat"],
    keys: "end",
    altKeys: "G",
    hidden: true,
  },
  {
    id: "chat.scrollDown",
    title: "Scroll down",
    section: "Transcript",
    when: ["chat"],
    keys: "down",
    altKeys: "j",
    repeatable: true,
    hidden: true,
  },
  {
    id: "chat.scrollUp",
    title: "Scroll up",
    section: "Transcript",
    when: ["chat"],
    keys: "up",
    altKeys: "k",
    repeatable: true,
    hidden: true,
  },
  {
    id: "chat.pageDown",
    title: "Page down",
    section: "Transcript",
    when: ["chat"],
    keys: "pagedown",
    altKeys: "ctrl+d",
    repeatable: true,
    hidden: true,
  },
  {
    id: "chat.pageUp",
    title: "Page up",
    section: "Transcript",
    when: ["chat"],
    keys: "pageup",
    altKeys: "ctrl+u",
    repeatable: true,
    hidden: true,
  },

  // -- Navigation -----------------------------------------------------------
  // The `g` namespace, and where most of the speed lives. Nothing here gets a
  // modifier chord as well: these screens are one sidebar click away, and spending
  // twelve chords on them would crowd out the ones people actually press.
  { id: "nav.chat", title: "Go to Chat", section: "Navigation", keys: "", altKeys: "g c" },
  { id: "nav.notes", title: "Go to Notes", section: "Navigation", keys: "", altKeys: "g n" },
  { id: "nav.tasks", title: "Go to Tasks", section: "Navigation", keys: "", altKeys: "g t" },
  { id: "nav.projects", title: "Go to Projects", section: "Navigation", keys: "", altKeys: "g p" },
  { id: "nav.research", title: "Go to Research", section: "Navigation", keys: "", altKeys: "g r" },
  { id: "nav.gallery", title: "Go to Gallery", section: "Navigation", keys: "", altKeys: "g i" },
  { id: "nav.files", title: "Go to Files", section: "Navigation", keys: "", altKeys: "g f" },
  { id: "nav.calendar", title: "Go to Calendar", section: "Navigation", keys: "", altKeys: "g d" },
  { id: "nav.memory", title: "Go to Memory", section: "Navigation", keys: "", altKeys: "g m" },
  { id: "nav.repos", title: "Go to Repositories", section: "Navigation", keys: "", altKeys: "g v" },
  { id: "nav.localModels", title: "Go to Local models", section: "Navigation", keys: "", altKeys: "g l" },
  { id: "nav.compareModels", title: "Go to Compare models", section: "Navigation", keys: "", altKeys: "g x" },

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

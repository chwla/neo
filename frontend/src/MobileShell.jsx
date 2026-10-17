/**
 * Neo's shell on a phone.
 *
 * Below 640px there is no room for the sidebar beside a screen, so it becomes a
 * drawer: a slim bar at the top says where you are and opens it, and the screen
 * underneath gets the whole height. The drawer is the same Sidebar the desktop
 * shows, so everything reachable there (chats, projects, every SYSTEM entry,
 * Settings and the workspace screens behind it, the profile) is reachable here
 * too, and anything added to the sidebar later is reachable without touching
 * this file.
 *
 * The drawer closes when anything is opened from it, on Escape (through the
 * modal stack, which also keeps keyboard shortcuts out while it is open) and on
 * a tap outside it. Focus moves into it when it opens and back to the bar's
 * button when it closes.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { registerModal } from "./modalStack.js";

export const NARROW_SHELL_QUERY = "(max-width: 640px)";

const VIEW_TITLES = {
  settings: "Settings",
  memory: "Memory",
  research: "Research",
  notes: "Notes",
  projects: "Projects",
  tasks: "Tasks",
  calendar: "Calendar",
  files: "Files",
  gallery: "Gallery",
  repos: "Repositories",
  localModels: "Local Models",
  compareModels: "Compare Models",
};

function mediaQuery() {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(NARROW_SHELL_QUERY) : null;
}

/** Whether the shell is in its phone layout. False wherever there is no window. */
export function useNarrowShell() {
  return useSyncExternalStore(
    (onChange) => {
      const query = mediaQuery();
      if (!query) return () => {};
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () => Boolean(mediaQuery()?.matches),
    () => false,
  );
}

export function shellTitle(activeView, chatTitle) {
  if (activeView === "chat") return chatTitle || "Neo";
  return VIEW_TITLES[activeView] ?? "Neo";
}

/**
 * The drawer's state. Going anywhere from the drawer closes it (App wraps
 * those actions); nothing else can navigate while it is open, since it is on
 * the modal stack. It is not closed by the view changing on its own, such as
 * the last chat finishing loading just after the menu was tapped.
 */
export function useNavDrawer(narrow) {
  const [open, setOpen] = useState(false);
  const button = useRef(null);
  const close = useCallback(() => setOpen(false), []);
  const toggle = useCallback(() => setOpen((value) => !value), []);

  useEffect(() => {
    if (!narrow) setOpen(false);
  }, [narrow]);

  useEffect(() => {
    if (!open) return undefined;
    const unregister = registerModal(close);
    // Into the drawer, at its first control.
    requestAnimationFrame(() => document.getElementById("neo-nav-drawer")?.querySelector("button, input, a[href]")?.focus());
    return () => {
      unregister();
      // Back to where the drawer was opened from, unless focus already moved on.
      const active = document.activeElement;
      if (!active || active === document.body || document.getElementById("neo-nav-drawer")?.contains(active)) button.current?.focus();
    };
  }, [open, close]);

  const drawer = useMemo(() => (narrow ? { open, onClose: close } : null), [narrow, open, close]);
  return { open, toggle, close, button, drawer };
}

/**
 * The bar at the top of a phone screen: the way to everything else, and where
 * you are. The command palette is on it too: it is how a keyboard reaches the
 * screens no sidebar entry leads to (Tasks, say), and on a phone there is no
 * keyboard shortcut to open it.
 */
export function MobileNavBar({ title, open, onToggle, buttonRef, onOpenPalette }) {
  return (
    <header className="neo-mobile-bar">
      <button
        ref={buttonRef}
        type="button"
        className="neo-mobile-menu"
        onClick={onToggle}
        aria-label="Open navigation"
        aria-expanded={open}
        aria-controls="neo-nav-drawer"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 7h16M4 12h16M4 17h16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </button>
      <h1 className="neo-mobile-title">{title}</h1>
      {onOpenPalette && (
        <button type="button" className="neo-mobile-menu neo-mobile-palette" onClick={onOpenPalette} aria-label="Go to a screen or run a command">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" strokeWidth="2" />
            <path d="m16 16 4.5 4.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </header>
  );
}

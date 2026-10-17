import { Suspense, lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import Icon from "./WorkspaceIcon.jsx";
import { useCommandHandlers } from "./keys/useCommands.js";
import NoteEditor from "./notes/NoteEditor.jsx";
import NoteInfoPanel from "./notes/NoteInfoPanel.jsx";
import NoteList from "./notes/NoteList.jsx";
import NotesSidebar from "./notes/NotesSidebar.jsx";
import { matchesView, notesController, releaseOtherControllers } from "./notes/notesController.js";
import { ConfirmDialog, PromptDialog, Toast } from "./notes/ui.jsx";
import "./notes/notes.css";

const SIDEBAR_KEY = "neo-notes-sidebar";
// Shown only around locked notes, so loaded only then.
const LockedNote = lazy(() => import("./notes/LockViews.jsx").then((m) => ({ default: m.LockedNote })));
const LockPrompt = lazy(() => import("./notes/LockViews.jsx").then((m) => ({ default: m.LockPrompt })));

function useNotes(controller, selector) {
  return useSyncExternalStore(controller.subscribe, () => selector(controller.getState()));
}

/** "wide" (three panes), "medium" (folders as an overlay) or "narrow" (one pane at a time). */
function useLayout(ref) {
  const [mode, setMode] = useState("wide");
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver !== "function") return undefined;
    const measure = (width) => setMode(width < 720 ? "narrow" : width < 1100 ? "medium" : "wide");
    measure(element.getBoundingClientRect().width);
    const observer = new ResizeObserver(([entry]) => measure(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return mode;
}

/** Height covered by an on-screen keyboard, from the visual viewport (iOS does not resize the page). */
function useKeyboardInset(ref) {
  useEffect(() => {
    const viewport = window.visualViewport;
    const element = ref.current;
    if (!viewport || !element) return undefined;
    // Written only when it changes: the visual viewport scrolls on every
    // keystroke that moves the caret, and rewriting a custom property or an
    // attribute on the Notes root restyles the whole note under it.
    let last = { inset: null, state: null };
    const update = () => {
      const covered = Math.round(Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop));
      const state = covered > 80 ? "open" : "closed";
      if (covered !== last.inset) element.style.setProperty("--nw-keyboard", `${covered}px`);
      if (state !== last.state) element.dataset.keyboard = state;
      last = { inset: covered, state };
    };
    update();
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, [ref]);
}

function readSidebarPreference() {
  try {
    return localStorage.getItem(SIDEBAR_KEY) !== "hidden";
  } catch {
    return true;
  }
}

/**
 * Notes, laid out as Apple Notes is: folders, the notes of a folder, and the
 * note. At medium widths the folders slide over; on a phone each is a screen of
 * its own with a way back.
 */
export default function Notes({ profile, initialNoteId = null, onOpenTask, onOpenFile }) {
  const profileId = profile?.id ?? "local";
  const controller = useMemo(() => notesController(profileId), [profileId]);
  const view = useNotes(controller, (state) => state.view);
  const list = useNotes(controller, (state) => state.list);
  const items = useNotes(controller, (state) => state.items);
  const drafts = useNotes(controller, (state) => state.drafts);
  const sidebar = useNotes(controller, (state) => state.sidebar);
  const settings = useNotes(controller, (state) => state.settings);
  const selection = useNotes(controller, (state) => state.selection);
  const session = useNotes(controller, (state) => state.session);
  const saveStatus = useNotes(controller, (state) => state.saveStatus);
  const notice = useNotes(controller, (state) => state.notice);
  const toast = useNotes(controller, (state) => state.toast);
  const lock = useNotes(controller, (state) => state.lock);
  const lockPrompt = useNotes(controller, (state) => state.lockPrompt);

  const root = useRef(null);
  const searchRef = useRef(null);
  const mode = useLayout(root);
  useKeyboardInset(root);
  const [sidebarShown, setSidebarShown] = useState(readSidebarPreference);
  const [overlay, setOverlay] = useState(false);
  const [pane, setPane] = useState("list");
  const [infoOpen, setInfoOpen] = useState(false);
  const [folderPrompt, setFolderPrompt] = useState(false);
  const [confirmRemoveLock, setConfirmRemoveLock] = useState(false);

  useEffect(() => {
    releaseOtherControllers(profileId);
  }, [profileId]);

  useEffect(() => {
    controller.loadSidebar();
    controller.refreshList();
  }, [controller]);

  useEffect(() => {
    if (!initialNoteId) return;
    controller.revealNote(initialNoteId);
    setPane("note");
  }, [controller, initialNoteId]);

  // Leaving Notes is not leaving the note's content: it is frozen and saved.
  useEffect(() => () => controller.leaveSession(), [controller]);

  useEffect(() => {
    if (!session) setInfoOpen(false);
  }, [session]);

  const toggleSidebar = useCallback(() => {
    if (mode === "wide") {
      setSidebarShown((shown) => {
        try {
          localStorage.setItem(SIDEBAR_KEY, shown ? "hidden" : "shown");
        } catch {
          /* storage unavailable */
        }
        return !shown;
      });
    } else if (mode === "medium") setOverlay((open) => !open);
    else setPane("sidebar");
  }, [mode]);

  const startNote = useCallback(() => {
    if (view.kind === "deleted" || view.kind === "archived") controller.setView({ kind: "all" });
    controller.newNote();
    setPane("note");
  }, [controller, view.kind]);

  const openRow = useCallback(
    (item) => {
      setPane("note");
      if (item.key) return controller.openDraft(item.key);
      return controller.openNote(item.id);
    },
    [controller],
  );

  const focusEditor = useCallback((item) => {
    const session = controller.getState().session;
    const key = item?.key ?? item?.id;
    if (key && session?.key !== key && session?.id !== key) return false;
    const editor = root.current?.querySelector(".nw-prose[contenteditable='true']");
    if (!editor) return false;
    editor.focus();
    return true;
  }, [controller]);

  const rows = useMemo(() => {
    const draftRows = view.q || view.kind === "deleted" || view.kind === "archived" ? [] : Object.values(drafts);
    const listed = list.ids.map((id) => items[id]).filter((item) => matchesView(item, view));
    return [...draftRows, ...listed];
  }, [drafts, list.ids, items, view]);

  const openId = session?.id ?? null;
  const openItem = openId ? items[openId] ?? session?.note : session?.note;

  useCommandHandlers({
    "notes.save": () => {
      controller.flushCurrent();
      return true;
    },
    "notes.new": () => {
      startNote();
    },
    "notes.focusSearch": () => {
      if (!searchRef.current) return false;
      if (mode === "narrow") setPane("list");
      searchRef.current.focus();
      return true;
    },
    "notes.newFolder": () => setFolderPrompt(true),
    "notes.togglePin": () => {
      if (!openId || openItem?.deleted) return false;
      controller.pin(openId, !openItem?.pinned);
      return true;
    },
    "notes.duplicate": () => {
      if (!openId || openItem?.deleted || session?.format === "encrypted") return false;
      controller.duplicate(openId);
      return true;
    },
    "notes.showInfo": () => {
      if (!session?.doc) return false;
      setInfoOpen((open) => !open);
      return true;
    },
    "notes.showRecentlyDeleted": () => {
      controller.setView({ kind: "deleted" });
      setPane("list");
    },
    "notes.toggleFolders": () => toggleSidebar(),
  });

  const folders = sidebar.folders ?? [];
  const folderName = (id) => (id ? folders.find((folder) => folder.id === id)?.name ?? "Notes" : "Notes");

  const lockedNote = session?.format === "encrypted";
  const noteActions = useMemo(() => {
    if (!openItem || !openId) return [];
    if (openItem.deleted) {
      return [
        { label: "Recover", onSelect: () => controller.restore(openId) },
        { label: "Info", onSelect: () => setInfoOpen(true) },
      ];
    }
    const lockItems = lockedNote
      ? [
          { label: "Lock Now", onSelect: () => controller.lockNow() },
          { label: "Remove Lock", disabled: Boolean(lock.busy), onSelect: () => setConfirmRemoveLock(true) },
        ]
      : [{ label: "Lock Note", disabled: Boolean(lock.busy), onSelect: () => controller.lockNote() }];
    return [
      { label: openItem.pinned ? "Unpin Note" : "Pin Note", onSelect: () => controller.pin(openId, !openItem.pinned) },
      ...lockItems,
      {
        label: "Move to",
        items: [
          { label: "Notes", disabled: !openItem.folder_id, onSelect: () => controller.move(openId, null) },
          ...folders.map((folder) => ({
            label: folder.name,
            disabled: openItem.folder_id === folder.id,
            onSelect: () => controller.move(openId, folder.id),
          })),
        ],
      },
      // A locked note's ciphertext belongs to it alone, so it isn't duplicated.
      !lockedNote && { label: "Duplicate", onSelect: () => controller.duplicate(openId) },
      { label: openItem.archived ? "Unarchive" : "Archive", onSelect: () => controller.archive(openId, !openItem.archived) },
      { label: "Info", onSelect: () => setInfoOpen(true) },
    ].filter(Boolean);
  }, [controller, openId, openItem, folders, lockedNote, lock.busy]);

  const showSidebar = mode === "wide" ? sidebarShown : mode === "medium" ? overlay : pane === "sidebar";
  const showList = mode !== "narrow" || pane === "list";
  const showEditor = mode !== "narrow" || pane === "note";

  return (
    <div className="nw" ref={root} data-mode={mode} onKeyDownCapture={controller.touchKeys} onPointerDownCapture={controller.touchKeys}>
      <div className="nw-shell" data-sidebar={showSidebar ? "shown" : "hidden"} data-info={infoOpen ? "open" : "closed"}>
        {showSidebar && (
          <>
            {mode === "medium" && (
              <button type="button" className="nw-overlay-scrim" aria-label="Close folders" onClick={() => setOverlay(false)} />
            )}
            <NotesSidebar
              controller={controller}
              sidebar={sidebar}
              view={view}
              onChosen={() => {
                if (mode === "medium") setOverlay(false);
                if (mode === "narrow") setPane("list");
              }}
            />
          </>
        )}
        {showList && (
          <NoteList
            controller={controller}
            view={view}
            list={list}
            rows={rows}
            sidebar={sidebar}
            settings={settings}
            selectedKey={selection?.id ?? selection?.key ?? null}
            onOpen={openRow}
            onNewNote={startNote}
            onBack={mode === "narrow" ? () => setPane("sidebar") : null}
            onToggleSidebar={mode === "narrow" ? null : toggleSidebar}
            onFocusEditor={focusEditor}
            searchRef={searchRef}
          />
        )}
        {showEditor && (
          <section className="nw-editorpane" aria-label="Note">
            {notice && <p className="nw-notice" role="status">{notice}</p>}
            {lock.busy && <p className="nw-notice" role="status" aria-live="polite">{lock.busy}</p>}
            {session?.format === "encrypted" && session.locked && !session.error ? (
              <Suspense fallback={<div className="nw-blank" aria-busy="true" />}>
              <LockedNote
                controller={controller}
                session={session}
                lock={lock}
                toolbarStart={
                  mode === "narrow" ? (
                    <button type="button" className="nw-tool nw-back nw-nav-tool" onClick={() => setPane("list")} aria-label="Back to notes">
                      <Icon name="back" />
                    </button>
                  ) : null
                }
                toolbarEnd={
                  openId && !openItem?.deleted ? (
                    <button type="button" className="nw-tool" aria-label="Delete note" title="Delete" onClick={() => controller.trash(openId)}>
                      <Icon name="trash" />
                    </button>
                  ) : null
                }
              />
              </Suspense>
            ) : session?.doc ? (
              <NoteEditor
                controller={controller}
                session={session}
                status={saveStatus[session.key] ?? "clean"}
                settings={settings}
                query={view.q}
                noteActions={noteActions}
                toolbarStart={
                  mode === "narrow" ? (
                    <button type="button" className="nw-tool nw-back nw-nav-tool" onClick={() => setPane("list")} aria-label="Back to notes">
                      <Icon name="back" />
                    </button>
                  ) : null
                }
                toolbarEnd={
                  <>
                    {/* On a phone, as in Apple Notes there, Lock is in the "..." menu: the bar has no room. */}
                    {openId && !openItem?.deleted && mode !== "narrow" && (
                      <button
                        type="button"
                        className="nw-tool"
                        aria-label={lockedNote ? "Lock Now" : "Lock Note"}
                        title={lockedNote ? "Lock Now" : "Lock Note"}
                        disabled={Boolean(lock.busy)}
                        onClick={() => (lockedNote ? controller.lockNow() : controller.lockNote())}
                      >
                        <Icon name={lockedNote ? "unlock" : "lock"} />
                      </button>
                    )}
                    {openId && !openItem?.deleted && (
                      <button type="button" className="nw-tool" aria-label="Delete note" title="Delete" onClick={() => controller.trash(openId)}>
                        <Icon name="trash" />
                      </button>
                    )}
                    {mode !== "narrow" && view.kind !== "deleted" && (
                      <button type="button" className="nw-tool" aria-label="New note" title="New note" onClick={startNote}>
                        <Icon name="compose" />
                      </button>
                    )}
                  </>
                }
              />
            ) : session?.error ? (
              <div className="nw-blank" role="alert">{session.error}</div>
            ) : session?.loading ? (
              <div className="nw-blank" aria-busy="true" />
            ) : (
              <div className="nw-blank">
                {view.kind !== "deleted" && (
                  <button type="button" className="nw-blank-new" onClick={startNote}>
                    <Icon name="compose" />
                    New Note
                  </button>
                )}
              </div>
            )}
            {openItem?.deleted && session?.doc && (
              <div className="nw-deleted-bar" role="status">
                <span>Notes in Recently Deleted can't be edited.</span>
                <button type="button" onClick={() => controller.restore(openId)}>Recover</button>
              </div>
            )}
          </section>
        )}
        {infoOpen && session?.doc && (
          <NoteInfoPanel
            controller={controller}
            session={session}
            folderName={folderName(openItem?.folder_id)}
            onOpenTask={onOpenTask}
            onOpenFile={onOpenFile}
            onClose={() => setInfoOpen(false)}
          />
        )}
      </div>
      <Toast toast={toast} onDismiss={controller.dismissToast} />
      {lockPrompt && (
        <Suspense fallback={null}>
          <LockPrompt key={lockPrompt.mode} controller={controller} prompt={lockPrompt} />
        </Suspense>
      )}
      {confirmRemoveLock && (
        <ConfirmDialog
          title="Remove Lock?"
          message="This note will be stored without encryption. Anyone who can open your Neo files can read it."
          confirmLabel="Remove Lock"
          danger
          onCancel={() => setConfirmRemoveLock(false)}
          onConfirm={() => {
            setConfirmRemoveLock(false);
            controller.removeLock();
          }}
        />
      )}
      {folderPrompt && (
        <PromptDialog
          title="New Folder"
          label="Name"
          initial="New Folder"
          confirmLabel="Create"
          onCancel={() => setFolderPrompt(false)}
          onSubmit={async (name) => {
            setFolderPrompt(false);
            const folder = await controller.createFolder(name);
            if (folder) controller.setView({ kind: "folder", folderId: folder.id });
          }}
        />
      )}
    </div>
  );
}

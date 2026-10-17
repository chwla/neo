import { memo, useCallback, useMemo, useRef, useState } from "react";

import Icon from "../WorkspaceIcon.jsx";
import { fileUrl } from "./extensions/attachments.js";
import { groupNotes, rowDate } from "./noteDates.js";
import { NOTE_DRAG_TYPE } from "./NotesSidebar.jsx";
import { ConfirmDialog, Menu, useContextMenu } from "./ui.jsx";

function Highlight({ text, query }) {
  const needle = query?.trim();
  if (!needle || needle.length < 2 || !text) return text ?? "";
  const lower = text.toLowerCase();
  const parts = [];
  let from = 0;
  for (let at = lower.indexOf(needle.toLowerCase()); at !== -1; at = lower.indexOf(needle.toLowerCase(), from)) {
    if (at > from) parts.push(text.slice(from, at));
    parts.push(<mark key={at}>{text.slice(at, at + needle.length)}</mark>);
    from = at + needle.length;
  }
  parts.push(text.slice(from));
  return parts;
}

export function viewTitle(view, sidebar) {
  if (view.q) return "Search";
  if (view.kind === "deleted") return "Recently Deleted";
  if (view.kind === "archived") return "Archived";
  if (view.kind === "tags") return view.tags.map((tag) => `#${tag}`).join(" ");
  if (view.kind === "smart") return sidebar.smart_folders?.find((folder) => folder.id === view.smartId)?.name ?? "Smart Folder";
  if (view.kind === "folder") {
    if (view.folderId === "default") return "Notes";
    return sidebar.folders?.find((folder) => folder.id === view.folderId)?.name ?? "Folder";
  }
  return (sidebar.folders?.length ?? 0) > 0 ? "All Notes" : "Notes";
}

const Row = memo(function Row({ item, selected, query, folderName, onOpen, onMenu, gallery, tabIndex }) {
  const context = useContextMenu((at) => onMenu(item, at));
  const title = item.title?.trim() || "New Note";
  const locked = item.content_format === "encrypted";
  // A locked note's title is all the list knows of it.
  const preview = locked ? "Locked" : item.snippet || item.preview || "No additional text";
  const draggable = !item.key && !item.deleted;
  const common = {
    type: "button",
    role: "option",
    "aria-selected": selected,
    tabIndex,
    "data-note-id": item.key ? undefined : item.id,
    "data-note-key": item.key ?? item.id,
    draggable,
    onDragStart: (event) => {
      event.dataTransfer.setData(NOTE_DRAG_TYPE, item.id);
      event.dataTransfer.effectAllowed = "move";
    },
    onClick: () => {
      if (context.consumedPress()) return;
      onOpen(item);
    },
    ...context,
  };
  if (gallery) {
    return (
      <button {...common} className={`nw-card ${selected ? "selected" : ""}`.trim()}>
        <span className={`nw-card-page ${item.cover_file_id ? "with-cover" : ""}`.trim()}>
          {item.cover_file_id ? (
            <img className="nw-card-cover" src={fileUrl(item.cover_file_id)} alt="" loading="lazy" decoding="async" />
          ) : (
            <>
              <span className="nw-card-title">{title}</span>
              <span className="nw-card-excerpt">{locked ? <Icon name="lock" className="nw-card-lock" /> : item.excerpt || item.preview || ""}</span>
            </>
          )}
        </span>
        <span className="nw-card-caption">
          <span className="nw-card-name"><Highlight text={title} query={query} /></span>
          <time dateTime={item.updated_at}>{rowDate(item.updated_at)}</time>
        </span>
      </button>
    );
  }
  return (
    <button {...common} className={`nw-row ${folderName ? "with-folder" : ""} ${item.cover_file_id ? "with-thumb" : ""} ${selected ? "selected" : ""}`.trim()}>
      {item.cover_file_id && <img className="nw-row-thumb" src={fileUrl(item.cover_file_id)} alt="" loading="lazy" decoding="async" />}
      <span className="nw-row-title">
        {item.pinned && !item.deleted && <Icon name="pin" className="nw-row-pin" />}
        <Highlight text={title} query={query} />
        {locked && <Icon name="lock" className="nw-row-lock" />}
      </span>
      <span className="nw-row-line">
        <time dateTime={item.updated_at}>{rowDate(item.updated_at)}</time>
        <span className="nw-row-preview"><Highlight text={preview} query={query} /></span>
      </span>
      {folderName && (
        <span className="nw-row-folder">
          <Icon name="folder" />
          {folderName}
        </span>
      )}
    </button>
  );
});

/** Enter opens the highlighted row; a cold note focuses once its fetch finishes. */
export function activateHighlightedNote(event, item, onOpen, onFocusEditor, schedule = requestAnimationFrame) {
  if (event.key !== "Enter" || !item) return false;
  event.preventDefault();
  const opening = onOpen(item);
  schedule(() => {
    if (onFocusEditor?.(item)) return;
    Promise.resolve(opening).then((opened) => {
      if (opened !== false) schedule(() => onFocusEditor?.(item));
    }).catch(() => {});
  });
  return true;
}

/**
 * The middle column: the notes of the current view in Apple's date sections (or
 * a gallery), its search, and the actions on a note. Arrow keys move through the
 * notes and open them; Delete sends the selected note to Recently Deleted.
 */
export default function NoteList({
  controller,
  view,
  list,
  rows,
  sidebar,
  settings,
  selectedKey,
  onOpen,
  onNewNote,
  onBack,
  onToggleSidebar,
  onFocusEditor,
  searchRef,
}) {
  const [menu, setMenu] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const listRef = useRef(null);
  const deletedView = view.kind === "deleted" && !view.q;
  const gallery = settings.view === "gallery" && !deletedView;
  const folderNames = useMemo(() => new Map((sidebar.folders ?? []).map((folder) => [folder.id, folder.name])), [sidebar.folders]);
  const showFolders = Boolean(view.q) || view.kind === "all" || view.kind === "tags" || view.kind === "smart";
  const sections = useMemo(
    () => groupNotes(rows, { sortBy: settings.sort_by, groupByDate: settings.group_by_date, pinning: !deletedView }),
    [rows, settings.sort_by, settings.group_by_date, deletedView],
  );
  const ordered = useMemo(() => sections.flatMap((section) => section.items), [sections]);

  const rowMenu = useCallback(
    (item, at) => {
      if (item.key) return;
      const destinations = [
        { label: "Notes", onSelect: () => (item.deleted ? controller.restore(item.id, null) : controller.move(item.id, null)), disabled: !item.deleted && !item.folder_id },
        ...(sidebar.folders ?? []).map((folder) => ({
          label: folder.name,
          disabled: !item.deleted && item.folder_id === folder.id,
          onSelect: () => (item.deleted ? controller.restore(item.id, folder.id) : controller.move(item.id, folder.id)),
        })),
      ];
      if (item.deleted) {
        setMenu({
          at,
          label: item.title || "Note",
          items: [
            { label: "Recover", onSelect: () => controller.restore(item.id) },
            { label: "Move to", items: destinations },
            { separator: true },
            { label: "Delete Permanently", danger: true, onSelect: () => setConfirm({ kind: "erase", item }) },
          ],
        });
        return;
      }
      setMenu({
        at,
        label: item.title || "Note",
        items: [
          { label: item.pinned ? "Unpin Note" : "Pin Note", onSelect: () => controller.pin(item.id, !item.pinned) },
          // Locking works on the note it opens; Remove Lock (confirmed) is in the note's own menu.
          item.content_format !== "encrypted" && {
            label: "Lock Note",
            onSelect: async () => {
              onOpen(item);
              await controller.openNote(item.id);
              controller.lockNote();
            },
          },
          { label: "Move to", items: destinations },
          item.content_format !== "encrypted" && { label: "Duplicate", onSelect: () => controller.duplicate(item.id) },
          { label: item.archived ? "Unarchive" : "Archive", onSelect: () => controller.archive(item.id, !item.archived) },
          { separator: true },
          { label: "Delete", danger: true, onSelect: () => controller.trash(item.id) },
        ],
      });
    },
    [controller, sidebar.folders],
  );

  function onKeyDown(event) {
    if (event.target.closest("input")) return;
    const index = ordered.findIndex((item) => (item.key ?? item.id) === selectedKey);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = ordered[Math.max(0, Math.min(ordered.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))];
      if (!next) return;
      onOpen(next);
      requestAnimationFrame(() => {
        const key = next.key ?? next.id;
        [...(listRef.current?.querySelectorAll("[data-note-key]") ?? [])]
          .find((row) => row.dataset.noteKey === key)?.focus();
      });
    } else if ((event.key === "Delete" || event.key === "Backspace") && index >= 0) {
      event.preventDefault();
      const item = ordered[index];
      if (item.key) return;
      if (item.deleted) setConfirm({ kind: "erase", item });
      else controller.trash(item.id);
    } else if (event.key === "Enter" && index >= 0) {
      activateHighlightedNote(event, ordered[index], onOpen, onFocusEditor);
    }
  }

  const viewMenu = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const sortBy = settings.sort_by;
    setMenu({
      at: { x: rect.left, y: rect.bottom + 4 },
      label: "View options",
      items: [
        { label: "Date Edited", checked: sortBy === "edited", onSelect: () => controller.updateSettings({ sort_by: "edited" }) },
        { label: "Date Created", checked: sortBy === "created", onSelect: () => controller.updateSettings({ sort_by: "created" }) },
        { label: "Title", checked: sortBy === "title", onSelect: () => controller.updateSettings({ sort_by: "title" }) },
        { separator: true },
        {
          label: sortBy === "title" ? "A to Z" : "Newest First",
          checked: sortBy === "title" ? settings.sort_order === "asc" : settings.sort_order === "desc",
          onSelect: () => controller.updateSettings({ sort_order: sortBy === "title" ? "asc" : "desc" }),
        },
        {
          label: sortBy === "title" ? "Z to A" : "Oldest First",
          checked: sortBy === "title" ? settings.sort_order === "desc" : settings.sort_order === "asc",
          onSelect: () => controller.updateSettings({ sort_order: sortBy === "title" ? "desc" : "asc" }),
        },
        { separator: true },
        { label: "Group by Date", checked: settings.group_by_date, onSelect: () => controller.updateSettings({ group_by_date: !settings.group_by_date }) },
        {
          label: "New Notes Start With",
          items: ["title", "heading", "subheading", "body"].map((style) => ({
            label: style[0].toUpperCase() + style.slice(1),
            checked: settings.new_note_style === style,
            onSelect: () => controller.updateSettings({ new_note_style: style }),
          })),
        },
        {
          label: "Sort Checked Items",
          items: [
            { label: "Manually", checked: settings.checked_items !== "auto", onSelect: () => controller.updateSettings({ checked_items: "manual" }) },
            { label: "Automatically", checked: settings.checked_items === "auto", onSelect: () => controller.updateSettings({ checked_items: "auto" }) },
          ],
        },
      ],
    });
  };

  const title = viewTitle(view, sidebar);
  const total = view.kind === "deleted" ? list.total : list.total;

  return (
    <section className="nw-listpane" aria-label={title}>
      <header className="nw-listhead">
        <div className="nw-listhead-top">
          {onBack ? (
            <button type="button" className="nw-tool nw-back" onClick={onBack} aria-label="Folders">
              <Icon name="back" />
            </button>
          ) : (
            onToggleSidebar && (
              <button type="button" className="nw-tool" onClick={onToggleSidebar} aria-label="Show or hide folders" title="Folders">
                <Icon name="sidebar" />
              </button>
            )
          )}
          <span className="nw-listhead-spacer" />
          {!deletedView && (
            <button
              type="button"
              className="nw-tool"
              aria-label={gallery ? "Show as list" : "Show as gallery"}
              title={gallery ? "List" : "Gallery"}
              onClick={() => controller.updateSettings({ view: gallery ? "list" : "gallery" })}
            >
              <Icon name={gallery ? "rows" : "gallery"} />
            </button>
          )}
          <button type="button" className="nw-tool" aria-label="View options" aria-haspopup="menu" title="View options" onClick={viewMenu}>
            <Icon name="sort" />
          </button>
          {!deletedView && (
            <button type="button" className="nw-tool" onClick={onNewNote} aria-label="New note" title="New note">
              <Icon name="compose" />
            </button>
          )}
        </div>
        <h1 title={title}>{title}</h1>
        <label className="nw-search">
          <Icon name="search" />
          <input
            ref={searchRef}
            type="search"
            value={view.q}
            onChange={(event) => controller.setSearch(event.target.value)}
            placeholder="Search all notes"
            aria-label="Search all notes"
          />
        </label>
        <p className="nw-count">
          {list.status === "loading" && !rows.length ? "Loading…" : `${total} note${total === 1 ? "" : "s"}`}
        </p>
      </header>
      {deletedView && (
        <div className="nw-deleted-info">
          <p>Notes are kept here for 30 days, then deleted for good.</p>
          {list.total > 0 && (
            <button type="button" onClick={() => setConfirm({ kind: "empty" })}>Delete All</button>
          )}
        </div>
      )}
      <div
        ref={listRef}
        className={gallery ? "nw-gallery" : "nw-rows"}
        role="listbox"
        aria-label={title}
        onKeyDown={onKeyDown}
      >
        {list.status === "error" && <p className="nw-empty">{list.error}</p>}
        {!rows.length && list.status === "ready" && (
          <p className="nw-empty">{view.q ? "No results" : deletedView ? "No recently deleted notes" : "No notes"}</p>
        )}
        {sections.map((section) => (
          <div key={section.key} className="nw-section" role="group" aria-label={section.label ?? "Notes"}>
            {section.label && <h2 className="nw-section-label">{section.label}</h2>}
            {section.items.map((item) => {
              const key = item.key ?? item.id;
              return (
                <Row
                  key={key}
                  item={item}
                  gallery={gallery}
                  selected={key === selectedKey}
                  tabIndex={key === selectedKey || (!selectedKey && item === ordered[0]) ? 0 : -1}
                  query={view.q}
                  folderName={showFolders && !item.key ? (item.folder_id ? folderNames.get(item.folder_id) ?? "Notes" : "Notes") : null}
                  onOpen={onOpen}
                  onMenu={rowMenu}
                />
              );
            })}
          </div>
        ))}
      </div>
      {menu && <Menu items={menu.items} at={menu.at} label={menu.label} onClose={() => setMenu(null)} />}
      {confirm?.kind === "erase" && (
        <ConfirmDialog
          title="Delete permanently?"
          message={`"${confirm.item.title || "New Note"}" will be deleted for good. This can't be undone.`}
          confirmLabel="Delete"
          danger
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            setConfirm(null);
            controller.deleteForever(confirm.item.id);
          }}
        />
      )}
      {confirm?.kind === "empty" && (
        <ConfirmDialog
          title="Delete all notes in Recently Deleted?"
          message="They will be deleted for good. This can't be undone."
          confirmLabel="Delete All"
          danger
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            setConfirm(null);
            controller.emptyTrash();
          }}
        />
      )}
    </section>
  );
}

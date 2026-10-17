import { memo, useMemo, useState } from "react";

import Icon from "../WorkspaceIcon.jsx";
import SmartFolderDialog from "./SmartFolderDialog.jsx";
import { ConfirmDialog, Menu, PromptDialog, useContextMenu } from "./ui.jsx";

export const NOTE_DRAG_TYPE = "application/x-neo-note";
export const FOLDER_DRAG_TYPE = "application/x-neo-note-folder";

function sameView(a, b) {
  return a.kind === b.kind && (a.folderId ?? null) === (b.folderId ?? null);
}

/** Folders as a tree, in position order, each with its depth. */
function flatten(folders, collapsed) {
  const byParent = new Map();
  for (const folder of folders) {
    const key = folder.parent_id ?? null;
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(folder);
  }
  const out = [];
  const walk = (parent, depth) => {
    for (const folder of byParent.get(parent) ?? []) {
      const children = byParent.has(folder.id);
      out.push({ ...folder, depth, children });
      if (children && !collapsed.has(folder.id)) walk(folder.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

function descendantsOf(folders, id) {
  const ids = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const folder of folders) {
      if (folder.parent_id && ids.has(folder.parent_id) && !ids.has(folder.id)) {
        ids.add(folder.id);
        grew = true;
      }
    }
  }
  return ids;
}

const Entry = memo(function Entry({ icon, label, count, active, depth = 0, onSelect, onDrop, dropKinds, menu, chevron, dragData }) {
  const [over, setOver] = useState(false);
  const context = useContextMenu((at) => menu?.(at));
  const accepts = (event) => dropKinds?.some((type) => event.dataTransfer.types.includes(type));
  return (
    <div
      className={`nw-side-entry ${active ? "active" : ""} ${over ? "drop" : ""}`.trim()}
      style={{ "--depth": depth }}
      onDragOver={(event) => {
        if (!onDrop || !accepts(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        setOver(false);
        if (!onDrop || !accepts(event)) return;
        event.preventDefault();
        onDrop(event.dataTransfer);
      }}
    >
      {chevron}
      <button
        type="button"
        className="nw-side-item"
        aria-current={active ? "true" : undefined}
        draggable={Boolean(dragData)}
        onDragStart={(event) => {
          if (!dragData) return;
          event.dataTransfer.setData(dragData.type, dragData.value);
          event.dataTransfer.effectAllowed = "move";
        }}
        onClick={() => {
          if (context.consumedPress()) return;
          onSelect();
        }}
        {...(menu ? context : {})}
      >
        <Icon name={icon} />
        <span className="nw-side-label">{label}</span>
        {count != null && <span className="nw-side-count">{count}</span>}
      </button>
    </div>
  );
});

/** A tag in the sidebar: tap to filter by it, long-press or right-click for its menu. */
const TagChip = memo(function TagChip({ tag, count, on, onToggle, menu }) {
  const context = useContextMenu((at) => menu(at));
  return (
    <button
      type="button"
      className={`nw-tag-chip ${on ? "on" : ""}`.trim()}
      aria-pressed={on}
      title={`${count} note${count === 1 ? "" : "s"}`}
      {...context}
      onClick={() => {
        if (context.consumedPress()) return;
        onToggle();
      }}
    >
      #{tag}
    </button>
  );
});

/**
 * The folder sidebar, as in Apple Notes: All Notes (once there are folders),
 * Notes, folders (nested, with counts), Archived (when it has notes), Recently
 * Deleted, and Tags. Notes and folders can be dropped onto folders.
 */
export default function NotesSidebar({ controller, sidebar, view, onChosen }) {
  const [collapsed, setCollapsed] = useState(() => new Set());
  const [menu, setMenu] = useState(null);
  const [dialog, setDialog] = useState(null);
  const folders = sidebar.folders ?? [];
  const tree = useMemo(() => flatten(folders, collapsed), [folders, collapsed]);
  const counts = sidebar.counts ?? {};

  const choose = (next) => {
    controller.setView(next);
    onChosen?.();
  };

  const dropNote = (folderId) => (transfer) => {
    const noteId = transfer.getData(NOTE_DRAG_TYPE);
    if (noteId) controller.move(noteId, folderId);
  };
  const dropOnFolder = (folderId) => (transfer) => {
    const noteId = transfer.getData(NOTE_DRAG_TYPE);
    if (noteId) return controller.move(noteId, folderId);
    const dragged = transfer.getData(FOLDER_DRAG_TYPE);
    if (dragged && !descendantsOf(folders, dragged).has(folderId)) controller.moveFolder(dragged, folderId);
    return undefined;
  };

  function folderMenu(folder, at) {
    const targets = folders.filter((other) => !descendantsOf(folders, folder.id).has(other.id));
    setMenu({
      at,
      label: folder.name,
      items: [
        { label: "Rename", onSelect: () => setDialog({ kind: "rename", folder }) },
        { label: "New Subfolder", onSelect: () => setDialog({ kind: "new", parentId: folder.id }) },
        {
          label: "Move to",
          items: [
            { label: "Top level", disabled: !folder.parent_id, onSelect: () => controller.moveFolder(folder.id, null) },
            ...targets.map((target) => ({
              label: target.name,
              disabled: target.id === folder.parent_id,
              onSelect: () => controller.moveFolder(folder.id, target.id),
            })),
          ],
        },
        { separator: true },
        { label: "Delete Folder", danger: true, onSelect: () => setDialog({ kind: "delete", folder }) },
      ],
    });
  }

  const smartFolders = sidebar.smart_folders ?? [];
  const tags = sidebar.tags ?? [];
  const selectedTags = view.kind === "tags" ? view.tags : [];
  const tagRules = (list, mode = "any") => ({
    match: "all",
    rules: [{ field: "tags", op: list.length > 1 && mode === "all" ? "includes_all" : "includes_any", value: list }],
  });

  function smartMenu(folder, at) {
    setMenu({
      at,
      label: folder.name,
      items: [
        { label: "Edit Smart Folder", onSelect: () => setDialog({ kind: "smart", initial: folder }) },
        { separator: true },
        { label: "Delete Smart Folder", danger: true, onSelect: () => setDialog({ kind: "deleteSmart", folder }) },
      ],
    });
  }

  function tagMenu(tag, at) {
    setMenu({
      at,
      label: `#${tag}`,
      items: [
        { label: "Rename Tag", onSelect: () => setDialog({ kind: "renameTag", tag }) },
        { label: "Create Smart Folder", onSelect: () => setDialog({ kind: "smart", initial: { name: `#${tag}`, rules: tagRules([tag]) } }) },
        { separator: true },
        { label: "Delete Tag", danger: true, onSelect: () => setDialog({ kind: "deleteTag", tag }) },
      ],
    });
  }
  const toggleTag = (tag) => {
    const next = selectedTags.includes(tag) ? selectedTags.filter((item) => item !== tag) : [...selectedTags, tag];
    if (!next.length) choose({ kind: "all" });
    else choose({ kind: "tags", tags: next, tagsMode: view.kind === "tags" ? view.tagsMode : "any" });
  };

  return (
    <nav className="nw-sidebar" aria-label="Folders">
      <div
        className="nw-side-head"
        onDragOver={(event) => event.dataTransfer.types.includes(FOLDER_DRAG_TYPE) && event.preventDefault()}
        onDrop={(event) => {
          const dragged = event.dataTransfer.getData(FOLDER_DRAG_TYPE);
          if (dragged) controller.moveFolder(dragged, null);
        }}
      >
        <span>Folders</span>
      </div>
      <div className="nw-side-list">
        {folders.length > 0 && (
          <Entry
            icon="layers"
            label="All Notes"
            count={counts.all}
            active={view.kind === "all" && !view.q}
            onSelect={() => choose({ kind: "all" })}
          />
        )}
        <Entry
          icon="folder"
          label="Notes"
          count={counts.default}
          active={sameView(view, { kind: "folder", folderId: "default" }) && !view.q}
          onSelect={() => choose({ kind: "folder", folderId: "default" })}
          onDrop={dropNote(null)}
          dropKinds={[NOTE_DRAG_TYPE]}
        />
        {tree.map((folder) => (
          <Entry
            key={folder.id}
            icon="folder"
            label={folder.name}
            count={folder.count ?? 0}
            depth={folder.depth}
            active={sameView(view, { kind: "folder", folderId: folder.id }) && !view.q}
            onSelect={() => choose({ kind: "folder", folderId: folder.id })}
            onDrop={dropOnFolder(folder.id)}
            dropKinds={[NOTE_DRAG_TYPE, FOLDER_DRAG_TYPE]}
            dragData={{ type: FOLDER_DRAG_TYPE, value: folder.id }}
            menu={(at) => folderMenu(folder, at)}
            chevron={
              folder.children ? (
                <button
                  type="button"
                  className="nw-side-chevron"
                  aria-label={collapsed.has(folder.id) ? `Expand ${folder.name}` : `Collapse ${folder.name}`}
                  aria-expanded={!collapsed.has(folder.id)}
                  onClick={() =>
                    setCollapsed((current) => {
                      const next = new Set(current);
                      if (next.has(folder.id)) next.delete(folder.id);
                      else next.add(folder.id);
                      return next;
                    })
                  }
                />
              ) : (
                <span className="nw-side-chevron-space" aria-hidden="true" />
              )
            }
          />
        ))}
        {smartFolders.map((folder) => (
          <Entry
            key={folder.id}
            icon="smartFolder"
            label={folder.name}
            count={folder.count ?? 0}
            depth={0}
            active={view.kind === "smart" && view.smartId === folder.id && !view.q}
            onSelect={() => choose({ kind: "smart", smartId: folder.id, rules: folder.rules })}
            menu={(at) => smartMenu(folder, at)}
            chevron={<span className="nw-side-chevron-space" aria-hidden="true" />}
          />
        ))}
        {counts.archived > 0 && (
          <Entry
            icon="archive"
            label="Archived"
            count={counts.archived}
            active={view.kind === "archived" && !view.q}
            onSelect={() => choose({ kind: "archived" })}
          />
        )}
        <Entry
          icon="trash"
          label="Recently Deleted"
          count={counts.deleted || null}
          active={view.kind === "deleted" && !view.q}
          onSelect={() => choose({ kind: "deleted" })}
        />
        {tags.length > 0 && (
          <section className="nw-side-tags" aria-label="Tags">
            <div className="nw-side-head">
              <span>Tags</span>
              {selectedTags.length > 1 && (
                <button
                  type="button"
                  className="nw-side-mode"
                  onClick={() => choose({ kind: "tags", tags: selectedTags, tagsMode: view.tagsMode === "all" ? "any" : "all" })}
                >
                  {view.tagsMode === "all" ? "All selected" : "Any selected"}
                </button>
              )}
              {selectedTags.length > 0 && (
                <button
                  type="button"
                  className="nw-side-mode"
                  onClick={() =>
                    setDialog({ kind: "smart", initial: { name: selectedTags.map((tag) => `#${tag}`).join(" "), rules: tagRules(selectedTags, view.tagsMode) } })
                  }
                >
                  Save as Smart Folder
                </button>
              )}
            </div>
            <div className="nw-tag-chips">
              {tags.map(({ tag, count }) => (
                <TagChip
                  key={tag}
                  tag={tag}
                  count={count}
                  on={selectedTags.includes(tag)}
                  onToggle={() => toggleTag(tag)}
                  menu={(at) => tagMenu(tag, at)}
                />
              ))}
            </div>
          </section>
        )}
      </div>
      <div className="nw-side-foot">
        <button type="button" className="nw-side-new" onClick={() => setDialog({ kind: "new", parentId: null })}>
          <Icon name="plus" />
          New Folder
        </button>
        <button type="button" className="nw-side-new" onClick={() => setDialog({ kind: "smart", initial: null })}>
          <Icon name="smartFolder" />
          New Smart Folder
        </button>
      </div>
      {menu && <Menu items={menu.items} at={menu.at} label={menu.label} onClose={() => setMenu(null)} />}
      {dialog?.kind === "new" && (
        <PromptDialog
          title={dialog.parentId ? "New Subfolder" : "New Folder"}
          label="Name"
          initial="New Folder"
          confirmLabel="Create"
          onCancel={() => setDialog(null)}
          onSubmit={async (name) => {
            setDialog(null);
            const folder = await controller.createFolder(name, dialog.parentId);
            if (folder) choose({ kind: "folder", folderId: folder.id });
          }}
        />
      )}
      {dialog?.kind === "rename" && (
        <PromptDialog
          title="Rename Folder"
          label="Name"
          initial={dialog.folder.name}
          confirmLabel="Rename"
          onCancel={() => setDialog(null)}
          onSubmit={(name) => {
            setDialog(null);
            controller.renameFolder(dialog.folder.id, name);
          }}
        />
      )}
      {dialog?.kind === "smart" && (
        <SmartFolderDialog
          initial={dialog.initial}
          tags={tags.map(({ tag }) => tag)}
          folders={folders}
          onCancel={() => setDialog(null)}
          onSubmit={async ({ name, rules }) => {
            const editing = dialog.initial?.id;
            setDialog(null);
            const saved = editing
              ? await controller.updateSmartFolder(editing, { name, rules })
              : await controller.createSmartFolder(name, rules);
            if (saved) choose({ kind: "smart", smartId: saved.id, rules: saved.rules });
          }}
        />
      )}
      {dialog?.kind === "deleteSmart" && (
        <ConfirmDialog
          title={`Delete "${dialog.folder.name}"?`}
          message="Only the smart folder goes. Its notes stay where they are."
          confirmLabel="Delete Smart Folder"
          danger
          onCancel={() => setDialog(null)}
          onConfirm={() => {
            setDialog(null);
            controller.deleteSmartFolder(dialog.folder.id);
          }}
        />
      )}
      {dialog?.kind === "renameTag" && (
        <PromptDialog
          title={`Rename #${dialog.tag}`}
          label="New name"
          initial={dialog.tag}
          confirmLabel="Rename"
          onCancel={() => setDialog(null)}
          onSubmit={(name) => {
            setDialog(null);
            controller.renameTag(dialog.tag, name.replace(/^#+/, ""));
          }}
        />
      )}
      {dialog?.kind === "deleteTag" && (
        <ConfirmDialog
          title={`Delete #${dialog.tag}?`}
          message="It is taken out of every note it is written in. The notes themselves stay."
          confirmLabel="Delete Tag"
          danger
          onCancel={() => setDialog(null)}
          onConfirm={() => {
            setDialog(null);
            controller.deleteTag(dialog.tag);
          }}
        />
      )}
      {dialog?.kind === "delete" && (
        <ConfirmDialog
          title={`Delete "${dialog.folder.name}"?`}
          message="Its notes and subfolders' notes move to Recently Deleted, where they stay for 30 days."
          confirmLabel="Delete Folder"
          danger
          onCancel={() => setDialog(null)}
          onConfirm={() => {
            setDialog(null);
            controller.deleteFolder(dialog.folder.id);
          }}
        />
      )}
    </nav>
  );
}

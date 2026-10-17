/**
 * Checklists: the official task item, plus what Apple Notes does with one.
 *
 * Actions: check all, uncheck all, delete checked, move checked to the bottom,
 * and toggling the item(s) under the caret.
 *
 * "Sort checked items automatically" (a setting): when an item is ticked it sinks
 * below the open items of its list about half a second later, as in Apple Notes,
 * so the tick is seen before the row moves. Only lists where an item was just
 * ticked are touched, never every list in the note, and a list the person keeps
 * typing in is left alone until they pause.
 */
import { Fragment } from "@tiptap/pm/model";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { TaskItem } from "@tiptap/extension-list";

const sortKey = new PluginKey("checklistSort");
const SORT_DELAY = 500;

/** Items of a task list with checked ones after open ones, keeping each group's order. */
export function sortedItems(list) {
  const open = [];
  const done = [];
  list.forEach((item) => (item.attrs.checked ? done : open).push(item));
  return [...open, ...done];
}

function isSorted(list) {
  let seenChecked = false;
  let sorted = true;
  list.forEach((item) => {
    if (item.attrs.checked) seenChecked = true;
    else if (seenChecked) sorted = false;
  });
  return sorted;
}

function sortList(tr, pos) {
  const list = tr.doc.nodeAt(pos);
  if (!list || list.type.name !== "taskList" || isSorted(list)) return false;
  tr.replaceWith(pos + 1, pos + list.nodeSize - 1, Fragment.fromArray(sortedItems(list)));
  return true;
}

function taskListsIn(doc) {
  const found = [];
  doc.descendants((node, pos) => {
    if (node.type.name === "taskList") found.push(pos);
  });
  return found;
}

export const NoteTaskItem = TaskItem.extend({
  addOptions() {
    return {
      ...this.parent?.(),
      nested: true,
      autoSort: () => false,
    };
  },

  // The official view, except that checking an item does not focus the editor:
  // on a phone that would raise the keyboard, which Apple Notes never does for
  // a tap on a circle. The change is caught on the way down to the checkbox,
  // before the official handler (which focuses) sees it. Read-only notes still
  // go to the official handler, which puts the box back.
  addNodeView() {
    const official = this.parent?.();
    return (props) => {
      const view = official(props);
      const { editor, getPos } = props;
      view.dom.addEventListener(
        "change",
        (event) => {
          const input = event.target;
          if (input?.type !== "checkbox" || input.closest("li") !== view.dom || !editor.isEditable) return;
          event.stopPropagation();
          const position = typeof getPos === "function" ? getPos() : null;
          const current = typeof position === "number" ? editor.state.doc.nodeAt(position) : null;
          if (!current) return;
          editor.view.dispatch(editor.state.tr.setNodeMarkup(position, undefined, { ...current.attrs, checked: input.checked }));
        },
        true,
      );
      return view;
    };
  },

  addCommands() {
    return {
      ...this.parent?.(),
      setAllChecklistItems:
        (checked) =>
        ({ tr, state }) => {
          let changed = false;
          state.doc.descendants((node, pos) => {
            if (node.type === this.type && Boolean(node.attrs.checked) !== checked) {
              tr.setNodeMarkup(pos, undefined, { ...node.attrs, checked });
              changed = true;
            }
          });
          return changed;
        },
      deleteCheckedItems:
        () =>
        ({ tr, state }) => {
          const targets = [];
          state.doc.descendants((node, pos) => {
            if (node.type.name === "taskList") {
              let open = 0;
              node.forEach((item) => {
                if (!item.attrs.checked) open += 1;
              });
              if (open === 0) {
                targets.push({ from: pos, to: pos + node.nodeSize });
                return false;
              }
            }
            if (node.type === this.type && node.attrs.checked) {
              targets.push({ from: pos, to: pos + node.nodeSize });
              return false;
            }
            return true;
          });
          for (const { from, to } of targets.reverse()) {
            tr.delete(tr.mapping.map(from), tr.mapping.map(to));
          }
          return targets.length > 0;
        },
      moveCheckedItemsToBottom:
        () =>
        ({ tr, state }) => {
          let changed = false;
          for (const pos of taskListsIn(state.doc).reverse()) {
            changed = sortList(tr, tr.mapping.map(pos)) || changed;
          }
          return changed;
        },
      toggleChecklistItem:
        () =>
        ({ tr, state }) => {
          const { from, to } = state.selection;
          let changed = false;
          state.doc.nodesBetween(from, to, (node, pos) => {
            if (node.type === this.type) {
              tr.setNodeMarkup(pos, undefined, { ...node.attrs, checked: !node.attrs.checked });
              changed = true;
              return false;
            }
            return true;
          });
          if (!changed) {
            const $from = state.selection.$from;
            for (let depth = $from.depth; depth > 0; depth -= 1) {
              const node = $from.node(depth);
              if (node.type === this.type) {
                tr.setNodeMarkup($from.before(depth), undefined, { ...node.attrs, checked: !node.attrs.checked });
                return true;
              }
            }
          }
          return changed;
        },
    };
  },

  addProseMirrorPlugins() {
    const options = this.options;
    const itemType = () => this.type;
    return [
      ...(this.parent?.() ?? []),
      new Plugin({
        key: sortKey,
        state: {
          init: () => [],
          apply(tr, pending, oldState, newState) {
            // The sort itself (or its no-op) settles everything that was waiting.
            if (tr.getMeta(sortKey)) return [];
            if (!tr.docChanged) return pending;
            const next = pending.map((pos) => tr.mapping.map(pos));
            const back = tr.mapping.invert();
            tr.mapping.maps.forEach((map, index) => {
              map.forEach((oldStart, oldEnd, newStart, newEnd) => {
                const later = tr.mapping.slice(index + 1);
                const from = later.map(newStart, -1);
                const to = later.map(newEnd, 1);
                newState.doc.nodesBetween(from, Math.min(to, newState.doc.content.size), (node, pos) => {
                  if (node.type !== itemType() || !node.attrs.checked) return true;
                  const before = oldState.doc.nodeAt(back.map(pos));
                  if (before?.type === itemType() && !before.attrs.checked) {
                    const $pos = newState.doc.resolve(pos);
                    next.push($pos.before($pos.depth));
                  }
                  return false;
                });
              });
            });
            return [...new Set(next)].filter((pos) => newState.doc.nodeAt(pos)?.type.name === "taskList");
          },
        },
        view: () => {
          let timer = null;
          return {
            update(view) {
              const pending = sortKey.getState(view.state);
              clearTimeout(timer);
              if (!pending.length || !options.autoSort()) return;
              timer = setTimeout(() => {
                const lists = sortKey.getState(view.state);
                if (!lists.length) return;
                const tr = view.state.tr;
                let changed = false;
                for (const pos of [...lists].sort((a, b) => b - a)) changed = sortList(tr, pos) || changed;
                tr.setMeta(sortKey, true);
                if (changed) view.dispatch(tr);
                else view.dispatch(view.state.tr.setMeta(sortKey, true).setMeta("addToHistory", false));
              }, SORT_DELAY);
            },
            destroy() {
              clearTimeout(timer);
            },
          };
        },
      }),
    ];
  },
});

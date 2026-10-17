/**
 * Collapsible sections, as in Apple Notes: a Title, Heading or Subheading line can
 * fold away everything under it up to the next heading of the same or a higher
 * level.
 *
 * The folded state is an attribute of the heading, so it is part of the note and
 * survives a reload. Hiding is a decoration, never a change to the document: the
 * text is still there, still searchable, still in the Markdown copy. If the
 * caret ever lands inside a folded section -- arrow keys, find, undo -- the
 * section opens, because typing into something you cannot see is never right.
 *
 * Toggling is marked `noteUi`: for a note still held as Markdown, folding is a
 * view change and must not count as the edit that converts it.
 */
import { Decoration } from "@tiptap/core";
import Heading from "@tiptap/extension-heading";
import { Plugin, PluginKey } from "@tiptap/pm/state";

/** Top-level ranges hidden by folded headings: [{ from, to, by }] (`by` = heading pos). */
export function foldedRanges(doc) {
  const hidden = [];
  let fold = null;
  doc.forEach((node, offset) => {
    const isHeading = node.type.name === "heading";
    if (isHeading && fold && node.attrs.level <= fold.level) fold = null;
    if (fold) {
      hidden.push({ from: offset, to: offset + node.nodeSize, by: fold.pos });
      return;
    }
    if (isHeading && node.attrs.collapsed) fold = { level: node.attrs.level, pos: offset };
  });
  return hidden;
}

/** Whether a top-level heading at `pos` has anything under it to fold. */
function hasSection(doc, pos) {
  const heading = doc.nodeAt(pos);
  const next = doc.resolve(pos + heading.nodeSize).nodeAfter;
  return Boolean(next) && !(next.type.name === "heading" && next.attrs.level <= heading.attrs.level);
}

const sectionKey = new PluginKey("noteSections");

export const NoteHeading = Heading.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      collapsed: {
        default: false,
        keepOnSplit: false,
        parseHTML: (element) => element.getAttribute("data-collapsed") === "true",
        renderHTML: (attributes) => (attributes.collapsed ? { "data-collapsed": "true" } : {}),
      },
    };
  },

  addKeyboardShortcuts() {
    const kept = { ...(this.parent?.() ?? {}) };
    for (const key of Object.keys(kept)) {
      if (/^Mod-Alt-\d$/.test(key)) delete kept[key];
    }
    return kept;
  },

  addCommands() {
    return {
      ...this.parent?.(),
      toggleSection:
        (pos) =>
        ({ tr, state }) => {
          const node = state.doc.nodeAt(pos);
          if (!node || node.type.name !== "heading") return false;
          tr.setNodeMarkup(pos, undefined, { ...node.attrs, collapsed: !node.attrs.collapsed });
          tr.setMeta("noteUi", true);
          return true;
        },
    };
  },

  addDecorations() {
    const editor = this.editor;
    return {
      shouldUpdate: ({ tr }) => tr.docChanged,
      create: ({ state }) => {
        const decorations = foldedRanges(state.doc).map(({ from, to }) =>
          Decoration.Node(from, to, { class: "nw-folded", "aria-hidden": "true" }),
        );
        state.doc.forEach((node, offset) => {
          if (node.type.name !== "heading" || !hasSection(state.doc, offset)) return;
          const collapsed = Boolean(node.attrs.collapsed);
          decorations.push(
            Decoration.Widget(
              offset + 1,
              (view, getPos) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "nw-fold";
                button.contentEditable = "false";
                button.setAttribute("aria-expanded", String(!collapsed));
                button.setAttribute("aria-label", collapsed ? "Expand section" : "Collapse section");
                button.addEventListener("mousedown", (event) => event.preventDefault());
                button.addEventListener("click", () => {
                  const at = getPos();
                  if (at !== undefined) editor.commands.toggleSection(at - 1);
                });
                return button;
              },
              { key: `fold-${offset}-${collapsed}`, side: -1, ignoreSelection: true },
            ),
          );
        });
        return decorations;
      },
    };
  },

  addProseMirrorPlugins() {
    return [
      ...(this.parent?.() ?? []),
      new Plugin({
        key: sectionKey,
        appendTransaction: (transactions, oldState, newState) => {
          if (!transactions.some((tr) => tr.selectionSet || tr.docChanged)) return null;
          const head = newState.selection.head;
          const inside = foldedRanges(newState.doc).find(({ from, to }) => head > from && head < to);
          if (!inside) return null;
          const heading = newState.doc.nodeAt(inside.by);
          const tr = newState.tr.setNodeMarkup(inside.by, undefined, { ...heading.attrs, collapsed: false });
          return tr.setMeta("noteUi", true).setMeta("addToHistory", false);
        },
      }),
    ];
  },
});

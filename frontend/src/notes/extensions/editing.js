/**
 * Small editing behaviours with no official equivalent: hashtag highlighting,
 * moving a list item up or down, pasting Markdown as formatted text, and the
 * Apple paragraph styles and list kinds as named commands.
 */
import { Decoration, Extension } from "@tiptap/core";
import { Fragment } from "@tiptap/pm/model";
import { Plugin, PluginKey, TextSelection } from "@tiptap/pm/state";

import { findTags } from "../tags.js";

function tagDecorations(doc, from, to) {
  const decorations = [];
  doc.nodesBetween(from, to, (node, pos, parent) => {
    if (node.type.spec.code) return false;
    if (!node.isText) return true;
    if (parent?.type.spec.code) return false;
    if (node.marks.some((mark) => mark.type.name === "code" || mark.type.name === "link")) return false;
    for (const tag of findTags(node.text)) {
      decorations.push(Decoration.Inline(pos + tag.from, pos + tag.to, { class: "nw-tag" }));
    }
    return false;
  });
  return decorations;
}

/** Highlights #tags in the text; the text itself is never changed. */
export const Hashtags = Extension.create({
  name: "hashtags",
  addDecorations() {
    return {
      update: "changedRanges",
      create: ({ state }) => tagDecorations(state.doc, 0, state.doc.content.size),
      createInRange: ({ state, from, to }) => tagDecorations(state.doc, from, to),
    };
  },
});

/**
 * Highlights matches of the current search in the open note. The query lives in
 * the extension's storage; changing it dispatches a transaction with the
 * `searchHighlight` meta (never a document change, so never an edit).
 */
export const SearchHighlight = Extension.create({
  name: "searchHighlight",
  addStorage() {
    return { query: "" };
  },
  addDecorations() {
    const storage = this.storage;
    return {
      shouldUpdate: ({ tr }) => tr.docChanged || tr.getMeta("searchHighlight") !== undefined,
      create: ({ state }) => {
        const query = String(storage.query ?? "").trim().toLowerCase();
        if (query.length < 2) return [];
        const decorations = [];
        state.doc.descendants((node, pos) => {
          if (!node.isText) return true;
          const text = node.text.toLowerCase();
          for (let index = text.indexOf(query); index !== -1; index = text.indexOf(query, index + query.length)) {
            decorations.push(Decoration.Inline(pos + index, pos + index + query.length, { class: "nw-hit" }));
          }
          return false;
        });
        return decorations;
      },
    };
  },
});

/** Position of the first match of `query` in the document, or -1. */
export function firstMatch(doc, query) {
  const needle = String(query ?? "").trim().toLowerCase();
  if (needle.length < 2) return -1;
  let found = -1;
  doc.descendants((node, pos) => {
    if (found !== -1) return false;
    if (!node.isText) return true;
    const index = node.text.toLowerCase().indexOf(needle);
    if (index !== -1) found = pos + index;
    return false;
  });
  return found;
}

function moveItem(direction) {
  return ({ state, tr, dispatch }) => {
    const { $from } = state.selection;
    for (let depth = $from.depth; depth > 0; depth -= 1) {
      const node = $from.node(depth);
      if (node.type.name !== "listItem" && node.type.name !== "taskItem") continue;
      const index = $from.index(depth - 1);
      const parent = $from.node(depth - 1);
      const target = direction === "up" ? index - 1 : index + 1;
      if (target < 0 || target >= parent.childCount) return false;
      const itemPos = $from.before(depth);
      const other = parent.child(target);
      const caret = state.selection.from - itemPos;
      if (dispatch) {
        if (direction === "up") {
          const start = itemPos - other.nodeSize;
          tr.replaceWith(start, itemPos + node.nodeSize, Fragment.fromArray([node, other]));
          tr.setSelection(TextSelection.create(tr.doc, start + caret));
        } else {
          const end = itemPos + node.nodeSize + other.nodeSize;
          tr.replaceWith(itemPos, end, Fragment.fromArray([other, node]));
          tr.setSelection(TextSelection.create(tr.doc, itemPos + other.nodeSize + caret));
        }
        tr.scrollIntoView();
      }
      return true;
    }
    return false;
  };
}

/** Markdown people paste from chat or another app: block signals, not stray symbols. */
export function looksLikeMarkdown(text) {
  const lines = String(text ?? "").split(/\r?\n/);
  if (lines.length < 2 && !/\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)/.test(text)) return false;
  let score = 0;
  if (lines.some((line) => /^\s{0,3}(```|~~~)/.test(line))) score += 2;
  if (lines.some((line) => /^\s*[-*+]\s+\[[ xX]\]\s/.test(line))) score += 2;
  if (lines.some((line, i) => /\|/.test(line) && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[i + 1] ?? ""))) score += 2;
  if (lines.some((line) => /^\s{0,3}#{1,6}\s+\S/.test(line))) score += 1;
  if (lines.filter((line) => /^\s*([-*+]|\d+[.)])\s+\S/.test(line)).length >= 2) score += 1;
  if (lines.some((line) => /^\s{0,3}>\s/.test(line))) score += 1;
  if (/\*\*[^*\n]+\*\*/.test(text)) score += 1;
  if (/\[[^\]\n]+\]\((https?:|mailto:)[^)\s]+\)/.test(text)) score += 1;
  return score >= 2;
}

const pasteKey = new PluginKey("markdownPaste");

/**
 * A Shift chord is never read as the chord without Shift. Some browsers report
 * Cmd+Shift+U with key "u", and ProseMirror then retries it as Mod-u
 * (underline), so the keystroke never reaches Neo's own Cmd+Shift+U. Here the
 * editor's keymaps see it as the capital Shift produces; when nothing in the
 * editor is bound to that, the keystroke is left alone for Neo's commands.
 */
export const ShiftChords = Extension.create({
  name: "shiftChords",
  priority: 10000,
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey("shiftChords"),
        props: {
          handleDOMEvents: {
            keydown(view, event) {
              const { key } = event;
              if (!event.shiftKey || !(event.metaKey || event.ctrlKey) || key.length !== 1 || key === key.toUpperCase()) return false;
              const capital = new KeyboardEvent("keydown", {
                key: key.toUpperCase(),
                code: event.code,
                shiftKey: true,
                metaKey: event.metaKey,
                ctrlKey: event.ctrlKey,
                altKey: event.altKey,
                cancelable: true,
              });
              Object.defineProperty(capital, "keyCode", { get: () => event.keyCode });
              if (view.someProp("handleKeyDown", (handle) => handle(view, capital))) event.preventDefault();
              // Handled or not, ProseMirror does not also try the lower case.
              return true;
            },
          },
        },
      }),
    ];
  },
});

/** Lists, styles and actions under Apple Notes' names, so the toolbar and the keys share them. */
export const NoteCommands = Extension.create({
  name: "noteCommands",

  addCommands() {
    return {
      setNoteStyle:
        (style) =>
        ({ commands }) => {
          if (style === "title") return commands.setHeading({ level: 1 });
          if (style === "heading") return commands.setHeading({ level: 2 });
          if (style === "subheading") return commands.setHeading({ level: 3 });
          if (style === "mono") return commands.toggleCodeBlock();
          return commands.setParagraph();
        },
      toggleNoteList:
        (kind) =>
        ({ editor, chain }) => {
          if (kind === "numbered") return chain().toggleOrderedList().run();
          if (kind === "checklist") return chain().toggleTaskList().run();
          const variant = kind === "dashed" ? "dash" : "bullet";
          if (editor.isActive("bulletList")) {
            if (editor.getAttributes("bulletList").variant === variant) {
              return chain().toggleBulletList().run();
            }
            return chain().updateAttributes("bulletList", { variant }).run();
          }
          return chain().toggleBulletList().updateAttributes("bulletList", { variant }).run();
        },
      moveListItemUp: () => moveItem("up"),
      moveListItemDown: () => moveItem("down"),
    };
  },

  addKeyboardShortcuts() {
    // Apple's chord is Control-Command-Arrow. Elsewhere "Mod" is Control itself,
    // so the portable Alt-Shift-Arrow is the chord there (and also works on a Mac).
    const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform ?? "");
    const appleChords = mac
      ? {
          "Ctrl-Meta-ArrowUp": () => this.editor.commands.moveListItemUp(),
          "Ctrl-Meta-ArrowDown": () => this.editor.commands.moveListItemDown(),
        }
      : {};
    return {
      ...appleChords,
      "Alt-Shift-ArrowUp": () => this.editor.commands.moveListItemUp(),
      "Alt-Shift-ArrowDown": () => this.editor.commands.moveListItemDown(),
      "Mod-]": () => this.editor.commands.sinkListItem(this.editor.isActive("taskItem") ? "taskItem" : "listItem"),
      "Mod-[": () => this.editor.commands.liftListItem(this.editor.isActive("taskItem") ? "taskItem" : "listItem"),
    };
  },

  addProseMirrorPlugins() {
    const editor = this.editor;
    return [
      new Plugin({
        key: pasteKey,
        props: {
          handlePaste(view, event) {
            const data = event.clipboardData;
            if (!data || data.files?.length || data.types.includes("text/html")) return false;
            if (view.state.selection.$from.parent.type.spec.code) return false;
            const text = data.getData("text/plain");
            if (!text || !looksLikeMarkdown(text) || !editor.markdown) return false;
            const parsed = editor.markdown.parse(text);
            if (!parsed?.content?.length) return false;
            return editor.commands.insertContent(parsed.content);
          },
        },
      }),
    ];
  },
});

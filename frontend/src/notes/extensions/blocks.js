/**
 * The official block nodes, adjusted in two ways for Notes.
 *
 * Shortcuts: formatting chords (title, heading, list kinds, checklist, monostyled,
 * quote) are Neo commands, rebindable and listed in the palette, so the copies
 * these extensions bind on their own are removed. Each chord then has exactly one
 * owner. Editing keys (Enter, Backspace, Tab) are untouched.
 *
 * Lists: Apple Notes has a bulleted list (•) and a dashed list (–). They are the
 * same official bullet list with a `variant`; "* " starts a bulleted one and "- "
 * a dashed one, as in Apple Notes. Markdown has one kind of bullet, so both are
 * written as "- " -- the variant lives in the document, which is the note.
 */
import { wrappingInputRule } from "@tiptap/core";
import Blockquote from "@tiptap/extension-blockquote";
import CodeBlock from "@tiptap/extension-code-block";
import Paragraph from "@tiptap/extension-paragraph";
import { BulletList, OrderedList, TaskList } from "@tiptap/extension-list";

function without(shortcuts, isOwnedByNeo) {
  const kept = { ...(shortcuts ?? {}) };
  for (const key of Object.keys(kept)) {
    if (isOwnedByNeo(key)) delete kept[key];
  }
  return kept;
}

export const NoteParagraph = Paragraph.extend({
  addKeyboardShortcuts() {
    return without(this.parent?.(), (key) => key === "Mod-Alt-0");
  },
});

export const NoteBlockquote = Blockquote.extend({
  addKeyboardShortcuts() {
    return without(this.parent?.(), (key) => key === "Mod-Shift-b");
  },
});

export const NoteCodeBlock = CodeBlock.extend({
  addKeyboardShortcuts() {
    return without(this.parent?.(), (key) => key === "Mod-Alt-c");
  },
});

export const NoteOrderedList = OrderedList.extend({
  addKeyboardShortcuts() {
    return without(this.parent?.(), (key) => key === "Mod-Shift-7");
  },
});

export const NoteTaskList = TaskList.extend({
  addKeyboardShortcuts() {
    return without(this.parent?.(), (key) => key === "Mod-Shift-9");
  },
});

export const NoteBulletList = BulletList.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      variant: {
        default: "bullet",
        parseHTML: (element) => (element.getAttribute("data-variant") === "dash" ? "dash" : "bullet"),
        renderHTML: (attributes) => ({ "data-variant": attributes.variant === "dash" ? "dash" : "bullet" }),
      },
    };
  },
  addKeyboardShortcuts() {
    return without(this.parent?.(), (key) => key === "Mod-Shift-8");
  },
  addInputRules() {
    return [
      wrappingInputRule({ find: /^\s*([*•])\s$/, type: this.type, getAttributes: { variant: "bullet" } }),
      wrappingInputRule({ find: /^\s*([-+])\s$/, type: this.type, getAttributes: { variant: "dash" } }),
    ];
  },
});

/**
 * The one list of editor extensions a note is made of.
 *
 * The editor, the Markdown import and the save-time projection all build from
 * this list, so what the editor can hold, what Markdown converts into, and what a
 * saved note is serialised from are the same schema by construction.
 *
 * Official TipTap first: StarterKit, lists, task lists, tables, text colour,
 * placeholder, character count and Markdown. The handful of local pieces (in
 * ./extensions) only add what Apple Notes has and TipTap does not.
 */
import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import Bold from "@tiptap/extension-bold";
import Italic from "@tiptap/extension-italic";
import Underline from "@tiptap/extension-underline";
import { TableKit } from "@tiptap/extension-table";
import { Color, TextStyle } from "@tiptap/extension-text-style";
import { CharacterCount, Placeholder } from "@tiptap/extensions";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";

import {
  NoteBlockquote,
  NoteBulletList,
  NoteCodeBlock,
  NoteOrderedList,
  NoteParagraph,
  NoteTaskList,
} from "./extensions/blocks.js";
import { Attachments, FileAttachment, NoteImage } from "./extensions/attachments.js";
import { NoteTaskItem } from "./extensions/checklist.js";
import { NoteLink, NoteUrlLinks } from "./extensions/noteLinks.js";
import { Sketch } from "./extensions/sketch.js";
import { Hashtags, NoteCommands, SearchHighlight, ShiftChords } from "./extensions/editing.js";
import { NoteHeading } from "./extensions/sections.js";

/** Apple Notes' five text colours. Each is a theme token, so it reads in light and dark. */
export const NOTE_COLORS = [
  { id: "purple", label: "Purple", value: "var(--nw-ink-purple)" },
  { id: "pink", label: "Pink", value: "var(--nw-ink-pink)" },
  { id: "orange", label: "Orange", value: "var(--nw-ink-orange)" },
  { id: "mint", label: "Mint", value: "var(--nw-ink-mint)" },
  { id: "blue", label: "Blue", value: "var(--nw-ink-blue)" },
];

const PALETTE = new Set(NOTE_COLORS.map((color) => color.value));

/** Colour from pasted pages is dropped unless it is one of the five: notes follow the theme. */
export function stripForeignColors(html) {
  return String(html ?? "").replace(/color\s*:\s*([^;"']+);?/gi, (match, value) =>
    PALETTE.has(value.trim()) ? match : "",
  );
}

const pasteColorKey = new PluginKey("noteColorPaste");
const ColorPaste = Extension.create({
  name: "noteColorPaste",
  addProseMirrorPlugins() {
    return [new Plugin({ key: pasteColorKey, props: { transformPastedHTML: stripForeignColors } })];
  },
});

/*
 * Bold, italic and underline answer Mod+B, I and U only. TipTap also binds the
 * capitals (for Caps Lock, which ProseMirror's key-code fallback already
 * covers), and ProseMirror retries a Shift chord without Shift, so Cmd+Shift+U
 * would underline instead of reaching "Mark as checked".
 */
const NoteBold = Bold.extend({
  addKeyboardShortcuts() {
    return { "Mod-b": () => this.editor.commands.toggleBold() };
  },
});
const NoteItalic = Italic.extend({
  addKeyboardShortcuts() {
    return { "Mod-i": () => this.editor.commands.toggleItalic() };
  },
});
const NoteUnderline = Underline.extend({
  addKeyboardShortcuts() {
    return { "Mod-u": () => this.editor.commands.toggleUnderline() };
  },
});

/**
 * `upload(file)` stores a picture or file and resolves with the attachment;
 * without it (the import guard, tests) pasted and dropped files are ignored.
 */
export function noteExtensions({ autoSortChecked = () => false, upload = null, onAttachmentError = null, currentNoteToken = null, resolveFile = null, noteLinks = {}, onSketchEdit = null } = {}) {
  return [
    StarterKit.configure({
      heading: false,
      paragraph: false,
      bulletList: false,
      orderedList: false,
      codeBlock: false,
      blockquote: false,
      bold: false,
      italic: false,
      underline: false,
      undoRedo: { depth: 300, newGroupDelay: 600 },
      link: {
        openOnClick: true,
        autolink: true,
        linkOnPaste: true,
        defaultProtocol: "https",
        // note://<id>: a link to another note, as the Markdown mirror writes one.
        protocols: ["note"],
        HTMLAttributes: { rel: "noopener noreferrer nofollow", target: "_blank" },
      },
    }),
    NoteBold,
    NoteItalic,
    NoteUnderline,
    NoteParagraph,
    NoteHeading.configure({ levels: [1, 2, 3, 4, 5, 6] }),
    NoteBulletList,
    NoteOrderedList,
    NoteTaskList,
    NoteTaskItem.configure({ nested: true, autoSort: autoSortChecked }),
    NoteCodeBlock,
    NoteBlockquote,
    TableKit.configure({ table: { resizable: false } }),
    NoteImage,
    FileAttachment,
    NoteLink.configure(noteLinks),
    NoteUrlLinks.configure({ onOpen: (id) => noteLinks.onOpen?.(id) }),
    Sketch.configure({ onEdit: onSketchEdit }),
    Attachments.configure({ upload, onError: onAttachmentError, currentNoteToken, resolveFile }),
    TextStyle,
    Color,
    ColorPaste,
    Placeholder.configure({
      placeholder: ({ pos }) => (pos === 0 ? "Title" : ""),
      showOnlyCurrent: false,
    }),
    CharacterCount,
    Hashtags,
    SearchHighlight,
    NoteCommands,
    ShiftChords,
    Markdown.configure({ indentation: { style: "space", size: 2 } }),
  ];
}

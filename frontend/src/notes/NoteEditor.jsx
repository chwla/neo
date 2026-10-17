import { Suspense, lazy, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { EditorState } from "@tiptap/pm/state";
import { EditorContent, useEditor, useEditorState } from "@tiptap/react";

import Icon from "../WorkspaceIcon.jsx";
import { api } from "../api.js";
import { useCommandHandlers } from "../keys/useCommands.js";
import { registerModal } from "../modalStack.js";
import { insertAttachments } from "./extensions/attachments.js";
import { firstMatch } from "./extensions/editing.js";
import { foldedRanges } from "./extensions/sections.js";
import { longDate } from "./noteDates.js";
import { noteCommandHandlers } from "./noteCommands.js";
import { NOTE_COLORS, noteExtensions } from "./noteSchema.js";
import { firstLine } from "./projection.js";
import { sketchFromAttrs } from "./sketch/strokes.js";

// Drawing is opened now and then; its code arrives when it is.
const SketchEditor = lazy(() => import("./SketchEditor.jsx"));
import { Menu } from "./ui.jsx";

const STYLES = [
  { id: "title", label: "Title" },
  { id: "heading", label: "Heading" },
  { id: "subheading", label: "Subheading" },
  { id: "body", label: "Body" },
  { id: "mono", label: "Monostyled" },
];

function styleOf(editor) {
  if (editor.isActive("codeBlock")) return "mono";
  const level = editor.getAttributes("heading").level;
  if (editor.isActive("heading")) return level === 1 ? "title" : level === 2 ? "heading" : "subheading";
  return "body";
}

/** Pressing a toolbar button must not move focus out of the text being edited. */
function keepEditorFocus(event) {
  if (event.target.closest("button")) event.preventDefault();
}


const STATUS_TEXT = {
  offline: "Offline. Saved on this device.",
  error: "Not saved yet. Retrying.",
  invalid: "This note couldn't be saved.",
};

/** A popover that closes on Escape (through the modal stack) and on an outside press. */
function Popover({ open, onClose, className, label, children }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const unregister = registerModal(onClose);
    const onPointer = (event) => {
      if (ref.current && !ref.current.contains(event.target)) onClose();
    };
    document.addEventListener("pointerdown", onPointer, true);
    return () => {
      unregister();
      document.removeEventListener("pointerdown", onPointer, true);
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div ref={ref} className={className} role="dialog" aria-label={label}>
      {children}
    </div>
  );
}

function FormatPanel({ editor, active, onClose }) {
  const run = (fn) => () => {
    fn(editor.chain().focus()).run();
  };
  return (
    <>
      <div className="nw-format-styles" role="group" aria-label="Paragraph style">
        {STYLES.map((style) => (
          <button
            key={style.id}
            type="button"
            className={`nw-style nw-style-${style.id} ${active.style === style.id ? "on" : ""}`.trim()}
            aria-pressed={active.style === style.id}
            onClick={() => {
              editor.chain().focus().setNoteStyle(style.id).run();
              onClose();
            }}
          >
            {style.label}
          </button>
        ))}
      </div>
      <div className="nw-format-row" role="group" aria-label="Text style">
        <button type="button" className={active.bold ? "on" : ""} aria-pressed={active.bold} aria-label="Bold" onClick={run((c) => c.toggleBold())}><b>B</b></button>
        <button type="button" className={active.italic ? "on" : ""} aria-pressed={active.italic} aria-label="Italic" onClick={run((c) => c.toggleItalic())}><i>I</i></button>
        <button type="button" className={active.underline ? "on" : ""} aria-pressed={active.underline} aria-label="Underline" onClick={run((c) => c.toggleUnderline())}><u>U</u></button>
        <button type="button" className={active.strike ? "on" : ""} aria-pressed={active.strike} aria-label="Strikethrough" onClick={run((c) => c.toggleStrike())}><s>S</s></button>
        <span className="nw-format-sep" aria-hidden="true" />
        {NOTE_COLORS.map((color) => (
          <button
            key={color.id}
            type="button"
            className={`nw-swatch nw-swatch-${color.id} ${active.color === color.value ? "on" : ""}`.trim()}
            aria-pressed={active.color === color.value}
            aria-label={`${color.label} text`}
            onClick={run((c) => (active.color === color.value ? c.unsetColor() : c.setColor(color.value)))}
          />
        ))}
      </div>
      <div className="nw-format-row" role="group" aria-label="Lists">
        <button type="button" className={active.bullet ? "on" : ""} aria-pressed={active.bullet} aria-label="Bulleted list" onClick={run((c) => c.toggleNoteList("bulleted"))}><Icon name="bullets" /></button>
        <button type="button" className={active.dash ? "on" : ""} aria-pressed={active.dash} aria-label="Dashed list" onClick={run((c) => c.toggleNoteList("dashed"))}><Icon name="dashes" /></button>
        <button type="button" className={active.ordered ? "on" : ""} aria-pressed={active.ordered} aria-label="Numbered list" onClick={run((c) => c.toggleNoteList("numbered"))}><Icon name="numbers" /></button>
        <button type="button" className={active.quote ? "on" : ""} aria-pressed={active.quote} aria-label="Block quote" onClick={run((c) => c.toggleBlockquote())}><Icon name="quote" /></button>
        <span className="nw-format-sep" aria-hidden="true" />
        <button type="button" aria-label="Outdent" onClick={run((c) => c.liftListItem(active.checklist ? "taskItem" : "listItem"))}><Icon name="outdent" /></button>
        <button type="button" aria-label="Indent" onClick={run((c) => c.sinkListItem(active.checklist ? "taskItem" : "listItem"))}><Icon name="indent" /></button>
      </div>
    </>
  );
}

function TableBar({ editor }) {
  const act = (fn) => () => fn(editor.chain().focus()).run();
  return (
    <div className="nw-tablebar" role="toolbar" aria-label="Table">
      <button type="button" onClick={act((c) => c.addRowBefore())}>Row above</button>
      <button type="button" onClick={act((c) => c.addRowAfter())}>Row below</button>
      <button type="button" onClick={act((c) => c.deleteRow())}>Delete row</button>
      <button type="button" onClick={act((c) => c.addColumnBefore())}>Column left</button>
      <button type="button" onClick={act((c) => c.addColumnAfter())}>Column right</button>
      <button type="button" onClick={act((c) => c.deleteColumn())}>Delete column</button>
      <button type="button" className="danger" onClick={act((c) => c.deleteTable())}>Delete table</button>
    </div>
  );
}

function LinkEditor({ editor, onClose }) {
  const [href, setHref] = useState(() => editor.getAttributes("link").href ?? "");
  const input = useRef(null);
  useEffect(() => input.current?.focus(), []);
  function apply(event) {
    event.preventDefault();
    const value = href.trim();
    const chain = editor.chain().focus().extendMarkRange("link");
    if (!value) chain.unsetLink().run();
    else if (editor.state.selection.empty && !editor.isActive("link")) {
      chain.insertContent({ type: "text", text: value, marks: [{ type: "link", attrs: { href: value } }] }).run();
    } else chain.setLink({ href: value }).run();
    onClose();
  }
  return (
    <form className="nw-link-editor" onSubmit={apply}>
      <input
        ref={input}
        type="url"
        value={href}
        onChange={(event) => setHref(event.target.value)}
        placeholder="https://"
        aria-label="Link address"
      />
      <button type="submit">{href.trim() ? "Apply" : "Remove link"}</button>
    </form>
  );
}

function Banner({ session, status, controller, onShowOriginal }) {
  if (session.blocked === "conflict" && session.blockedDetail?.reason === "deleted") {
    return (
      <div className="nw-banner warn" role="alert">
        <span>This note was deleted somewhere else. Your changes are kept.</span>
        <button type="button" onClick={() => controller.resolve("restore-and-keep")}>Restore with my changes</button>
        <button type="button" onClick={() => controller.resolve("keep-both")}>Save mine as a new note</button>
        <button type="button" onClick={() => controller.resolve("keep-theirs")}>Discard mine</button>
      </div>
    );
  }
  if (session.blocked === "conflict") {
    return (
      <div className="nw-banner warn" role="alert">
        <span>This note changed in another window or by Neo. Both versions are kept until you choose.</span>
        <button type="button" onClick={() => controller.resolve("keep-mine")}>Keep mine</button>
        <button type="button" onClick={() => controller.resolve("keep-theirs")}>Keep theirs</button>
        {/* A locked note's ciphertext can't become another note's. */}
        {session.format !== "encrypted" && <button type="button" onClick={() => controller.resolve("keep-both")}>Keep both</button>}
      </div>
    );
  }
  if (session.blocked === "locked") {
    return (
      <div className="nw-banner warn" role="alert">
        <span>This note was locked in another window. Your changes are kept on this device until it's unlocked.</span>
        <button type="button" onClick={() => controller.keepLockedEdit()}>Unlock and keep my changes</button>
      </div>
    );
  }
  if (session.blocked === "gone") {
    return (
      <div className="nw-banner warn" role="alert">
        <span>This note was permanently deleted. Your version is still on this device.</span>
        <button type="button" onClick={() => controller.resolve("new-note")}>Save as a new note</button>
      </div>
    );
  }
  if (session.lossy && !session.lossyAccepted) {
    return (
      <div className="nw-banner" role="status">
        <span>
          This note has {session.reasons?.includes("HTML") ? "HTML" : "formatting"} the editor can't show exactly, so it's
          open read-only and nothing has changed. Editing converts it; the original stays in Versions.
        </span>
        <button type="button" onClick={onShowOriginal}>View original</button>
        <button type="button" onClick={() => controller.editAnyway()}>Edit anyway</button>
      </div>
    );
  }
  if (session.note?.deleted) {
    return (
      <div className="nw-banner" role="status">
        <span>This note is in Recently Deleted.</span>
      </div>
    );
  }
  if (STATUS_TEXT[status]) {
    return (
      <div className={`nw-banner ${status === "offline" ? "" : "warn"}`.trim()} role="status">
        <span>{STATUS_TEXT[status]}</span>
        {status !== "invalid" && (
          <button type="button" onClick={() => controller.flushCurrent()}>Retry now</button>
        )}
      </div>
    );
  }
  return null;
}

/**
 * The note editor: one TipTap editor for every note.
 *
 * Switching notes swaps in a fresh EditorState (fresh undo history) after the
 * old note's content is frozen for the saver. Typing does not re-render this
 * component: only the toolbar subscribes to editor state, through a selector.
 */
function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function NoteEditor({
  controller,
  session,
  status,
  settings,
  query = "",
  toolbarStart = null,
  toolbarEnd = null,
  noteActions = [],
}) {
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const shownKey = useRef(null);
  const [panel, setPanel] = useState(null);
  const [showOriginal, setShowOriginal] = useState(false);
  const [menuAt, setMenuAt] = useState(null);
  const [attachAt, setAttachAt] = useState(null);
  const [sketch, setSketch] = useState(null);
  const editorRef = useRef(null);
  const openSketch = (pos, isNew = false) => {
    const instance = editorRef.current;
    const node = instance?.state.doc.nodeAt(pos);
    if (!instance || !instance.isEditable || node?.type.name !== "sketch") return;
    setSketch({ pos, isNew, initial: sketchFromAttrs(node.attrs) });
  };
  const photoInput = useRef(null);
  const fileInput = useRef(null);

  // In a locked note an attachment is encrypted here before it is uploaded.
  const currentNoteToken = () => sessionRef.current?.token;
  const upload = (file, originToken) => {
    if (currentNoteToken() !== originToken) throw new Error("This note changed while the attachment was being added.");
    return sessionRef.current?.format === "encrypted" ? controller.uploadEncrypted(file) : api.uploadNoteAttachment(file);
  };
  const attachmentError = (message) => controller.notify(message);
  const extensions = useMemo(
    () =>
      noteExtensions({
        autoSortChecked: () => settingsRef.current?.checked_items === "auto",
        upload,
        onAttachmentError: attachmentError,
        currentNoteToken,
        resolveFile: (attrs) => controller.resolveFile(attrs),
        onSketchEdit: (pos) => openSketch(pos),
        noteLinks: {
          search: (query) => controller.searchNoteTitles(query, sessionRef.current?.id ?? null),
          titleOf: controller.noteTitleOf,
          subscribe: controller.subscribeTitles,
          onOpen: (id) => controller.revealNote(id),
        },
      }),
    [], // eslint-disable-line react-hooks/exhaustive-deps -- the editor is made once; both close over stable things
  );

  const editor = useEditor(
    {
      extensions,
      immediatelyRender: true,
      shouldRerenderOnTransaction: false,
      editorProps: {
        attributes: {
          class: "nw-prose",
          "aria-label": "Note",
          spellcheck: "true",
          autocapitalize: "sentences",
        },
        handleKeyDown: (view, event) => {
          if ((event.metaKey || event.ctrlKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k") {
            setPanel("link");
            return true;
          }
          return false;
        },
      },
      onUpdate: ({ editor: instance, transaction }) => {
        const current = sessionRef.current;
        if (!current || current.readOnly || !transaction.docChanged) return;
        if (transaction.getMeta("noteUi") && current.format === "markdown") return;
        controller.edit(current.key);
        controller.liveRowChanged(current.key, instance.state.doc.toJSON());
      },
    },
    [],
  );
  editorRef.current = editor;

  // Show a different note: freeze the one leaving, then swap in a fresh state.
  useLayoutEffect(() => {
    if (!editor || !session?.doc) return;
    if (shownKey.current && shownKey.current !== session.key) controller.captureKey(shownKey.current);
    shownKey.current = session.key;
    // Only the document: the Markdown projection is made once per send, not on
    // every keystroke's journal write.
    controller.attachEditor(session.key, () => ({ doc: editor.state.doc.toJSON() }));
    let doc;
    try {
      doc = editor.schema.nodeFromJSON(session.doc);
    } catch {
      doc = editor.schema.nodeFromJSON({ type: "doc", content: [{ type: "paragraph" }] });
    }
    editor.view.updateState(EditorState.create({ schema: editor.schema, doc, plugins: editor.state.plugins }));
    editor.setEditable(!session.readOnly, false);
    setPanel(null);
    setShowOriginal(false);
    if (session.isNew) requestAnimationFrame(() => editor.commands.focus("start"));
  }, [editor, session?.token]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    editor?.setEditable(!session?.readOnly, false);
  }, [editor, session?.readOnly]);

  // The list's search, highlighted in the open note; a hit in a folded section opens it.
  useEffect(() => {
    if (!editor || editor.isDestroyed) return;
    editor.storage.searchHighlight.query = query;
    editor.view.dispatch(editor.state.tr.setMeta("searchHighlight", query).setMeta("addToHistory", false));
    const hit = firstMatch(editor.state.doc, query);
    if (hit === -1) return;
    const hidden = foldedRanges(editor.state.doc).find(({ from, to }) => hit > from && hit < to);
    if (hidden) editor.commands.toggleSection(hidden.by);
  }, [editor, query, session?.token]);

  const run = (apply) => {
    if (!editor || !editor.isEditable) return false;
    apply(editor.chain().focus()).run();
    return true;
  };
  useCommandHandlers(noteCommandHandlers(() => editor));

  // With the keyboard up the formatting bar is docked over the bottom of what
  // can be seen, and the browser only keeps the caret inside the visible area,
  // not clear of the bar. Whenever the caret moves, or the keyboard comes up,
  // the note scrolls so the line being written stays above it.
  useEffect(() => {
    if (!editor) return undefined;
    let frame = 0;
    const keepCaretClear = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (editor.isDestroyed || !editor.isFocused) return;
        const root = editor.view.dom.closest(".nw");
        if (root?.dataset.keyboard !== "open") return;
        const bar = root.querySelector(".nw-toolbar")?.getBoundingClientRect();
        const page = editor.view.dom.closest(".nw-page");
        if (!bar || !page) return;
        const caret = editor.view.coordsAtPos(editor.state.selection.head);
        const overlap = caret.bottom + 12 - bar.top;
        if (overlap > 0) page.scrollBy({ top: overlap });
      });
    };
    editor.on("selectionUpdate", keepCaretClear);
    editor.on("focus", keepCaretClear);
    window.visualViewport?.addEventListener("resize", keepCaretClear);
    return () => {
      cancelAnimationFrame(frame);
      editor.off("selectionUpdate", keepCaretClear);
      editor.off("focus", keepCaretClear);
      window.visualViewport?.removeEventListener("resize", keepCaretClear);
    };
  }, [editor]);

  const active = useEditorState({
    editor,
    selector: ({ editor: instance }) => {
      if (!instance) return null;
      return {
        style: styleOf(instance),
        bold: instance.isActive("bold"),
        italic: instance.isActive("italic"),
        underline: instance.isActive("underline"),
        strike: instance.isActive("strike"),
        bullet: instance.isActive("bulletList", { variant: "bullet" }),
        dash: instance.isActive("bulletList", { variant: "dash" }),
        ordered: instance.isActive("orderedList"),
        checklist: instance.isActive("taskList"),
        quote: instance.isActive("blockquote"),
        table: instance.isActive("table"),
        link: instance.isActive("link"),
        color: instance.getAttributes("textStyle").color ?? null,
        editable: instance.isEditable,
        canUndo: instance.can().undo(),
        canRedo: instance.can().redo(),
      };
    },
  });

  if (!session) return null;
  const note = session.note ?? {};
  const disabled = !active?.editable;

  function markdownOfNote() {
    return editor.markdown.serialize(editor.getJSON()).trim();
  }

  function editorMenuItems() {
    const hasChecklist = editor.state.doc.toString().includes("taskItem");
    const name = `${(firstLine(editor.getJSON()).text || "Note").replace(/[\\/:*?"<>|]+/g, " ").trim()}.md`;
    return [
      ...noteActions,
      hasChecklist && !disabled && {
        label: "Checklist",
        items: [
          { label: "Check All", onSelect: () => editor.chain().focus().setAllChecklistItems(true).run() },
          { label: "Uncheck All", onSelect: () => editor.chain().focus().setAllChecklistItems(false).run() },
          { label: "Move Checked to Bottom", onSelect: () => editor.chain().focus().moveCheckedItemsToBottom().run() },
          { label: "Delete Checked", onSelect: () => editor.chain().focus().deleteCheckedItems().run() },
        ],
      },
      { separator: true },
      { label: "Copy as Markdown", onSelect: () => navigator.clipboard?.writeText(markdownOfNote()).catch(() => {}) },
      { label: "Export as Markdown", onSelect: () => download(name, `${markdownOfNote()}\n`) },
      { label: "Print", onSelect: () => window.print() },
    ];
  }

  return (
    <div className="nw-editor">
      <div
        className="nw-toolbar"
        role="toolbar"
        aria-label="Formatting"
        onMouseDown={keepEditorFocus}
      >
        {toolbarStart}
        <div className="nw-tool-group">
          <button
            type="button"
            className={`nw-tool ${panel === "format" ? "on" : ""}`.trim()}
            aria-label="Format"
            aria-haspopup="dialog"
            aria-expanded={panel === "format"}
            disabled={disabled}
            onClick={() => setPanel(panel === "format" ? null : "format")}
          >
            <Icon name="format" />
          </button>
          <button
            type="button"
            className={`nw-tool ${active?.checklist ? "on" : ""}`.trim()}
            aria-label="Checklist"
            aria-pressed={Boolean(active?.checklist)}
            disabled={disabled}
            onClick={() => editor.chain().focus().toggleNoteList("checklist").run()}
          >
            <Icon name="checklist" />
          </button>
          <button
            type="button"
            className={`nw-tool ${active?.table ? "on" : ""}`.trim()}
            aria-label="Table"
            disabled={disabled}
            onClick={() => editor.chain().focus().insertTable({ rows: 2, cols: 2, withHeaderRow: false }).run()}
          >
            <Icon name="table" />
          </button>
          <button
            type="button"
            className={`nw-tool ${attachAt ? "on" : ""}`.trim()}
            aria-label="Attach"
            aria-haspopup="menu"
            disabled={disabled}
            onClick={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();
              setAttachAt(attachAt ? null : { x: rect.left, y: rect.bottom + 4 });
            }}
          >
            <Icon name="attach" />
          </button>
          <button
            type="button"
            className={`nw-tool ${active?.link || panel === "link" ? "on" : ""}`.trim()}
            aria-label="Link"
            disabled={disabled}
            onClick={() => setPanel(panel === "link" ? null : "link")}
          >
            <Icon name="link" />
          </button>
        </div>
        {/* On a touch screen there is no Cmd+Z: undo and redo are buttons, and on a
            phone, while the keyboard is up, they and Done take the place of the
            note's navigation (CSS: .nw-touch-edit, .nw-nav-tool). */}
        <div className="nw-tool-group nw-touch-edit">
          <button type="button" className="nw-tool" aria-label="Undo" disabled={disabled || !active?.canUndo} onClick={() => editor.chain().focus().undo().run()}>
            <Icon name="undo" />
          </button>
          <button type="button" className="nw-tool" aria-label="Redo" disabled={disabled || !active?.canRedo} onClick={() => editor.chain().focus().redo().run()}>
            <Icon name="redo" />
          </button>
          <button type="button" className="nw-tool nw-done" aria-label="Done editing" onClick={() => editor.commands.blur()}>
            Done
          </button>
        </div>
        <div className="nw-tool-group nw-nav-tool">
          {toolbarEnd}
          <button
            type="button"
            className={`nw-tool ${menuAt ? "on" : ""}`.trim()}
            aria-label="More"
            aria-haspopup="menu"
            onClick={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();
              setMenuAt(menuAt ? null : { x: rect.right - 220, y: rect.bottom + 4 });
            }}
          >
            <Icon name="more" />
          </button>
        </div>
        {menuAt && <Menu items={editorMenuItems()} at={menuAt} label="Note actions" onClose={() => setMenuAt(null)} />}
        {attachAt && (
          <Menu
            at={attachAt}
            label="Attach"
            onClose={() => setAttachAt(null)}
            items={[
              { label: "Choose Photo", onSelect: () => photoInput.current?.click() },
              { label: "Attach File", onSelect: () => fileInput.current?.click() },
              {
                label: "Sketch",
                onSelect: () => {
                  // An empty sketch where the caret is, opened at once to draw in.
                  const at = editor.state.selection.from;
                  editor.chain().focus().insertContent({ type: "sketch", attrs: { aspect: 0.75, strokes: [] } }).run();
                  let found = null;
                  editor.state.doc.nodesBetween(Math.max(0, at - 2), Math.min(editor.state.doc.content.size, at + 4), (node, pos) => {
                    if (found === null && node.type.name === "sketch" && !node.attrs.strokes?.length) found = pos;
                  });
                  if (found !== null) openSketch(found, true);
                },
              },
            ]}
          />
        )}
        {[photoInput, fileInput].map((ref, index) => (
          <input
            key={index}
            ref={ref}
            type="file"
            multiple
            accept={index === 0 ? "image/*" : undefined}
            hidden
            onChange={(event) => {
              const files = [...(event.target.files ?? [])];
              event.target.value = "";
              if (files.length) insertAttachments(editor, files, { upload, onError: attachmentError, currentNoteToken });
            }}
          />
        ))}
        <Popover open={panel === "format" && active} onClose={() => setPanel(null)} className="nw-popover nw-format" label="Format">
          {active && <FormatPanel editor={editor} active={active} onClose={() => setPanel(null)} />}
        </Popover>
        <Popover open={panel === "link"} onClose={() => setPanel(null)} className="nw-popover nw-link-pop" label="Link">
          <LinkEditor editor={editor} onClose={() => setPanel(null)} />
        </Popover>
      </div>
      {sketch && (
        <Suspense fallback={null}>
          <SketchEditor
            initial={sketch.initial}
            isNew={sketch.isNew}
            onCancel={() => {
              // A new sketch nobody drew in is not kept.
              const node = editor.state.doc.nodeAt(sketch.pos);
              if (sketch.isNew && node?.type.name === "sketch" && !node.attrs.strokes?.length) {
                editor.chain().deleteRange({ from: sketch.pos, to: sketch.pos + node.nodeSize }).run();
              }
              setSketch(null);
            }}
            onDone={({ aspect, strokes }) => {
              const node = editor.state.doc.nodeAt(sketch.pos);
              if (node?.type.name === "sketch") {
                if (!strokes.length && sketch.isNew) {
                  editor.chain().deleteRange({ from: sketch.pos, to: sketch.pos + node.nodeSize }).run();
                } else {
                  editor.view.dispatch(editor.state.tr.setNodeMarkup(sketch.pos, undefined, { ...node.attrs, aspect, strokes }));
                  // Back to the drawing's button, not the text: no keyboard pops up on a phone.
                  editor.view.nodeDOM(sketch.pos)?.querySelector?.(".nw-sketch-edit")?.focus({ preventScroll: true });
                }
              }
              setSketch(null);
            }}
          />
        </Suspense>
      )}
      <Banner session={session} status={status} controller={controller} onShowOriginal={() => setShowOriginal(true)} />
      {showOriginal && (
        <section className="nw-original" aria-label="Original Markdown">
          <header>
            <span>Original Markdown</span>
            <button type="button" onClick={() => setShowOriginal(false)} aria-label="Close original">
              <Icon name="close" />
            </button>
          </header>
          <pre>{note.body}</pre>
        </section>
      )}
      {active?.table && !disabled && (
        <div onMouseDown={keepEditorFocus}>
          <TableBar editor={editor} />
        </div>
      )}
      <div className="nw-page">
        <p className="nw-date">{longDate(note.updated_at || note.created_at)}</p>
        <EditorContent editor={editor} className="nw-content" />
      </div>
    </div>
  );
}

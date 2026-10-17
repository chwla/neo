/**
 * What each Notes formatting command does to the open editor, for the rebindable
 * `notes.*` commands in keys/commands.js. Kept apart from NoteEditor so the
 * browser suite can drive the same handlers through the real keyboard engine.
 *
 * A handler returns false when there is nothing to act on (no editor, or a note
 * that can't be edited), which leaves the keystroke to the browser.
 */
export function noteCommandHandlers(getEditor) {
  const run = (apply) => {
    const editor = getEditor();
    if (!editor || editor.isDestroyed || !editor.isEditable) return false;
    apply(editor.chain().focus()).run();
    return true;
  };
  return {
    "notes.styleTitle": () => run((c) => c.setNoteStyle("title")),
    "notes.styleHeading": () => run((c) => c.setNoteStyle("heading")),
    "notes.styleSubheading": () => run((c) => c.setNoteStyle("subheading")),
    "notes.styleBody": () => run((c) => c.setNoteStyle("body")),
    "notes.styleMono": () => run((c) => c.setNoteStyle("mono")),
    "notes.bulletedList": () => run((c) => c.toggleNoteList("bulleted")),
    "notes.dashedList": () => run((c) => c.toggleNoteList("dashed")),
    "notes.numberedList": () => run((c) => c.toggleNoteList("numbered")),
    "notes.checklist": () => run((c) => c.toggleNoteList("checklist")),
    "notes.markChecked": () => run((c) => c.toggleChecklistItem()),
    "notes.insertTable": () => run((c) => c.insertTable({ rows: 2, cols: 2, withHeaderRow: false })),
  };
}

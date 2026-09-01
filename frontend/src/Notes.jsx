import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api.js";
import FileAttachments from "./FileAttachments.jsx";
import Icon from "./WorkspaceIcon.jsx";
import {
  countWords,
  formatAbsoluteTime,
  formatRelativeTime,
  mergeTags,
  noteExcerpt,
  parseTagInput,
  renderMarkdown,
} from "./notePresentation.js";

const AUTOSAVE_DELAY = 1400;

function draftFromNote(note) {
  return {
    title: note?.title || "",
    body: note?.body || "",
    tags: [...(note?.tags || [])],
    summary: note?.summary || "",
  };
}

function noteChanged(draft, note) {
  const tagsText = draft.tags.join(",");
  if (!note) {
    return Boolean(draft.title.trim() || draft.body.trim() || tagsText || draft.summary.trim());
  }
  return (
    draft.title !== (note.title || "") ||
    draft.body !== (note.body || "") ||
    tagsText !== (note.tags || []).join(",") ||
    draft.summary !== (note.summary || "")
  );
}

/** Reading time at the pace people actually read prose on a screen. */
function readingTime(words) {
  if (!words) return "";
  const minutes = Math.max(1, Math.round(words / 220));
  return `${minutes} min read`;
}

/**
 * The notes workspace.
 *
 * Three zones, and the middle one is the point: the list narrows, the editor is
 * a page rather than a form, and everything that is *about* the note rather than
 * *in* it -- links, files, provenance -- lives behind one panel so it cannot
 * crowd the writing. Saving is automatic and the indicator says which of the
 * three states it is in, because a Save button that is usually disabled teaches
 * nobody whether their words are safe.
 */
export default function Notes({ onBack, onOpenTask, onOpenFile, initialNoteId = null }) {
  const [notes, setNotes] = useState([]);
  const [tagCounts, setTagCounts] = useState([]);
  const [projects, setProjects] = useState([]);
  const [linkedProjects, setLinkedProjects] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [linkedTasks, setLinkedTasks] = useState([]);
  const [total, setTotal] = useState(0);

  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [activeTag, setActiveTag] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);

  const [selectedNote, setSelectedNote] = useState(null);
  const [draft, setDraft] = useState(draftFromNote(null));
  const [tagInput, setTagInput] = useState("");
  const [isNew, setIsNew] = useState(false);
  const [saveState, setSaveState] = useState("idle");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const [attachProjectId, setAttachProjectId] = useState("");
  const [attachTaskId, setAttachTaskId] = useState("");
  const [showDetails, setShowDetails] = useState(false);
  const [showMore, setShowMore] = useState(false);
  const [view, setView] = useState("write");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const titleRef = useRef(null);
  const bodyRef = useRef(null);
  const searchRef = useRef(null);
  const saveRef = useRef(null);

  const dirty = useMemo(
    () => noteChanged(draft, isNew ? null : selectedNote) || Boolean(tagInput.trim()),
    [draft, isNew, selectedNote, tagInput],
  );
  const editing = Boolean(selectedNote) || isNew;
  const savable = dirty && Boolean(draft.body.trim() || draft.title.trim());
  const bodyWords = useMemo(() => countWords(draft.body), [draft.body]);
  const previewHtml = useMemo(
    () => (view === "write" ? "" : renderMarkdown(draft.body)),
    [draft.body, view],
  );

  useEffect(() => {
    const handle = setTimeout(() => setQuery(typed.trim()), 200);
    return () => clearTimeout(handle);
  }, [typed]);

  const loadNotes = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.notesList({
        q: query,
        tag: activeTag || undefined,
        includeArchived,
        limit: 100,
      });
      setNotes(data.notes || []);
      setTotal(data.total || 0);
    } finally {
      setLoading(false);
    }
  }, [activeTag, includeArchived, query]);

  const loadTags = useCallback(async () => {
    const data = await api.notesTags();
    setTagCounts(data.tags || []);
  }, []);

  useEffect(() => {
    loadNotes().catch((err) => setError(err.message || "Failed to load notes."));
  }, [loadNotes]);

  useEffect(() => {
    loadTags().catch(() => {});
    api.projectsList({ limit: 100 }).then((data) => setProjects(data.projects || [])).catch(() => {});
    api.tasksList({ includeDone: false, limit: 100 }).then((data) => setTasks(data.tasks || [])).catch(() => {});
  }, [loadTags]);

  const unsavedRef = useRef(false);
  unsavedRef.current = saveState === "error" && dirty;

  const openExistingNote = useCallback(async (noteId) => {
    if (unsavedRef.current && !window.confirm("This note could not be saved. Discard the changes?")) {
      return;
    }
    setError("");
    try {
      const [data, projectData, taskData] = await Promise.all([
        api.note(noteId),
        api.noteProjects(noteId),
        api.noteTasks(noteId),
      ]);
      setSelectedNote(data.note);
      setLinkedProjects(projectData.projects || []);
      setLinkedTasks(taskData.tasks || []);
      setDraft(draftFromNote(data.note));
      setTagInput("");
      setIsNew(false);
      setSaveState("idle");
      setConfirmDelete(false);
      setShowMore(false);
      setAttachProjectId("");
      setAttachTaskId("");
    } catch (err) {
      setError(err.message || "Failed to open note.");
    }
  }, []);

  useEffect(() => {
    if (initialNoteId) openExistingNote(initialNoteId);
  }, [initialNoteId, openExistingNote]);

  const saveNote = useCallback(async () => {
    const tagList = mergeTags(draft.tags, parseTagInput(tagInput));
    const payload = {
      title: draft.title,
      body: draft.body,
      tags: tagList,
      summary: draft.summary || null,
      source_type: selectedNote?.source_type || "manual",
      source_id: selectedNote?.source_id || null,
      source_url: selectedNote?.source_url || null,
      source_title: selectedNote?.source_title || null,
      source_metadata: selectedNote?.source_metadata || {},
    };
    setError("");
    setSaveState("saving");
    try {
      const creating = isNew || !selectedNote;
      const data = creating
        ? await api.createNote(payload)
        : await api.updateNote(selectedNote.id, payload);
      setSelectedNote(data.note);
      setDraft(draftFromNote(data.note));
      setTagInput("");
      setIsNew(false);
      setSaveState("saved");
      const [projectData, taskData] = await Promise.all([
        api.noteProjects(data.note.id),
        api.noteTasks(data.note.id),
      ]);
      setLinkedProjects(projectData.projects || []);
      setLinkedTasks(taskData.tasks || []);
      await Promise.all([loadNotes(), loadTags().catch(() => {})]);
    } catch (err) {
      setSaveState("error");
      setError(err.message || "Failed to save note.");
    }
  }, [draft, isNew, loadNotes, loadTags, selectedNote, tagInput]);

  saveRef.current = saveNote;

  /* Autosave, so the Save button is a reassurance rather than a requirement. */
  useEffect(() => {
    if (!editing || !savable) return undefined;
    const handle = setTimeout(() => saveRef.current?.(), AUTOSAVE_DELAY);
    return () => clearTimeout(handle);
  }, [draft, editing, savable, tagInput]);

  const startNewNote = useCallback(() => {
    if (unsavedRef.current && !window.confirm("This note could not be saved. Discard the changes?")) {
      return;
    }
    setSelectedNote(null);
    setLinkedProjects([]);
    setLinkedTasks([]);
    setDraft(draftFromNote(null));
    setTagInput("");
    setIsNew(true);
    setView("write");
    setSaveState("idle");
    setError("");
    setConfirmDelete(false);
    window.requestAnimationFrame(() => titleRef.current?.focus());
  }, []);

  useEffect(() => {
    function onKeyDown(event) {
      const meta = event.metaKey || event.ctrlKey;
      const typing = /^(INPUT|TEXTAREA)$/.test(event.target?.tagName || "");
      if (meta && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (savable) saveRef.current?.();
        return;
      }
      if (meta && event.key.toLowerCase() === "n") {
        event.preventDefault();
        startNewNote();
        return;
      }
      if (event.key === "/" && !typing) {
        event.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (event.key === "Escape" && typing) event.target.blur();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [savable, startNewNote]);

  async function toggleNoteFlag(action) {
    if (!selectedNote || isNew) return;
    setError("");
    setShowMore(false);
    try {
      const data =
        action === "pin"
          ? await api.pinNote(selectedNote.id, !selectedNote.pinned)
          : await api.archiveNote(selectedNote.id, !selectedNote.archived);
      setSelectedNote(data.note);
      await loadNotes();
    } catch (err) {
      setError(err.message || "Failed to update the note.");
    }
  }

  async function deleteSelected() {
    if (!selectedNote || isNew) return;
    setError("");
    try {
      await api.deleteNote(selectedNote.id);
      setSelectedNote(null);
      setLinkedProjects([]);
      setLinkedTasks([]);
      setDraft(draftFromNote(null));
      setTagInput("");
      setIsNew(false);
      setSaveState("idle");
      setConfirmDelete(false);
      setShowMore(false);
      await Promise.all([loadNotes(), loadTags().catch(() => {})]);
    } catch (err) {
      setError(err.message || "Failed to delete note.");
    }
  }

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
    setSaveState("editing");
  }

  function commitTagInput() {
    const incoming = parseTagInput(tagInput);
    if (!incoming.length) {
      setTagInput("");
      return;
    }
    setDraft((current) => ({ ...current, tags: mergeTags(current.tags, incoming) }));
    setTagInput("");
    setSaveState("editing");
  }

  function handleTagKeyDown(event) {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      commitTagInput();
      return;
    }
    if (event.key === "Backspace" && !tagInput && draft.tags.length) {
      event.preventDefault();
      setDraft((current) => ({ ...current, tags: current.tags.slice(0, -1) }));
      setSaveState("editing");
    }
  }

  /* Tab in a Markdown body should indent, not leave the field mid-sentence. */
  function handleBodyKeyDown(event) {
    if (event.key !== "Tab" || event.shiftKey) return;
    event.preventDefault();
    const field = event.target;
    const { selectionStart: start, selectionEnd: end, value } = field;
    const next = `${value.slice(0, start)}  ${value.slice(end)}`;
    updateDraft("body", next);
    window.requestAnimationFrame(() => field.setSelectionRange(start + 2, start + 2));
  }

  async function attachToProject() {
    if (!selectedNote || isNew || !attachProjectId) return;
    try {
      await api.attachNoteToProject(attachProjectId, selectedNote.id);
      setLinkedProjects((await api.noteProjects(selectedNote.id)).projects || []);
      setAttachProjectId("");
    } catch (err) {
      setError(err.message || "Failed to attach note to project.");
    }
  }

  async function attachToTask() {
    if (!selectedNote || isNew || !attachTaskId) return;
    try {
      await api.attachNoteToTask(attachTaskId, selectedNote.id);
      setLinkedTasks((await api.noteTasks(selectedNote.id)).tasks || []);
      setAttachTaskId("");
    } catch (err) {
      setError(err.message || "Failed to attach note to task.");
    }
  }

  const attachableProjects = projects.filter(
    (project) => !linkedProjects.some((linked) => linked.id === project.id),
  );
  const attachableTasks = tasks.filter((task) => !linkedTasks.some((linked) => linked.id === task.id));
  const linkCount = linkedProjects.length + linkedTasks.length;
  const filtering = Boolean(query || activeTag || includeArchived);

  const pinned = notes.filter((note) => note.pinned);
  const rest = notes.filter((note) => !note.pinned);

  const stateLabel =
    saveState === "saving" ? "Saving…"
      : saveState === "error" ? "Not saved"
        : dirty ? "Unsaved"
          : saveState === "saved" ? "Saved"
            : isNew ? "New note" : "Saved";

  function renderRow(note) {
    const active = selectedNote?.id === note.id;
    return (
      <button
        key={note.id}
        type="button"
        className={`nw-row ${active ? "active" : ""} ${note.archived ? "archived" : ""}`.trim()}
        onClick={() => openExistingNote(note.id)}
      >
        <span className="nw-row-head">
          {note.pinned && <Icon name="pin" className="nw-row-pin" />}
          <span className="nw-row-title">{note.title || "Untitled"}</span>
          <time className="nw-row-time" title={formatAbsoluteTime(note.updated_at)}>
            {formatRelativeTime(note.updated_at)}
          </time>
        </span>
        <span className="nw-row-excerpt">{noteExcerpt(note) || "Empty note"}</span>
        {(note.tags || []).length > 0 && (
          <span className="nw-row-tags">
            {(note.tags || []).slice(0, 3).map((tag) => (
              <span className="nw-chip small" key={tag}>{tag}</span>
            ))}
            {(note.tags || []).length > 3 && (
              <span className="nw-row-more">+{note.tags.length - 3}</span>
            )}
          </span>
        )}
      </button>
    );
  }

  return (
    <div className="nw">
      <aside className="nw-rail">
        <header className="nw-rail-head">
          <div className="nw-rail-top">
            <button className="nw-back" type="button" onClick={onBack}>
              <Icon name="back" />
              Chat
            </button>
            <span className="nw-rail-count">
              {loading ? "…" : `${total} note${total === 1 ? "" : "s"}`}
            </span>
          </div>
          <h1 className="nw-rail-title">Notes</h1>
          <button className="nw-new" type="button" onClick={startNewNote}>
            <Icon name="plus" />
            New note
            <kbd>⌘N</kbd>
          </button>
        </header>

        <div className="nw-filters">
          <div className="nw-search">
            <Icon name="search" />
            <input
              ref={searchRef}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              placeholder="Search notes"
              aria-label="Search notes"
            />
            {typed ? (
              <button className="nw-search-clear" type="button" onClick={() => setTyped("")} aria-label="Clear search">
                <Icon name="close" />
              </button>
            ) : (
              <kbd className="nw-kbd">/</kbd>
            )}
          </div>
          {tagCounts.length > 0 && (
            <div className="nw-tagbar">
              <button
                type="button"
                className={`nw-tagchip ${activeTag ? "" : "on"}`.trim()}
                onClick={() => setActiveTag("")}
              >
                All
              </button>
              {tagCounts.slice(0, 14).map((entry) => (
                <button
                  key={entry.tag}
                  type="button"
                  className={`nw-tagchip ${activeTag === entry.tag ? "on" : ""}`.trim()}
                  onClick={() => setActiveTag(activeTag === entry.tag ? "" : entry.tag)}
                >
                  {entry.tag}
                  <em>{entry.count}</em>
                </button>
              ))}
            </div>
          )}
          <button
            className={`nw-toggle ${includeArchived ? "on" : ""}`.trim()}
            type="button"
            aria-pressed={includeArchived}
            onClick={() => setIncludeArchived((value) => !value)}
          >
            <Icon name="archive" />
            Include archived
          </button>
        </div>

        <div className="nw-list">
          {notes.length === 0 ? (
            <div className="nw-list-empty">
              {filtering ? (
                <>
                  <p>Nothing matches.</p>
                  <button className="nw-link" type="button" onClick={() => { setTyped(""); setActiveTag(""); setIncludeArchived(false); }}>
                    Clear filters
                  </button>
                </>
              ) : (
                <p>No notes yet.</p>
              )}
            </div>
          ) : (
            <>
              {pinned.length > 0 && (
                <>
                  <p className="nw-group">Pinned</p>
                  {pinned.map(renderRow)}
                  {rest.length > 0 && <p className="nw-group">Everything else</p>}
                </>
              )}
              {rest.map(renderRow)}
            </>
          )}
        </div>
      </aside>

      <section className="nw-main">
        {!editing ? (
          <div className="nw-blank">
            <div className="nw-blank-mark"><Icon name="note" /></div>
            <h2>Nothing open</h2>
            <p>Pick a note from the list, or start a new one. Notes save themselves as you type.</p>
            <button className="nw-new" type="button" onClick={startNewNote}>
              <Icon name="plus" />
              New note
            </button>
            {error && <div className="nw-error"><Icon name="warning" />{error}</div>}
          </div>
        ) : (
          <>
            <div className="nw-toolbar">
              <div className="nw-toolbar-state">
                <span className={`nw-dot ${saveState === "saving" ? "busy" : dirty ? "dirty" : "clean"}`} />
                <span className="nw-state-text">{stateLabel}</span>
                {!isNew && selectedNote?.updated_at && (
                  <span className="nw-meta" title={formatAbsoluteTime(selectedNote.updated_at)}>
                    {formatRelativeTime(selectedNote.updated_at)}
                  </span>
                )}
                <span className="nw-meta">{bodyWords} words</span>
                {bodyWords > 0 && <span className="nw-meta">{readingTime(bodyWords)}</span>}
                {selectedNote?.archived && <span className="nw-badge">Archived</span>}
                {selectedNote?.pinned && <span className="nw-badge accent">Pinned</span>}
              </div>

              <div className="nw-toolbar-actions">
                <div className="nw-segment" role="group" aria-label="Editor view">
                  <button type="button" className={view === "write" ? "on" : ""} onClick={() => setView("write")} title="Write">
                    <Icon name="write" />
                    Write
                  </button>
                  <button type="button" className={view === "split" ? "on" : ""} onClick={() => setView("split")} title="Side by side">
                    <Icon name="split" />
                    Split
                  </button>
                  <button type="button" className={view === "preview" ? "on" : ""} onClick={() => setView("preview")} title="Preview">
                    <Icon name="preview" />
                    Preview
                  </button>
                </div>

                {!isNew && (
                  <button
                    className={`nw-action ${showDetails ? "on" : ""}`.trim()}
                    type="button"
                    aria-pressed={showDetails}
                    onClick={() => setShowDetails((value) => !value)}
                  >
                    <Icon name="link" />
                    Links{linkCount ? ` ${linkCount}` : ""}
                  </button>
                )}

                <button className="nw-save" type="button" onClick={saveNote} disabled={!savable} title="Save (⌘S)">
                  {saveState === "saving" ? "Saving" : "Save"}
                </button>

                {!isNew && (
                  <div className="nw-menu-wrap">
                    <button
                      className={`nw-action icon ${showMore ? "on" : ""}`.trim()}
                      type="button"
                      aria-haspopup="menu"
                      aria-expanded={showMore}
                      aria-label="More actions"
                      onClick={() => setShowMore((value) => !value)}
                    >
                      <Icon name="more" />
                    </button>
                    {showMore && (
                      <>
                        <button className="nw-menu-scrim" type="button" aria-label="Close menu" onClick={() => setShowMore(false)} />
                        <div className="nw-menu" role="menu">
                          <button type="button" role="menuitem" onClick={() => toggleNoteFlag("pin")}>
                            <Icon name="pin" />
                            {selectedNote?.pinned ? "Unpin" : "Pin to top"}
                          </button>
                          <button type="button" role="menuitem" onClick={() => toggleNoteFlag("archive")}>
                            <Icon name="archive" />
                            {selectedNote?.archived ? "Unarchive" : "Archive"}
                          </button>
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => { navigator.clipboard?.writeText(draft.body).catch(() => {}); setShowMore(false); }}
                          >
                            <Icon name="copy" />
                            Copy Markdown
                          </button>
                          <button type="button" role="menuitem" className="danger" onClick={() => { setShowMore(false); setConfirmDelete(true); }}>
                            <Icon name="trash" />
                            Delete
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>

            {confirmDelete && (
              <div className="nw-confirm" role="alertdialog">
                <Icon name="warning" />
                <span>Delete “{selectedNote?.title || "Untitled"}”? This cannot be undone.</span>
                <button type="button" className="nw-action" onClick={() => setConfirmDelete(false)}>Cancel</button>
                <button type="button" className="nw-action danger on" onClick={deleteSelected}>Delete</button>
              </div>
            )}

            <div className={`nw-stage ${showDetails && !isNew ? "with-details" : ""}`.trim()}>
              <div className={`nw-doc view-${view}`}>
                {selectedNote?.source_type && selectedNote.source_type !== "manual" && (
                  <div className="nw-source">
                    <span className="nw-badge accent">{selectedNote.source_type}</span>
                    {selectedNote.source_title && <span>{selectedNote.source_title}</span>}
                    {selectedNote.source_url && (
                      <a className="nw-link" href={selectedNote.source_url} target="_blank" rel="noreferrer">
                        Open source
                        <Icon name="external" />
                      </a>
                    )}
                  </div>
                )}

                <div className="nw-doc-head">
                  <input
                    ref={titleRef}
                    className="nw-title-input"
                    value={draft.title}
                    onChange={(event) => updateDraft("title", event.target.value)}
                    placeholder="Untitled note"
                    maxLength={200}
                    aria-label="Note title"
                  />
                  <input
                    className="nw-summary-input"
                    value={draft.summary}
                    onChange={(event) => updateDraft("summary", event.target.value)}
                    placeholder="Add a one-line summary"
                    aria-label="Summary"
                  />
                  <div className="nw-tags">
                    <Icon name="tag" className="nw-tags-icon" />
                    {draft.tags.map((tag) => (
                      <span className="nw-chip" key={tag}>
                        {tag}
                        <button
                          type="button"
                          onClick={() => {
                            setDraft((current) => ({ ...current, tags: current.tags.filter((item) => item !== tag) }));
                            setSaveState("editing");
                          }}
                          aria-label={`Remove tag ${tag}`}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    <input
                      className="nw-tag-input"
                      value={tagInput}
                      onChange={(event) => setTagInput(event.target.value)}
                      onKeyDown={handleTagKeyDown}
                      onBlur={commitTagInput}
                      placeholder={draft.tags.length ? "Add tag" : "Add tags"}
                      aria-label="Add tags"
                    />
                  </div>
                </div>

                <div className="nw-panes">
                  {view !== "preview" && (
                    <textarea
                      ref={bodyRef}
                      className="nw-body-input"
                      value={draft.body}
                      onChange={(event) => updateDraft("body", event.target.value)}
                      onKeyDown={handleBodyKeyDown}
                      placeholder="Write in Markdown…"
                      aria-label="Note body"
                      spellCheck="true"
                    />
                  )}
                  {view !== "write" && (
                    <div className="nw-preview">
                      {draft.body.trim() ? (
                        <div
                          className="nw-preview-body"
                          // Source is this user's own note, escaped by renderMarkdown.
                          dangerouslySetInnerHTML={{ __html: previewHtml }}
                        />
                      ) : (
                        <p className="nw-preview-empty">Nothing to preview yet.</p>
                      )}
                    </div>
                  )}
                </div>

                {error && <div className="nw-error"><Icon name="warning" />{error}</div>}
              </div>

              {showDetails && !isNew && (
                <aside className="nw-detail">
                  <div className="nw-detail-head">
                    <h2>Links</h2>
                    <button type="button" onClick={() => setShowDetails(false)} aria-label="Close links panel">
                      <Icon name="close" />
                    </button>
                  </div>

                  <section>
                    <h3>Projects</h3>
                    {linkedProjects.length === 0 ? (
                      <p className="nw-detail-empty">Not linked to a project.</p>
                    ) : (
                      <div className="nw-detail-list">
                        {linkedProjects.map((project) => (
                          <span key={project.id}>{project.title}</span>
                        ))}
                      </div>
                    )}
                    <div className="nw-detail-attach">
                      <select value={attachProjectId} onChange={(event) => setAttachProjectId(event.target.value)} aria-label="Attach to project">
                        <option value="">Attach to project…</option>
                        {attachableProjects.map((project) => (
                          <option key={project.id} value={project.id}>{project.title}</option>
                        ))}
                      </select>
                      <button type="button" onClick={attachToProject} disabled={!attachProjectId}>Add</button>
                    </div>
                  </section>

                  <section>
                    <h3>Tasks</h3>
                    {linkedTasks.length === 0 ? (
                      <p className="nw-detail-empty">Not linked to a task.</p>
                    ) : (
                      <div className="nw-detail-list">
                        {linkedTasks.map((task) => (
                          <button type="button" key={task.id} onClick={() => onOpenTask?.(task.id)}>
                            {task.title}
                            <em>{task.status}</em>
                          </button>
                        ))}
                      </div>
                    )}
                    <div className="nw-detail-attach">
                      <select value={attachTaskId} onChange={(event) => setAttachTaskId(event.target.value)} aria-label="Attach to task">
                        <option value="">Attach to task…</option>
                        {attachableTasks.map((task) => (
                          <option key={task.id} value={task.id}>{task.title}</option>
                        ))}
                      </select>
                      <button type="button" onClick={attachToTask} disabled={!attachTaskId}>Add</button>
                    </div>
                  </section>

                  <FileAttachments linkType="note" targetId={selectedNote.id} onOpenFile={onOpenFile} />
                </aside>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

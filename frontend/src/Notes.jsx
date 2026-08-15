import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api.js";
import FileAttachments from "./FileAttachments.jsx";
import {
  countWords,
  formatAbsoluteTime,
  formatRelativeTime,
  mergeTags,
  noteExcerpt,
  parseTagInput,
  renderMarkdown,
} from "./notePresentation.js";

const ICON_PATHS = {
  pin: ["M9 3h6l-1 6 4 3v2h-5l-1 7-1-7H6v-2l4-3z"],
  archive: ["M3 5h18v4H3z", "M5 9v10h14V9", "M9 13h6"],
  trash: ["M4 6h16", "M9 6V4h6v2", "m6 6 1 14h10l1-14"],
  details: ["M3 4h18v16H3z", "M15 4v16"],
  preview: ["M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"],
  write: ["M4 20h4l10-10-4-4L4 16z", "m14 6 4 4"],
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "m20 20-4-4"],
  plus: ["M12 5v14", "M5 12h14"],
  back: ["m15 5-7 7 7 7"],
};

function Icon({ name, className = "" }) {
  return (
    <svg
      className={`nw-icon ${className}`.trim()}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {(ICON_PATHS[name] || []).map((path) => (
        <path d={path} key={path} />
      ))}
    </svg>
  );
}

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

export default function Notes({ onBack, onOpenTask, onOpenFile, initialNoteId = null }) {
  const [notes, setNotes] = useState([]);
  const [projects, setProjects] = useState([]);
  const [linkedProjects, setLinkedProjects] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [linkedTasks, setLinkedTasks] = useState([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedNote, setSelectedNote] = useState(null);
  const [draft, setDraft] = useState(draftFromNote(null));
  const [tagInput, setTagInput] = useState("");
  const [isNew, setIsNew] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [attachProjectId, setAttachProjectId] = useState("");
  const [attachTaskId, setAttachTaskId] = useState("");
  const [showDetails, setShowDetails] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const titleRef = useRef(null);

  const dirty = useMemo(
    () => noteChanged(draft, isNew ? null : selectedNote) || Boolean(tagInput.trim()),
    [draft, isNew, selectedNote, tagInput],
  );
  const editing = Boolean(selectedNote) || isNew;
  const bodyWords = useMemo(() => countWords(draft.body), [draft.body]);
  const previewHtml = useMemo(
    () => (previewing ? renderMarkdown(draft.body) : ""),
    [draft.body, previewing],
  );

  const loadNotes = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.notesList({
        q: query.trim(),
        includeArchived,
        limit: 75,
      });
      setNotes(data.notes || []);
      setTotal(data.total || 0);
    } finally {
      setLoading(false);
    }
  }, [includeArchived, query]);

  const loadProjects = useCallback(async () => {
    const data = await api.projectsList({ limit: 100 });
    setProjects(data.projects || []);
  }, []);

  const loadTasks = useCallback(async () => {
    const data = await api.tasksList({ includeDone: false, limit: 100 });
    setTasks(data.tasks || []);
  }, []);

  useEffect(() => {
    loadNotes().catch((err) => setError(err.message || "Failed to load notes."));
  }, [loadNotes]);

  useEffect(() => {
    loadProjects().catch(() => {});
  }, [loadProjects]);

  useEffect(() => {
    loadTasks().catch(() => {});
  }, [loadTasks]);

  useEffect(() => {
    if (!initialNoteId) {
      return;
    }
    openExistingNote(initialNoteId);
  }, [initialNoteId]);

  useEffect(() => {
    function onKeyDown(event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (editing && dirty && draft.body.trim()) {
          saveNote();
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  function canLeaveCurrent() {
    return !dirty || window.confirm("Discard unsaved changes?");
  }

  async function openExistingNote(noteId) {
    if (!canLeaveCurrent()) {
      return;
    }
    setError("");
    setStatus("");
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
      setPreviewing(false);
      setAttachProjectId("");
      setAttachTaskId("");
    } catch (err) {
      setError(err.message || "Failed to open note.");
    }
  }

  function startNewNote() {
    if (!canLeaveCurrent()) {
      return;
    }
    setSelectedNote(null);
    setLinkedProjects([]);
    setLinkedTasks([]);
    setDraft(draftFromNote(null));
    setTagInput("");
    setIsNew(true);
    setPreviewing(false);
    setStatus("Unsaved changes");
    setError("");
    window.requestAnimationFrame(() => titleRef.current?.focus());
  }

  async function saveNote() {
    setError("");
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
    try {
      setStatus("Saving...");
      const data = isNew
        ? await api.createNote(payload)
        : await api.updateNote(selectedNote.id, payload);
      setSelectedNote(data.note);
      const [projectData, taskData] = await Promise.all([
        api.noteProjects(data.note.id),
        api.noteTasks(data.note.id),
      ]);
      setLinkedProjects(projectData.projects || []);
      setLinkedTasks(taskData.tasks || []);
      setDraft(draftFromNote(data.note));
      setTagInput("");
      setIsNew(false);
      setStatus("Saved");
      await loadNotes();
    } catch (err) {
      setStatus("Unsaved changes");
      setError(err.message || "Failed to save note.");
    }
  }

  async function pinSelected() {
    if (!selectedNote || isNew) return;
    setError("");
    try {
      const data = await api.pinNote(selectedNote.id, !selectedNote.pinned);
      setSelectedNote(data.note);
      await loadNotes();
    } catch (err) {
      setError(err.message || "Failed to update pin.");
    }
  }

  async function archiveSelected() {
    if (!selectedNote || isNew) return;
    setError("");
    try {
      const data = await api.archiveNote(selectedNote.id, !selectedNote.archived);
      setSelectedNote(data.note);
      await loadNotes();
    } catch (err) {
      setError(err.message || "Failed to update archive state.");
    }
  }

  async function deleteSelected() {
    if (!selectedNote || isNew) return;
    if (!window.confirm(`Delete note ${selectedNote.title}?`)) {
      return;
    }
    setError("");
    try {
      await api.deleteNote(selectedNote.id);
      setSelectedNote(null);
      setLinkedProjects([]);
      setLinkedTasks([]);
      setDraft(draftFromNote(null));
      setTagInput("");
      setIsNew(false);
      setStatus("");
      await loadNotes();
    } catch (err) {
      setError(err.message || "Failed to delete note.");
    }
  }

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
    setStatus("Unsaved changes");
  }

  function commitTagInput() {
    const incoming = parseTagInput(tagInput);
    if (!incoming.length) {
      setTagInput("");
      return;
    }
    setDraft((current) => ({ ...current, tags: mergeTags(current.tags, incoming) }));
    setTagInput("");
    setStatus("Unsaved changes");
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
      setStatus("Unsaved changes");
    }
  }

  function removeTag(tag) {
    setDraft((current) => ({ ...current, tags: current.tags.filter((item) => item !== tag) }));
    setStatus("Unsaved changes");
  }

  async function attachToProject() {
    if (!selectedNote || isNew || !attachProjectId) return;
    setError("");
    try {
      await api.attachNoteToProject(attachProjectId, selectedNote.id);
      const projectData = await api.noteProjects(selectedNote.id);
      setLinkedProjects(projectData.projects || []);
      setAttachProjectId("");
    } catch (err) {
      setError(err.message || "Failed to attach note to project.");
    }
  }

  async function attachToTask() {
    if (!selectedNote || isNew || !attachTaskId) return;
    setError("");
    try {
      await api.attachNoteToTask(attachTaskId, selectedNote.id);
      const taskData = await api.noteTasks(selectedNote.id);
      setLinkedTasks(taskData.tasks || []);
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
  const filtered = Boolean(query.trim());

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
          </button>
        </header>

        <div className="nw-filters">
          <div className="nw-search">
            <Icon name="search" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search notes"
              aria-label="Search notes"
            />
            {query && (
              <button
                className="nw-search-clear"
                type="button"
                onClick={() => setQuery("")}
                aria-label="Clear search"
              >
                ×
              </button>
            )}
          </div>
          <div className="nw-filter-row">
            <button
              className={`nw-toggle ${includeArchived ? "on" : ""}`}
              type="button"
              aria-pressed={includeArchived}
              onClick={() => setIncludeArchived((value) => !value)}
            >
              Archived
            </button>
          </div>
        </div>

        <div className="nw-list">
          {notes.length === 0 ? (
            <div className="nw-list-empty">
              {filtered ? (
                <>
                  <p>No notes match.</p>
                  <button
                    className="nw-link"
                    type="button"
                    onClick={() => setQuery("")}
                  >
                    Clear filters
                  </button>
                </>
              ) : (
                <p>No notes yet.</p>
              )}
            </div>
          ) : (
            notes.map((note) => {
              const active = selectedNote?.id === note.id;
              return (
                <button
                  key={note.id}
                  type="button"
                  className={`nw-row ${active ? "active" : ""} ${note.archived ? "archived" : ""}`}
                  onClick={() => openExistingNote(note.id)}
                >
                  <span className="nw-row-head">
                    {note.pinned && <Icon name="pin" className="nw-row-pin" />}
                    <span className="nw-row-title">{note.title || "Untitled"}</span>
                    <time className="nw-row-time" title={formatAbsoluteTime(note.updated_at)}>
                      {formatRelativeTime(note.updated_at)}
                    </time>
                  </span>
                  <span className="nw-row-excerpt">{noteExcerpt(note)}</span>
                  {(note.tags || []).length > 0 && (
                    <span className="nw-row-tags">
                      {(note.tags || []).slice(0, 3).map((tag) => (
                        <span className="nw-chip small" key={tag}>
                          {tag}
                        </span>
                      ))}
                      {(note.tags || []).length > 3 && (
                        <span className="nw-row-more">+{note.tags.length - 3}</span>
                      )}
                    </span>
                  )}
                </button>
              );
            })
          )}
        </div>
      </aside>

      <section className="nw-main">
        {!editing ? (
          <div className="nw-blank">
            <div className="nw-blank-mark">
              <Icon name="write" />
            </div>
            <h2>Nothing open</h2>
            <p>Pick a note from the list, or start a new one.</p>
            <button className="nw-new" type="button" onClick={startNewNote}>
              <Icon name="plus" />
              New note
            </button>
            {error && <div className="nw-error">{error}</div>}
          </div>
        ) : (
          <>
            <div className="nw-toolbar">
              <div className="nw-toolbar-state">
                <span className={`nw-dot ${dirty ? "dirty" : "clean"}`} />
                <span className="nw-state-text">
                  {dirty ? "Unsaved changes" : status || "Saved"}
                </span>
                {!isNew && selectedNote?.updated_at && (
                  <span className="nw-meta" title={formatAbsoluteTime(selectedNote.updated_at)}>
                    Edited {formatRelativeTime(selectedNote.updated_at)}
                  </span>
                )}
                <span className="nw-meta">{bodyWords} words</span>
                {selectedNote?.archived && <span className="nw-badge">Archived</span>}
                {selectedNote?.pinned && <span className="nw-badge accent">Pinned</span>}
              </div>

              <div className="nw-toolbar-actions">
                <div className="nw-segment">
                  <button
                    type="button"
                    className={previewing ? "" : "on"}
                    onClick={() => setPreviewing(false)}
                  >
                    <Icon name="write" />
                    Write
                  </button>
                  <button
                    type="button"
                    className={previewing ? "on" : ""}
                    onClick={() => setPreviewing(true)}
                  >
                    <Icon name="preview" />
                    Preview
                  </button>
                </div>

                {!isNew && (
                  <>
                    <button className="nw-action" type="button" onClick={pinSelected}>
                      <Icon name="pin" />
                      {selectedNote?.pinned ? "Unpin" : "Pin"}
                    </button>
                    <button className="nw-action" type="button" onClick={archiveSelected}>
                      <Icon name="archive" />
                      {selectedNote?.archived ? "Unarchive" : "Archive"}
                    </button>
                    <button
                      className={`nw-action ${showDetails ? "on" : ""}`}
                      type="button"
                      aria-pressed={showDetails}
                      onClick={() => setShowDetails((value) => !value)}
                    >
                      <Icon name="details" />
                      Links{linkCount ? ` (${linkCount})` : ""}
                    </button>
                    <button className="nw-action danger" type="button" onClick={deleteSelected}>
                      <Icon name="trash" />
                      Delete
                    </button>
                  </>
                )}

                <button
                  className="nw-save"
                  type="button"
                  onClick={saveNote}
                  disabled={!dirty || !draft.body.trim()}
                  title="Save (⌘S)"
                >
                  Save
                </button>
              </div>
            </div>

            <div className={`nw-stage ${showDetails && !isNew ? "with-details" : ""}`}>
              <div className="nw-doc">
                {selectedNote?.source_type && selectedNote.source_type !== "manual" && (
                  <div className="nw-source">
                    <span className="nw-badge accent">{selectedNote.source_type}</span>
                    {selectedNote.source_title && <span>{selectedNote.source_title}</span>}
                  </div>
                )}

                <input
                  ref={titleRef}
                  className="nw-title-input"
                  value={draft.title}
                  onChange={(event) => updateDraft("title", event.target.value)}
                  placeholder="Untitled note"
                  maxLength={200}
                  aria-label="Note title"
                />

                <div className="nw-tags">
                  {draft.tags.map((tag) => (
                    <span className="nw-chip" key={tag}>
                      {tag}
                      <button
                        type="button"
                        onClick={() => removeTag(tag)}
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

                <input
                  className="nw-summary-input"
                  value={draft.summary}
                  onChange={(event) => updateDraft("summary", event.target.value)}
                  placeholder="One-line summary (optional)"
                  aria-label="Summary"
                />

                {previewing ? (
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
                ) : (
                  <textarea
                    className="nw-body-input"
                    value={draft.body}
                    onChange={(event) => updateDraft("body", event.target.value)}
                    placeholder="Write in Markdown…"
                    aria-label="Note body"
                  />
                )}

                {error && <div className="nw-error">{error}</div>}
              </div>

              {showDetails && !isNew && (
                <aside className="nw-detail">
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
                      <select
                        value={attachProjectId}
                        onChange={(event) => setAttachProjectId(event.target.value)}
                        aria-label="Attach to project"
                      >
                        <option value="">Attach to project…</option>
                        {attachableProjects.map((project) => (
                          <option key={project.id} value={project.id}>
                            {project.title}
                          </option>
                        ))}
                      </select>
                      <button type="button" onClick={attachToProject} disabled={!attachProjectId}>
                        Add
                      </button>
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
                      <select
                        value={attachTaskId}
                        onChange={(event) => setAttachTaskId(event.target.value)}
                        aria-label="Attach to task"
                      >
                        <option value="">Attach to task…</option>
                        {attachableTasks.map((task) => (
                          <option key={task.id} value={task.id}>
                            {task.title}
                          </option>
                        ))}
                      </select>
                      <button type="button" onClick={attachToTask} disabled={!attachTaskId}>
                        Add
                      </button>
                    </div>
                  </section>

                  <FileAttachments
                    linkType="note"
                    targetId={selectedNote.id}
                    onOpenFile={onOpenFile}
                  />
                </aside>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";
import FileAttachments from "./FileAttachments.jsx";

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function tagsToText(tags) {
  return (tags || []).join(", ");
}

function textToTags(text) {
  return text
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function draftFromNote(note) {
  return {
    title: note?.title || "",
    body: note?.body || "",
    tagsText: tagsToText(note?.tags || []),
    summary: note?.summary || "",
  };
}

function noteChanged(draft, note) {
  if (!note) {
    return Boolean(draft.title.trim() || draft.body.trim() || draft.tagsText.trim() || draft.summary.trim());
  }
  return (
    draft.title !== (note.title || "") ||
    draft.body !== (note.body || "") ||
    draft.tagsText !== tagsToText(note.tags || []) ||
    draft.summary !== (note.summary || "")
  );
}

export default function Notes({ onBack, onOpenTask, onOpenFile, initialNoteId = null }) {
  const [notes, setNotes] = useState([]);
  const [tags, setTags] = useState([]);
  const [projects, setProjects] = useState([]);
  const [linkedProjects, setLinkedProjects] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [linkedTasks, setLinkedTasks] = useState([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedNote, setSelectedNote] = useState(null);
  const [draft, setDraft] = useState(draftFromNote(null));
  const [isNew, setIsNew] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [attachProjectId, setAttachProjectId] = useState("");
  const [attachTaskId, setAttachTaskId] = useState("");

  const dirty = useMemo(() => noteChanged(draft, isNew ? null : selectedNote), [draft, isNew, selectedNote]);

  const loadTags = useCallback(async () => {
    const data = await api.notesTags();
    setTags(data.tags || []);
  }, []);

  const loadNotes = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.notesList({
        q: query.trim(),
        tag: tagFilter,
        includeArchived,
        limit: 75,
      });
      setNotes(data.notes || []);
      setTotal(data.total || 0);
    } finally {
      setLoading(false);
    }
  }, [includeArchived, query, tagFilter]);

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
    loadTags().catch(() => {});
  }, [loadTags]);

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
      const [data, projectData, taskData] = await Promise.all([api.note(noteId), api.noteProjects(noteId), api.noteTasks(noteId)]);
      setSelectedNote(data.note);
      setLinkedProjects(projectData.projects || []);
      setLinkedTasks(taskData.tasks || []);
      setDraft(draftFromNote(data.note));
      setIsNew(false);
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
    setIsNew(true);
    setStatus("Unsaved changes");
    setError("");
  }

  async function saveNote() {
    setError("");
    const payload = {
      title: draft.title,
      body: draft.body,
      tags: textToTags(draft.tagsText),
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
      const [projectData, taskData] = await Promise.all([api.noteProjects(data.note.id), api.noteTasks(data.note.id)]);
      setLinkedProjects(projectData.projects || []);
      setLinkedTasks(taskData.tasks || []);
      setDraft(draftFromNote(data.note));
      setIsNew(false);
      setStatus("Saved");
      await Promise.all([loadNotes(), loadTags()]);
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
      setIsNew(false);
      setStatus("");
      await Promise.all([loadNotes(), loadTags()]);
    } catch (err) {
      setError(err.message || "Failed to delete note.");
    }
  }

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
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

  return (
    <main className="notes-layout">
      <section className="notes-list-pane">
        <div className="notes-header">
          <button className="research-back" onClick={onBack} type="button">Chat</button>
          <h2 className="notes-title">Notes</h2>
        </div>

        <button className="notes-new-btn" type="button" onClick={startNewNote}>
          New Note
        </button>

        <input
          className="notes-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search notes"
        />

        <div className="notes-filters">
          <select value={tagFilter} onChange={(event) => setTagFilter(event.target.value)}>
            <option value="">All tags</option>
            {tags.map((item) => (
              <option key={item.tag} value={item.tag}>
                {item.tag} ({item.count})
              </option>
            ))}
          </select>
          <label className="notes-archived-toggle">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(event) => setIncludeArchived(event.target.checked)}
            />
            Archived
          </label>
        </div>

        <div className="notes-list-meta">{loading ? "Loading..." : `${total} note${total === 1 ? "" : "s"}`}</div>

        <div className="notes-list">
          {notes.length === 0 ? (
            <div className="notes-empty">
              {query.trim() || tagFilter ? "No notes match your search." : "No notes yet. Create a note or save a research report."}
            </div>
          ) : (
            notes.map((note) => (
              <button
                key={note.id}
                className={`notes-item ${selectedNote?.id === note.id ? "active" : ""}`}
                type="button"
                onClick={() => openExistingNote(note.id)}
              >
                <span className="notes-item-title">
                  {note.pinned && <span className="notes-pin" title="Pinned">PIN</span>}
                  {note.title}
                </span>
                <span className="notes-item-preview">{note.preview || note.body}</span>
                <span className="notes-item-tags">
                  {(note.tags || []).slice(0, 4).map((tag) => (
                    <span key={tag}>{tag}</span>
                  ))}
                </span>
                <span className="notes-item-time">{formatTime(note.updated_at)}</span>
              </button>
            ))
          )}
        </div>
      </section>

      <section className="notes-editor-pane">
        {!selectedNote && !isNew ? (
          <div className="notes-editor-empty">Select a note or create a new one.</div>
        ) : (
          <>
            <div className="notes-editor-toolbar">
              <span className={`notes-save-state ${dirty ? "dirty" : ""}`}>{dirty ? "Unsaved changes" : status || "Saved"}</span>
              <button type="button" onClick={saveNote} disabled={!dirty || !draft.body.trim()}>
                Save
              </button>
              {!isNew && (
                <>
                  <button type="button" onClick={pinSelected}>
                    {selectedNote?.pinned ? "Unpin" : "Pin"}
                  </button>
                  <button type="button" onClick={archiveSelected}>
                    {selectedNote?.archived ? "Unarchive" : "Archive"}
                  </button>
                  <button className="notes-danger-btn" type="button" onClick={deleteSelected}>
                    Delete
                  </button>
                </>
              )}
            </div>

            {selectedNote?.source_type && selectedNote.source_type !== "manual" && (
              <div className="notes-source">
                <span>{selectedNote.source_type}</span>
                {selectedNote.source_title && <strong>{selectedNote.source_title}</strong>}
              </div>
            )}

            {!isNew && (
              <div className="notes-projects-box">
                <div className="notes-projects-title">Projects</div>
                <div className="notes-projects-list">
                  {linkedProjects.length === 0 ? (
                    <span>No linked projects.</span>
                  ) : (
                    linkedProjects.map((project) => (
                      <span key={project.id}>{project.title}</span>
                    ))
                  )}
                </div>
                <div className="notes-projects-attach">
                  <select value={attachProjectId} onChange={(event) => setAttachProjectId(event.target.value)}>
                    <option value="">Attach to Project</option>
                    {attachableProjects.map((project) => (
                      <option key={project.id} value={project.id}>{project.title}</option>
                    ))}
                  </select>
                  <button type="button" onClick={attachToProject} disabled={!attachProjectId}>
                    Attach
                  </button>
                </div>
              </div>
            )}

            {!isNew && (
              <div className="notes-projects-box notes-tasks-box">
                <div className="notes-projects-title">Tasks</div>
                <div className="notes-projects-list">
                  {linkedTasks.length === 0 ? <span>No linked tasks.</span> : linkedTasks.map((task) => (
                    <button type="button" key={task.id} onClick={() => onOpenTask?.(task.id)}>{task.title} · {task.status}</button>
                  ))}
                </div>
                <div className="notes-projects-attach">
                  <select value={attachTaskId} onChange={(event) => setAttachTaskId(event.target.value)}>
                    <option value="">Attach to Task</option>
                    {attachableTasks.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}
                  </select>
                  <button type="button" onClick={attachToTask} disabled={!attachTaskId}>Attach</button>
                </div>
              </div>
            )}

            {!isNew && <FileAttachments linkType="note" targetId={selectedNote.id} onOpenFile={onOpenFile} />}

            <input
              className="notes-title-input"
              value={draft.title}
              onChange={(event) => updateDraft("title", event.target.value)}
              placeholder="Title"
              maxLength={200}
            />

            <input
              className="notes-tags-input"
              value={draft.tagsText}
              onChange={(event) => updateDraft("tagsText", event.target.value)}
              placeholder="Tags, separated by commas"
            />

            <textarea
              className="notes-summary-input"
              value={draft.summary}
              onChange={(event) => updateDraft("summary", event.target.value)}
              placeholder="Summary"
              rows={2}
            />

            <textarea
              className="notes-body-input"
              value={draft.body}
              onChange={(event) => updateDraft("body", event.target.value)}
              placeholder="Write in Markdown"
            />

            {error && <div className="notes-error">{error}</div>}
          </>
        )}
        {error && !selectedNote && !isNew && <div className="notes-error">{error}</div>}
      </section>
    </main>
  );
}

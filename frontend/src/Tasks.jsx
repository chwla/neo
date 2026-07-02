import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";

const STATUSES = ["todo", "doing", "blocked", "done"];
const PRIORITIES = ["low", "medium", "high", "critical"];

function toDraft(task) {
  return {
    title: task?.title || "",
    description: task?.description || "",
    status: task?.status || "todo",
    priority: task?.priority || "medium",
    due_at: task?.due_at ? task.due_at.slice(0, 16) : "",
    project_id: task?.project_id || "",
    tags: (task?.tags || []).join(", "),
  };
}

function tagsFrom(value) {
  return value.split(",").map((tag) => tag.trim()).filter(Boolean);
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export default function Tasks({ initialTaskId = null, initialProjectId = null, onBack, onOpenNote, onTaskChange }) {
  const [tasks, setTasks] = useState([]);
  const [projects, setProjects] = useState([]);
  const [notes, setNotes] = useState([]);
  const [taskTags, setTaskTags] = useState([]);
  const [selectedTask, setSelectedTask] = useState(null);
  const [linkedNotes, setLinkedNotes] = useState([]);
  const [draft, setDraft] = useState(toDraft(null));
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState("");
  const [projectFilter, setProjectFilter] = useState(initialProjectId || "");
  const [tag, setTag] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadTasks = useCallback(async () => {
    const data = await api.tasksList({ q: query, status, priority, projectId: projectFilter, tag, includeArchived });
    setTasks(data.tasks || []);
    return data.tasks || [];
  }, [query, status, priority, projectFilter, tag, includeArchived]);

  const loadMeta = useCallback(async () => {
    const [projectsData, notesData, tagsData] = await Promise.all([
      api.projectsList({ limit: 100 }), api.notesList({ limit: 100 }), api.tasksTags(),
    ]);
    setProjects(projectsData.projects || []);
    setNotes(notesData.notes || []);
    setTaskTags(tagsData.tags || []);
  }, []);

  useEffect(() => { loadTasks().catch((err) => setError(err.message)); }, [loadTasks]);
  useEffect(() => { loadMeta().catch((err) => setError(err.message)); }, [loadMeta]);
  useEffect(() => {
    if (initialTaskId) openTask(initialTaskId);
  }, [initialTaskId]);
  useEffect(() => {
    if (initialProjectId) setProjectFilter(initialProjectId);
  }, [initialProjectId]);

  const dirty = useMemo(() => selectedTask && JSON.stringify(draft) !== JSON.stringify(toDraft(selectedTask)), [draft, selectedTask]);
  const attachableNotes = notes.filter((note) => !linkedNotes.some((linked) => linked.id === note.id));

  function canLeave() {
    return !dirty || window.confirm("Discard unsaved task changes?");
  }

  async function openTask(taskId) {
    if (!canLeave()) return;
    setBusy(true);
    setError("");
    try {
      const data = await api.task(taskId);
      setSelectedTask(data.task);
      setDraft(toDraft(data.task));
      setLinkedNotes(data.notes || []);
      onTaskChange?.(data.task.id);
    } catch (err) {
      setError(err.message || "Failed to load task.");
    } finally {
      setBusy(false);
    }
  }

  async function createTask() {
    if (!canLeave()) return;
    setBusy(true);
    setError("");
    try {
      const data = await api.createTask({ title: "New task", project_id: initialProjectId || null });
      await Promise.all([loadTasks(), loadMeta()]);
      setSelectedTask(data.task);
      setDraft(toDraft(data.task));
      setLinkedNotes([]);
      onTaskChange?.(data.task.id);
    } catch (err) {
      setError(err.message || "Failed to create task.");
    } finally {
      setBusy(false);
    }
  }

  async function saveTask(event) {
    event.preventDefault();
    if (!selectedTask) return;
    setBusy(true);
    setError("");
    try {
      const data = await api.updateTask(selectedTask.id, {
        title: draft.title,
        description: draft.description,
        status: draft.status,
        priority: draft.priority,
        due_at: draft.due_at || null,
        project_id: draft.project_id || null,
        tags: tagsFrom(draft.tags),
      });
      setSelectedTask(data.task);
      setDraft(toDraft(data.task));
      await Promise.all([loadTasks(), loadMeta()]);
    } catch (err) {
      setError(err.message || "Failed to save task.");
    } finally {
      setBusy(false);
    }
  }

  async function togglePin() {
    const data = await api.pinTask(selectedTask.id, !selectedTask.pinned);
    setSelectedTask(data.task); setDraft(toDraft(data.task)); await loadTasks();
  }

  async function toggleArchive() {
    const data = await api.archiveTask(selectedTask.id, !selectedTask.archived);
    setSelectedTask(data.task); setDraft(toDraft(data.task)); await loadTasks();
  }

  async function removeTask() {
    if (!window.confirm("Delete this task?")) return;
    await api.deleteTask(selectedTask.id);
    setSelectedTask(null); setLinkedNotes([]); setDraft(toDraft(null)); onTaskChange?.(null); await loadTasks();
  }

  async function attachNote(noteId) {
    if (!noteId) return;
    await api.attachNoteToTask(selectedTask.id, noteId);
    const data = await api.task(selectedTask.id);
    setSelectedTask(data.task); setLinkedNotes(data.notes || []); await loadTasks();
  }

  async function detachNote(noteId) {
    await api.detachNoteFromTask(selectedTask.id, noteId);
    setLinkedNotes((current) => current.filter((note) => note.id !== noteId));
    await loadTasks();
  }

  return (
    <main className="tasks-layout">
      <section className="tasks-list-pane">
        <div className="tasks-pane-header">
          <button className="neo-button secondary" type="button" onClick={onBack}>Back</button>
          <h2>Tasks</h2>
          <button className="neo-button" type="button" onClick={createTask} disabled={busy}>New Task</button>
        </div>
        <input className="tasks-search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search tasks" />
        <div className="tasks-filters">
          <select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">All statuses</option>{STATUSES.map((item) => <option key={item}>{item}</option>)}</select>
          <select value={priority} onChange={(e) => setPriority(e.target.value)}><option value="">All priorities</option>{PRIORITIES.map((item) => <option key={item}>{item}</option>)}</select>
          <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}><option value="">All projects</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select>
          <select value={tag} onChange={(e) => setTag(e.target.value)}><option value="">All tags</option>{taskTags.map((item) => <option key={item.tag} value={item.tag}>{item.tag} ({item.count})</option>)}</select>
          <label className="tasks-check"><input type="checkbox" checked={includeArchived} onChange={(e) => setIncludeArchived(e.target.checked)} /> Archived</label>
        </div>
        <div className="tasks-list">
          {tasks.length === 0 ? <p className="tasks-empty">No tasks yet.<br />Create a task or add one from a project.</p> : tasks.map((task) => (
            <button key={task.id} type="button" className={`task-card ${selectedTask?.id === task.id ? "selected" : ""}`} onClick={() => openTask(task.id)}>
              <div className="task-card-title">{task.pinned ? "★ " : ""}{task.title}</div>
              <div className="task-card-meta"><span className={`task-status ${task.status}`}>{task.status}</span><span className={`task-priority ${task.priority}`}>{task.priority}</span>{task.project_title ? <span>{task.project_title}</span> : null}</div>
              {task.due_at ? <div className="task-card-due">Due {formatTime(task.due_at)}</div> : null}
              {task.tags?.length ? <div className="task-tags">{task.tags.map((item) => <span key={item}>#{item}</span>)}</div> : null}
              <div className="task-card-updated">Updated {formatTime(task.updated_at)}</div>
            </button>
          ))}
        </div>
      </section>

      <section className="tasks-editor-pane">
        {error ? <div className="task-error">{error}</div> : null}
        {!selectedTask ? <div className="tasks-empty editor">Select a task or create a new one.</div> : (
          <form className="task-editor" onSubmit={saveTask}>
            <input className="task-title-input" value={draft.title} maxLength={200} onChange={(e) => setDraft({ ...draft, title: e.target.value })} placeholder="Task title" />
            <textarea value={draft.description} maxLength={50000} onChange={(e) => setDraft({ ...draft, description: e.target.value })} placeholder="Description" />
            <div className="task-field-grid">
              <label>Status<select value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })}>{STATUSES.map((item) => <option key={item}>{item}</option>)}</select></label>
              <label>Priority<select value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })}>{PRIORITIES.map((item) => <option key={item}>{item}</option>)}</select></label>
              <label>Due date<input type="datetime-local" value={draft.due_at} onChange={(e) => setDraft({ ...draft, due_at: e.target.value })} /></label>
              <label>Project<select value={draft.project_id} onChange={(e) => setDraft({ ...draft, project_id: e.target.value })}><option value="">No project linked</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
            </div>
            {!draft.project_id ? <p className="task-help">No project linked. Tasks can exist independently or belong to a project.</p> : null}
            <label className="task-tags-input">Tags<input value={draft.tags} onChange={(e) => setDraft({ ...draft, tags: e.target.value })} placeholder="neo, backend" /></label>
            <div className="task-actions">
              <button className="neo-button" type="submit" disabled={busy || !draft.title.trim()}>Save</button>
              <button className="neo-button secondary" type="button" onClick={togglePin}>{selectedTask.pinned ? "Unpin" : "Pin"}</button>
              <button className="neo-button secondary" type="button" onClick={toggleArchive}>{selectedTask.archived ? "Unarchive" : "Archive"}</button>
              <button className="neo-button danger" type="button" onClick={removeTask}>Delete</button>
            </div>
            <div className="task-completion-meta">{selectedTask.completed_at ? `Completed ${formatTime(selectedTask.completed_at)}` : ""}</div>

            <div className="task-notes-section">
              <div className="task-section-title">Linked Notes</div>
              <select defaultValue="" onChange={(e) => { attachNote(e.target.value); e.target.value = ""; }}>
                <option value="">Attach note…</option>
                {attachableNotes.map((note) => <option key={note.id} value={note.id}>{note.title}</option>)}
              </select>
              {linkedNotes.length === 0 ? <p className="task-help">No notes attached yet.<br />Attach notes for context.</p> : (
                <div className="task-linked-list">{linkedNotes.map((note) => <div key={note.id} className="task-linked-row"><button type="button" onClick={() => onOpenNote?.(note.id)}>{note.title}</button><button type="button" onClick={() => detachNote(note.id)}>Detach</button></div>)}</div>
              )}
            </div>
          </form>
        )}
      </section>
    </main>
  );
}

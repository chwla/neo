import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";

const STATUSES = ["todo", "doing", "blocked", "done"];
const PRIORITIES = ["low", "medium", "high", "critical"];
const ACTIVE_RUN_STATUSES = new Set(["queued", "planning", "running", "waiting_approval"]);

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

function formatStatus(value) {
  if (value === "waiting_approval") return "Waiting for approval";
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function Tasks({ initialTaskId = null, initialProjectId = null, onBack, onOpenNote, onTaskChange }) {
  const [tasks, setTasks] = useState([]);
  const [projects, setProjects] = useState([]);
  const [notes, setNotes] = useState([]);
  const [taskTags, setTaskTags] = useState([]);
  const [selectedTask, setSelectedTask] = useState(null);
  const [linkedNotes, setLinkedNotes] = useState([]);
  const [subtasks, setSubtasks] = useState([]);
  const [draft, setDraft] = useState(toDraft(null));
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState("");
  const [projectFilter, setProjectFilter] = useState(initialProjectId || "");
  const [tag, setTag] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [agentRuns, setAgentRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [runObjective, setRunObjective] = useState("");
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentMessage, setAgentMessage] = useState("");

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

  useEffect(() => {
    if (!selectedRun || !ACTIVE_RUN_STATUSES.has(selectedRun.run.status)) return undefined;
    const interval = window.setInterval(() => {
      refreshAgentRuns(selectedRun.run.task_id, selectedRun.run.id).catch(() => {});
    }, 1000);
    return () => window.clearInterval(interval);
  }, [selectedRun?.run?.id, selectedRun?.run?.status]);

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
      setSubtasks(data.subtasks || []);
      await refreshAgentRuns(data.task.id, null, true);
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
      setSubtasks([]);
      setAgentRuns([]);
      setSelectedRun(null);
      setRunObjective("");
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
    setSelectedTask(null); setLinkedNotes([]); setSubtasks([]); setDraft(toDraft(null)); onTaskChange?.(null); await loadTasks();
    setAgentRuns([]); setSelectedRun(null); setRunObjective("");
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

  async function refreshAgentRuns(taskId = selectedTask?.id, runId = selectedRun?.run?.id, openLatest = false) {
    if (!taskId) return;
    const data = await api.taskAgentRuns(taskId);
    const runs = data.runs || [];
    setAgentRuns(runs);
    const targetId = runId || (openLatest ? runs[0]?.id : null);
    if (targetId) {
      const detail = await api.agentRun(targetId);
      setSelectedRun(detail);
    } else if (runs.length === 0) {
      setSelectedRun(null);
    }
  }

  async function startAgentRun() {
    if (!selectedTask) return;
    setAgentBusy(true);
    setAgentMessage("");
    setError("");
    try {
      const data = await api.startAgentRun({
        task_id: selectedTask.id,
        objective: runObjective.trim() || null,
        mode: "assist",
      });
      await refreshAgentRuns(selectedTask.id, data.run.id);
      setAgentMessage("Agent run started.");
    } catch (err) {
      setError(err.message || "Failed to start agent run.");
    } finally {
      setAgentBusy(false);
    }
  }

  async function cancelAgentRun() {
    if (!selectedRun) return;
    setAgentBusy(true);
    try {
      await api.cancelAgentRun(selectedRun.run.id);
      await refreshAgentRuns(selectedTask.id, selectedRun.run.id);
      setAgentMessage("Agent run cancelled. Partial output was preserved.");
    } catch (err) {
      setError(err.message || "Failed to cancel agent run.");
    } finally {
      setAgentBusy(false);
    }
  }

  async function saveRunToNote() {
    if (!selectedRun) return;
    setAgentBusy(true);
    try {
      const data = await api.saveAgentRunToNote(selectedRun.run.id, { tags: ["agent", "task-output"] });
      await Promise.all([refreshAgentRuns(selectedTask.id, selectedRun.run.id), loadMeta()]);
      setAgentMessage(data.already_saved ? "Output was already saved to this note." : "Output saved to note.");
    } catch (err) {
      setError(err.message || "Failed to save agent output.");
    } finally {
      setAgentBusy(false);
    }
  }

  async function approveAgentStep(stepId, approved) {
    if (!selectedRun) return;
    setAgentBusy(true);
    try {
      await api.approveAgentStep(selectedRun.run.id, stepId, approved);
      await refreshAgentRuns(selectedTask.id, selectedRun.run.id);
    } catch (err) {
      setError(err.message || "Failed to record approval.");
    } finally {
      setAgentBusy(false);
    }
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
              <div className="task-card-title">{task.parent_task_id ? "↳ " : ""}{task.pinned ? "★ " : ""}{task.title}</div>
              <div className="task-card-meta"><span className={`task-status ${task.status}`}>{task.status}</span><span className={`task-priority ${task.priority}`}>{task.priority}</span>{task.project_title ? <span>{task.project_title}</span> : null}</div>
              {task.subtask_count ? <div className="task-card-updated">{task.open_subtask_count}/{task.subtask_count} subtasks open</div> : null}
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
              <label>Due date<input type="datetime-local" value={draft.due_at} onInput={(e) => setDraft({ ...draft, due_at: e.currentTarget.value })} /></label>
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

            {subtasks.length ? (
              <section className="task-subtasks-section">
                <div className="task-section-title">Subtasks</div>
                <div className="task-subtask-list">
                  {subtasks.map((subtask, index) => (
                    <button type="button" key={subtask.id} onClick={() => openTask(subtask.id)}>
                      <span>{index + 1}. {subtask.title}</span>
                      <span className={`task-status ${subtask.status}`}>{subtask.status}</span>
                    </button>
                  ))}
                </div>
              </section>
            ) : null}

            <section className="agent-runs-section">
              <div className="agent-runs-header">
                <div>
                  <div className="task-section-title">Agent Runs</div>
                  <p className="task-help">Task-linked assisted execution. No shell or destructive actions.</p>
                </div>
                <button className="neo-button" type="button" onClick={startAgentRun} disabled={agentBusy}>
                  Run Agent
                </button>
              </div>
              <textarea
                className="agent-objective-input"
                value={runObjective}
                onChange={(event) => setRunObjective(event.target.value)}
                placeholder="Optional run objective; defaults to the task description or title"
                rows={2}
              />
              {agentMessage ? <div className="agent-message">{agentMessage}</div> : null}
              {agentRuns.length === 0 ? (
                <p className="task-help">No agent runs yet.<br />Start a run to work on this task.</p>
              ) : (
                <div className="agent-run-list">
                  {agentRuns.map((run) => (
                    <button type="button" key={run.id} className={selectedRun?.run?.id === run.id ? "selected" : ""}
                      onClick={() => refreshAgentRuns(selectedTask.id, run.id)}>
                      <strong>{run.title}</strong>
                      <span className={`agent-status ${run.status}`}>{formatStatus(run.status)}</span>
                      <small>{formatTime(run.created_at)}</small>
                    </button>
                  ))}
                </div>
              )}

              {selectedRun ? (
                <div className="agent-run-detail">
                  <div className="agent-run-toolbar">
                    <span className={`agent-status ${selectedRun.run.status}`}>{formatStatus(selectedRun.run.status)}</span>
                    {ACTIVE_RUN_STATUSES.has(selectedRun.run.status) ? (
                      <button type="button" onClick={cancelAgentRun} disabled={agentBusy}>Cancel Run</button>
                    ) : null}
                    {selectedRun.run.status === "completed" ? (
                      <button type="button" onClick={saveRunToNote} disabled={agentBusy}>Save output to note</button>
                    ) : null}
                  </div>
                  <div className="agent-run-objective"><strong>Objective</strong><span>{selectedRun.run.objective}</span><small>{formatTime(selectedRun.run.created_at)}</small></div>
                  {selectedRun.run.status === "failed" ? <div className="agent-failure">Agent run failed.<br />The logs below show what happened.</div> : null}
                  {selectedRun.run.status === "cancelled" ? <div className="agent-message">Agent run cancelled.<br />Partial output was preserved.</div> : null}
                  <div className="agent-step-list">
                    {selectedRun.steps.map((step) => (
                      <article key={step.id} className="agent-step">
                        <div><strong>{step.step_index + 1}. {step.title}</strong><span className={`agent-status ${step.status}`}>{formatStatus(step.status)}</span></div>
                        {step.output_text ? <pre>{step.output_text}</pre> : null}
                        {step.error ? <div className="agent-failure">{step.error}</div> : null}
                        {step.status === "waiting_approval" ? <div className="agent-approval-actions"><button type="button" onClick={() => approveAgentStep(step.id, true)}>Approve</button><button type="button" onClick={() => approveAgentStep(step.id, false)}>Deny</button></div> : null}
                      </article>
                    ))}
                  </div>
                  {selectedRun.run.final_output ? <div className="agent-final-output"><strong>Final output</strong><pre>{selectedRun.run.final_output}</pre></div> : null}
                  {selectedRun.artifacts.length ? <div className="agent-artifacts"><strong>Artifacts</strong>{selectedRun.artifacts.map((artifact) => <div key={artifact.id}><span>{artifact.title}</span>{artifact.note_id ? <button type="button" onClick={() => onOpenNote?.(artifact.note_id)}>Open Note</button> : null}</div>)}</div> : null}
                </div>
              ) : null}
            </section>
          </form>
        )}
      </section>
    </main>
  );
}

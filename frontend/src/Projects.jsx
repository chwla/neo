import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";
import FileAttachments from "./FileAttachments.jsx";
import Repos from "./Repos.jsx";
import RelatedMemories from "./RelatedMemories.jsx";
import Icon from "./WorkspaceIcon.jsx";

const STATUSES = ["active", "paused", "completed", "archived"];
const PRIORITIES = ["low", "medium", "high", "critical"];

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

function draftFromProject(project) {
  return {
    title: project?.title || "",
    description: project?.description || "",
    status: project?.status || "active",
    priority: project?.priority || "medium",
    tagsText: tagsToText(project?.tags || []),
  };
}

function projectChanged(draft, project) {
  if (!project) {
    return Boolean(draft.title.trim() || draft.description.trim() || draft.tagsText.trim());
  }
  return (
    draft.title !== (project.title || "") ||
    draft.description !== (project.description || "") ||
    draft.status !== (project.status || "active") ||
    draft.priority !== (project.priority || "medium") ||
    draft.tagsText !== tagsToText(project.tags || [])
  );
}

export default function Projects({ initialProjectId = null, onBack, onOpenNote, onOpenTask, onOpenFile, onProjectChange }) {
  const [projects, setProjects] = useState([]);
  const [projectTags, setProjectTags] = useState([]);
  const [notes, setNotes] = useState([]);
  const [linkedNotes, setLinkedNotes] = useState([]);
  const [projectTasks, setProjectTasks] = useState([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedProject, setSelectedProject] = useState(null);
  const [draft, setDraft] = useState(draftFromProject(null));
  const [isNew, setIsNew] = useState(false);
  const [attachNoteId, setAttachNoteId] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState("overview");

  const dirty = useMemo(
    () => projectChanged(draft, isNew ? null : selectedProject),
    [draft, isNew, selectedProject],
  );

  const loadProjects = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.projectsList({
        q: query.trim(),
        tag: tagFilter,
        status: statusFilter,
        includeArchived,
        limit: 75,
      });
      setProjects(data.projects || []);
      setTotal(data.total || 0);
    } finally {
      setLoading(false);
    }
  }, [includeArchived, query, statusFilter, tagFilter]);

  const loadProjectTags = useCallback(async () => {
    const data = await api.projectsTags();
    setProjectTags(data.tags || []);
  }, []);

  const loadNotes = useCallback(async () => {
    const data = await api.notesList({ limit: 100 });
    setNotes(data.notes || []);
  }, []);

  useEffect(() => {
    loadProjects().catch((err) => setError(err.message || "Failed to load projects."));
  }, [loadProjects]);

  useEffect(() => {
    Promise.all([loadProjectTags(), loadNotes()]).catch(() => {});
  }, [loadProjectTags, loadNotes]);

  useEffect(() => {
    if (initialProjectId && selectedProject?.id !== initialProjectId) {
      openProject(initialProjectId, { skipLeaveCheck: true });
    } else if (!initialProjectId && selectedProject && !isNew) {
      setSelectedProject(null);
      setLinkedNotes([]);
      setProjectTasks([]);
      setDraft(draftFromProject(null));
      setStatus("");
    }
  }, [initialProjectId, isNew, selectedProject?.id]);

  function canLeaveCurrent() {
    return !dirty || window.confirm("Discard unsaved project changes?");
  }

  async function openProject(projectId, options = {}) {
    if (!options.skipLeaveCheck && !canLeaveCurrent()) return;
    setError("");
    setStatus("");
    try {
      const [data, tasksData] = await Promise.all([api.project(projectId), api.projectTasks(projectId, { includeDone: true })]);
      setSelectedProject(data.project);
      setDraft(draftFromProject(data.project));
      setLinkedNotes(data.notes || []);
      setProjectTasks(tasksData.tasks || []);
      setIsNew(false);
      setAttachNoteId("");
      onProjectChange?.(data.project.id);
    } catch (err) {
      setError(err.message || "Failed to open project.");
    }
  }

  function startNewProject() {
    if (!canLeaveCurrent()) return;
    setSelectedProject(null);
    setLinkedNotes([]);
    setProjectTasks([]);
    setDraft(draftFromProject(null));
    setIsNew(true);
    setAttachNoteId("");
    setStatus("Unsaved changes");
    setError("");
    onProjectChange?.(null);
  }

  async function saveProject() {
    setError("");
    const payload = {
      title: draft.title,
      description: draft.description,
      status: draft.status,
      priority: draft.priority,
      tags: textToTags(draft.tagsText),
    };
    try {
      setStatus("Saving...");
      const data = isNew
        ? await api.createWorkspaceProject(payload)
        : await api.updateWorkspaceProject(selectedProject.id, payload);
      setSelectedProject(data.project);
      setDraft(draftFromProject(data.project));
      setIsNew(false);
      setStatus("Saved");
      await Promise.all([loadProjects(), loadProjectTags()]);
      if (data.project?.id) {
        const full = await api.project(data.project.id);
        setLinkedNotes(full.notes || []);
        onProjectChange?.(data.project.id, { replace: isNew });
      }
    } catch (err) {
      setStatus("Unsaved changes");
      setError(err.message || "Failed to save project.");
    }
  }

  async function pinSelected() {
    if (!selectedProject || isNew) return;
    setError("");
    try {
      const data = await api.pinProject(selectedProject.id, !selectedProject.pinned);
      setSelectedProject(data.project);
      await loadProjects();
    } catch (err) {
      setError(err.message || "Failed to update pin.");
    }
  }

  async function archiveSelected() {
    if (!selectedProject || isNew) return;
    setError("");
    try {
      const data = await api.archiveProject(selectedProject.id, !selectedProject.archived);
      setSelectedProject(data.project);
      setDraft(draftFromProject(data.project));
      await loadProjects();
    } catch (err) {
      setError(err.message || "Failed to archive project.");
    }
  }

  async function deleteSelected() {
    if (!selectedProject || isNew) return;
    if (!window.confirm(`Delete project ${selectedProject.title}?`)) return;
    setError("");
    try {
      await api.deleteWorkspaceProject(selectedProject.id);
      setSelectedProject(null);
      setLinkedNotes([]);
      setProjectTasks([]);
      setDraft(draftFromProject(null));
      setIsNew(false);
      setStatus("");
      onProjectChange?.(null, { replace: true });
      await Promise.all([loadProjects(), loadProjectTags()]);
    } catch (err) {
      setError(err.message || "Failed to delete project.");
    }
  }

  async function attachSelectedNote() {
    if (!selectedProject || !attachNoteId) return;
    setError("");
    try {
      await api.attachNoteToProject(selectedProject.id, attachNoteId);
      const data = await api.project(selectedProject.id);
      setSelectedProject(data.project);
      setLinkedNotes(data.notes || []);
      setAttachNoteId("");
      await loadProjects();
    } catch (err) {
      setError(err.message || "Failed to attach note.");
    }
  }

  async function detachNote(noteId) {
    if (!selectedProject) return;
    setError("");
    try {
      await api.detachNoteFromProject(selectedProject.id, noteId);
      const data = await api.project(selectedProject.id);
      setSelectedProject(data.project);
      setLinkedNotes(data.notes || []);
      await loadProjects();
    } catch (err) {
      setError(err.message || "Failed to detach note.");
    }
  }

  async function createTaskForProject() {
    if (!selectedProject || isNew) return;
    setError("");
    try {
      const data = await api.createProjectTask(selectedProject.id, { title: "New task" });
      const tasksData = await api.projectTasks(selectedProject.id, { includeDone: true });
      setProjectTasks(tasksData.tasks || []);
      onOpenTask?.(data.task.id);
    } catch (err) {
      setError(err.message || "Failed to create project task.");
    }
  }

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
    setStatus("Unsaved changes");
  }

  const attachableNotes = notes.filter((note) => !linkedNotes.some((linked) => linked.id === note.id));
  const editing = Boolean(selectedProject) || isNew;
  const filtered = Boolean(query.trim() || tagFilter || statusFilter);
  const openTasks = projectTasks.filter((task) => !["done", "archived"].includes(task.status));
  const blockedTasks = projectTasks.filter((task) => task.status === "blocked");
  const doneTasks = projectTasks.filter((task) => task.status === "done");

  const TABS = [
    ["overview", "Overview", null],
    ["tasks", "Tasks", projectTasks.length],
    ["notes", "Notes", linkedNotes.length],
    ["files", "Files", null],
    ["repos", "Repos", null],
  ];

  return (
    <div className="ws">
      <aside className="ws-rail">
        <header className="ws-rail-head">
          <div className="ws-rail-top">
            <button className="ws-back" type="button" onClick={onBack}>
              <Icon name="back" />
              Chat
            </button>
            <span className="ws-rail-count">
              {loading ? "…" : `${total} project${total === 1 ? "" : "s"}`}
            </span>
          </div>
          <h1 className="ws-rail-title">Projects</h1>
          <button className="ws-primary" type="button" onClick={startNewProject}>
            <Icon name="plus" />
            New project
          </button>
        </header>

        <div className="ws-filters">
          <div className="ws-search">
            <Icon name="search" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search projects"
              aria-label="Search projects"
            />
            {query && (
              <button className="ws-search-clear" type="button" onClick={() => setQuery("")} aria-label="Clear search">
                ×
              </button>
            )}
          </div>
          <div className="ws-filter-row">
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              aria-label="Filter by status"
            >
              <option value="">All statuses</option>
              {STATUSES.map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
            <button
              className={`ws-toggle ${includeArchived ? "on" : ""}`}
              type="button"
              aria-pressed={includeArchived}
              onClick={() => setIncludeArchived((value) => !value)}
            >
              Archived
            </button>
          </div>
        </div>

        <div className="ws-list">
          {projects.length === 0 ? (
            <div className="ws-list-empty">
              {filtered ? (
                <>
                  <p>No projects match.</p>
                  <button
                    className="ws-link"
                    type="button"
                    onClick={() => { setQuery(""); setTagFilter(""); setStatusFilter(""); }}
                  >
                    Clear filters
                  </button>
                </>
              ) : (
                <p>No projects yet.</p>
              )}
            </div>
          ) : (
            projects.map((project) => (
              <a
                key={project.id}
                className={`ws-row ${selectedProject?.id === project.id ? "active" : ""} ${project.archived ? "dim" : ""}`}
                href={`/projects/${encodeURIComponent(project.id)}`}
                onClick={(event) => {
                  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  openProject(project.id);
                }}
              >
                <span className="ws-row-head">
                  {project.pinned && <Icon name="pin" className="ws-row-pin" />}
                  <span className="ws-row-title">{project.title}</span>
                  <time className="ws-row-time">{formatTime(project.updated_at)}</time>
                </span>
                {(project.preview || project.description) && (
                  <span className="ws-row-excerpt">{project.preview || project.description}</span>
                )}
                <span className="ws-row-meta">
                  <span className={`ws-badge ${statusTone(project.status)}`}>{project.status}</span>
                  <span className="ws-badge mute">{project.priority}</span>
                  {project.linked_notes_count > 0 && (
                    <span className="ws-row-more">{project.linked_notes_count} notes</span>
                  )}
                  {(project.tags || []).slice(0, 2).map((tag) => (
                    <span className="ws-chip" key={tag}>{tag}</span>
                  ))}
                </span>
              </a>
            ))
          )}
        </div>
      </aside>

      <section className="ws-main">
        {!editing ? (
          <div className="ws-blank">
            <div className="ws-blank-mark"><Icon name="folder" /></div>
            <h2>No project open</h2>
            <p>Pick a project from the list, or create one to group notes, tasks, files and repositories.</p>
            <button className="ws-primary" type="button" onClick={startNewProject}>
              <Icon name="plus" />
              New project
            </button>
            {error && <div className="ws-error">{error}</div>}
          </div>
        ) : (
          <>
            <div className="ws-toolbar">
              <div className="ws-toolbar-state">
                <span className={`ws-dot ${dirty ? "dirty" : ""}`} />
                <span className="ws-state-text">{dirty ? "Unsaved changes" : status || "Saved"}</span>
                {!isNew && selectedProject?.updated_at && (
                  <span className="ws-meta">Edited {formatTime(selectedProject.updated_at)}</span>
                )}
                {selectedProject?.archived && <span className="ws-badge">Archived</span>}
                {selectedProject?.pinned && <span className="ws-badge accent">Pinned</span>}
              </div>
              <div className="ws-toolbar-actions">
                {!isNew && (
                  <>
                    <button className="ws-action" type="button" onClick={pinSelected}>
                      <Icon name="pin" />
                      {selectedProject?.pinned ? "Unpin" : "Pin"}
                    </button>
                    <button className="ws-action" type="button" onClick={archiveSelected}>
                      <Icon name="archive" />
                      {selectedProject?.archived ? "Unarchive" : "Archive"}
                    </button>
                    <button className="ws-action danger" type="button" onClick={deleteSelected}>
                      <Icon name="trash" />
                      Delete
                    </button>
                  </>
                )}
                <button
                  className="ws-save"
                  type="button"
                  onClick={saveProject}
                  disabled={!dirty || !draft.title.trim()}
                >
                  Save
                </button>
              </div>
            </div>

            {!isNew && (
              <nav className="ws-tabs" aria-label="Project sections">
                {TABS.map(([key, label, count]) => (
                  <button
                    key={key}
                    type="button"
                    className={`ws-tab ${tab === key ? "on" : ""}`}
                    aria-pressed={tab === key}
                    onClick={() => setTab(key)}
                  >
                    {label}
                    {count ? <span className="ws-tab-count">{count}</span> : null}
                  </button>
                ))}
              </nav>
            )}

            <div className="ws-stage">
              <div className="ws-doc">
                {(isNew || tab === "overview") && (
                  <>
                    <input
                      className="ws-title-input"
                      value={draft.title}
                      onChange={(event) => updateDraft("title", event.target.value)}
                      placeholder="Untitled project"
                      maxLength={200}
                      aria-label="Project title"
                    />

                    <div className="ws-field-row">
                      <label className="ws-field">
                        <span>Status</span>
                        <select value={draft.status} onChange={(event) => updateDraft("status", event.target.value)}>
                          {STATUSES.map((item) => (
                            <option key={item} value={item}>{item}</option>
                          ))}
                        </select>
                      </label>
                      <label className="ws-field">
                        <span>Priority</span>
                        <select value={draft.priority} onChange={(event) => updateDraft("priority", event.target.value)}>
                          {PRIORITIES.map((item) => (
                            <option key={item} value={item}>{item}</option>
                          ))}
                        </select>
                      </label>
                      <label className="ws-field">
                        <span>Tags</span>
                        <input
                          value={draft.tagsText}
                          onChange={(event) => updateDraft("tagsText", event.target.value)}
                          placeholder="comma separated"
                        />
                      </label>
                    </div>

                    <textarea
                      className="ws-textarea"
                      value={draft.description}
                      onChange={(event) => updateDraft("description", event.target.value)}
                      placeholder="What is this project about?"
                      aria-label="Project description"
                    />

                    {!isNew && (
                      <section className="ws-section">
                        <div className="ws-section-head">
                          <h3 className="ws-section-title">At a glance</h3>
                        </div>
                        <div className="ws-row-meta">
                          <span className="ws-badge">{openTasks.length} open</span>
                          <span className={`ws-badge ${blockedTasks.length ? "warn" : "mute"}`}>
                            {blockedTasks.length} blocked
                          </span>
                          <span className="ws-badge mute">{doneTasks.length} done</span>
                          <span className="ws-badge mute">{linkedNotes.length} notes</span>
                        </div>
                      </section>
                    )}
                  </>
                )}

                {!isNew && tab === "tasks" && (
                  <section className="ws-section">
                    <div className="ws-section-head">
                      <h3 className="ws-section-title">Tasks</h3>
                      <button className="ws-action" type="button" onClick={createTaskForProject}>
                        <Icon name="plus" />
                        New task
                      </button>
                    </div>
                    {projectTasks.length === 0 ? (
                      <p className="ws-empty-line">No tasks linked to this project.</p>
                    ) : (
                      <div className="ws-tiles">
                        {projectTasks.map((task) => (
                          <button type="button" key={task.id} className="ws-tile" onClick={() => onOpenTask?.(task.id)}>
                            <strong>{task.title}</strong>
                            <span className={`ws-badge ${taskTone(task.status)}`}>{task.status}</span>
                            <span>{task.priority}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </section>
                )}

                {!isNew && tab === "notes" && (
                  <section className="ws-section">
                    <div className="ws-section-head">
                      <h3 className="ws-section-title">Linked notes</h3>
                    </div>
                    {linkedNotes.length === 0 ? (
                      <p className="ws-empty-line">
                        No notes attached yet. Attach a note or save a research report to this project.
                      </p>
                    ) : (
                      <div className="ws-tiles">
                        {linkedNotes.map((note) => (
                          <div className="ws-tile" key={note.id}>
                            <strong>{note.title}</strong>
                            <small>{formatTime(note.updated_at)}</small>
                            <span className="ws-tile-actions">
                              <button className="ws-action" type="button" onClick={() => onOpenNote?.(note.id)}>
                                Open
                              </button>
                              <button className="ws-action danger" type="button" onClick={() => detachNote(note.id)}>
                                Detach
                              </button>
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="ws-attach">
                      <select
                        value={attachNoteId}
                        onChange={(event) => setAttachNoteId(event.target.value)}
                        aria-label="Attach a note"
                      >
                        <option value="">Attach a note…</option>
                        {attachableNotes.map((note) => (
                          <option key={note.id} value={note.id}>{note.title}</option>
                        ))}
                      </select>
                      <button type="button" onClick={attachSelectedNote} disabled={!attachNoteId}>
                        Attach
                      </button>
                    </div>
                  </section>
                )}

                {!isNew && tab === "files" && (
                  <>
                    <FileAttachments linkType="project" targetId={selectedProject.id} onOpenFile={onOpenFile} />
                    <RelatedMemories scopeType="project" scopeId={selectedProject.id} />
                  </>
                )}

                {!isNew && tab === "repos" && (
                  <Repos projectId={selectedProject.id} onOpenFile={onOpenFile} compact />
                )}

                {error && <div className="ws-error">{error}</div>}
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function statusTone(status) {
  if (status === "active") return "accent";
  if (status === "paused") return "warn";
  if (status === "archived") return "mute";
  return "";
}

function taskTone(status) {
  if (status === "done") return "accent";
  if (status === "blocked") return "danger";
  if (status === "doing") return "warn";
  return "mute";
}

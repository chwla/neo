import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";
import FileAttachments from "./FileAttachments.jsx";
import Repos from "./Repos.jsx";
import RelatedMemories from "./RelatedMemories.jsx";

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

  return (
    <main className="projects-layout">
      <section className="projects-list-pane">
        <div className="projects-header">
          <button className="research-back" onClick={onBack} type="button">Chat</button>
          <h2 className="projects-title">Projects</h2>
        </div>

        <button className="projects-new-btn" type="button" onClick={startNewProject}>
          New Project
        </button>

        <input
          className="projects-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search projects"
        />

        <div className="projects-filters">
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">All statuses</option>
            {STATUSES.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
          <select value={tagFilter} onChange={(event) => setTagFilter(event.target.value)}>
            <option value="">All tags</option>
            {projectTags.map((item) => (
              <option key={item.tag} value={item.tag}>{item.tag} ({item.count})</option>
            ))}
          </select>
        </div>

        <label className="projects-archived-toggle">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => setIncludeArchived(event.target.checked)}
          />
          Archived
        </label>

        <div className="projects-list-meta">
          {loading ? "Loading..." : `${total} project${total === 1 ? "" : "s"}`}
        </div>

        <div className="projects-list">
          {projects.length === 0 ? (
            <div className="projects-empty">
              {query.trim() || tagFilter || statusFilter
                ? "No projects match your search."
                : "No projects yet. Create a project to organize notes and research."}
            </div>
          ) : (
            projects.map((project) => (
              <a
                key={project.id}
                className={`projects-item ${selectedProject?.id === project.id ? "active" : ""}`}
                href={`/projects/${encodeURIComponent(project.id)}`}
                onClick={(event) => {
                  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  openProject(project.id);
                }}
              >
                <span className="projects-item-title">
                  {project.pinned && <span className="projects-pin" title="Pinned">PIN</span>}
                  {project.title}
                </span>
                <span className="projects-item-preview">{project.preview || project.description}</span>
                <span className="projects-item-meta">
                  <strong>{project.status}</strong>
                  <span>{project.priority}</span>
                  {project.linked_notes_count > 0 && <span>{project.linked_notes_count} notes</span>}
                </span>
                <span className="projects-item-tags">
                  {(project.tags || []).slice(0, 4).map((tag) => (
                    <span key={tag}>{tag}</span>
                  ))}
                </span>
                <span className="projects-item-time">{formatTime(project.updated_at)}</span>
              </a>
            ))
          )}
        </div>
      </section>

      <section className="projects-editor-pane">
        {!selectedProject && !isNew ? (
          <div className="projects-editor-empty">Select a project or create a new one.</div>
        ) : (
          <>
            <div className="projects-editor-toolbar">
              <span className={`projects-save-state ${dirty ? "dirty" : ""}`}>
                {dirty ? "Unsaved changes" : status || "Saved"}
              </span>
              <button type="button" onClick={saveProject} disabled={!dirty || !draft.title.trim()}>
                Save
              </button>
              {!isNew && (
                <>
                  <button type="button" onClick={pinSelected}>
                    {selectedProject?.pinned ? "Unpin" : "Pin"}
                  </button>
                  <button type="button" onClick={archiveSelected}>
                    {selectedProject?.archived ? "Unarchive" : "Archive"}
                  </button>
                  <button className="projects-danger-btn" type="button" onClick={deleteSelected}>
                    Delete
                  </button>
                </>
              )}
            </div>

            <input
              className="projects-title-input"
              value={draft.title}
              onChange={(event) => updateDraft("title", event.target.value)}
              placeholder="Project title"
              maxLength={200}
            />

            <textarea
              className="projects-description-input"
              value={draft.description}
              onChange={(event) => updateDraft("description", event.target.value)}
              placeholder="Description"
              rows={5}
            />

            <div className="projects-editor-grid">
              <select value={draft.status} onChange={(event) => updateDraft("status", event.target.value)}>
                {STATUSES.map((item) => (
                  <option key={item} value={item}>{item}</option>
                ))}
              </select>
              <select value={draft.priority} onChange={(event) => updateDraft("priority", event.target.value)}>
                {PRIORITIES.map((item) => (
                  <option key={item} value={item}>{item}</option>
                ))}
              </select>
            </div>

            <input
              className="projects-tags-input"
              value={draft.tagsText}
              onChange={(event) => updateDraft("tagsText", event.target.value)}
              placeholder="Tags, separated by commas"
            />

            {!isNew && (
              <section className="projects-linked-notes project-tasks-section">
                <div className="projects-section-title">Tasks</div>
                <div className="project-task-counts">
                  <span>{projectTasks.filter((task) => !["done", "archived"].includes(task.status)).length} open</span>
                  <span>{projectTasks.filter((task) => task.status === "blocked").length} blocked</span>
                  <span>{projectTasks.filter((task) => task.status === "done").length} completed</span>
                </div>
                <button type="button" onClick={createTaskForProject}>New Task for this Project</button>
                {projectTasks.length === 0 ? (
                  <div className="projects-empty small">No tasks linked to this project.</div>
                ) : (
                  <div className="project-task-list">
                    {projectTasks.slice(0, 8).map((task) => (
                      <button type="button" key={task.id} onClick={() => onOpenTask?.(task.id)}>
                        <strong>{task.title}</strong><span>{task.status} · {task.priority}</span>
                      </button>
                    ))}
                  </div>
                )}
              </section>
            )}

            {!isNew && (
              <>
                <FileAttachments linkType="project" targetId={selectedProject.id} onOpenFile={onOpenFile} />
                <RelatedMemories scopeType="project" scopeId={selectedProject.id} />
              </>
            )}

            {!isNew && (
              <Repos projectId={selectedProject.id} onOpenFile={onOpenFile} compact />
            )}

            {!isNew && (
              <section className="projects-linked-notes">
                <div className="projects-section-title">Linked Notes</div>
                <div className="projects-attach-row">
                  <select value={attachNoteId} onChange={(event) => setAttachNoteId(event.target.value)}>
                    <option value="">Select note</option>
                    {attachableNotes.map((note) => (
                      <option key={note.id} value={note.id}>{note.title}</option>
                    ))}
                  </select>
                  <button type="button" onClick={attachSelectedNote} disabled={!attachNoteId}>
                    Attach
                  </button>
                </div>

                {linkedNotes.length === 0 ? (
                  <div className="projects-empty small">
                    No notes attached yet. Attach notes or save research reports to this project.
                  </div>
                ) : (
                  <div className="projects-linked-list">
                    {linkedNotes.map((note) => (
                      <article key={note.id} className="projects-linked-note">
                        <div className="projects-linked-note-main">
                          <strong>{note.title}</strong>
                          <span>{note.preview || note.summary || note.body}</span>
                          <div className="projects-item-tags">
                            {(note.tags || []).slice(0, 5).map((tag) => (
                              <span key={tag}>{tag}</span>
                            ))}
                          </div>
                          <small>{formatTime(note.updated_at)}</small>
                        </div>
                        <div className="projects-linked-note-actions">
                          <button type="button" onClick={() => onOpenNote?.(note.id)}>Open Note</button>
                          <button type="button" onClick={() => detachNote(note.id)}>Detach</button>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            )}

            {error && <div className="projects-error">{error}</div>}
          </>
        )}
        {error && !selectedProject && !isNew && <div className="projects-error">{error}</div>}
      </section>
    </main>
  );
}

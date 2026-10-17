import { useEffect, useRef, useState } from "react";

import { api } from "../api.js";
import FileAttachments from "../FileAttachments.jsx";
import Icon from "../WorkspaceIcon.jsx";
import { registerModal } from "../modalStack.js";
import { longDate } from "./noteDates.js";
import { nodeText } from "./projection.js";

const VERSION_LABELS = {
  original_markdown: "Original Markdown",
  rich_before_markdown_write: "Before an update from Neo",
  pre_restore: "Before a version was restored",
  conflict_displaced: "Replaced in a conflict",
  conflict_mine: "Your version from a conflict",
};

const SOURCE_LABELS = {
  research_report: "Research report",
  agent_run: "Agent run",
  chat: "Chat",
  web_source: "Web page",
};

function countWords(doc) {
  const text = nodeText(doc ?? {}).trim();
  return { words: text ? text.split(/\s+/).length : 0, characters: text.length };
}

/**
 * Everything about a note that is not in it: dates, counts, folder, tags,
 * summary, where it came from, its links to Projects, Tasks and Files, and its
 * saved versions. Neo's additions to Apple Notes live here so they never crowd
 * the writing.
 */
export default function NoteInfoPanel({ controller, session, folderName, onOpenTask, onOpenFile, onClose }) {
  const note = session?.note ?? {};
  const [summary, setSummary] = useState(note.summary ?? "");
  const [projects, setProjects] = useState(null);
  const [tasks, setTasks] = useState(null);
  const [allProjects, setAllProjects] = useState([]);
  const [allTasks, setAllTasks] = useState([]);
  const [versions, setVersions] = useState(null);
  const [error, setError] = useState("");
  const [counts] = useState(() => countWords(controller.currentDoc()));
  const panel = useRef(null);
  const noteId = session?.id;

  useEffect(() => registerModal(onClose), [onClose]);
  useEffect(() => setSummary(note.summary ?? ""), [noteId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!noteId) return undefined;
    let cancelled = false;
    Promise.all([
      api.noteProjects(noteId),
      api.noteTasks(noteId),
      api.projectsList({ limit: 100 }),
      api.tasksList({ includeDone: false, limit: 100 }),
      api.noteVersions(noteId),
    ])
      .then(([linkedProjects, linkedTasks, everyProject, everyTask, saved]) => {
        if (cancelled) return;
        setProjects(linkedProjects.projects || []);
        setTasks(linkedTasks.tasks || []);
        setAllProjects(everyProject.projects || []);
        setAllTasks(everyTask.tasks || []);
        setVersions(saved || []);
      })
      .catch((err) => !cancelled && setError(err.message || "Couldn't load this note's links."));
    return () => {
      cancelled = true;
    };
  }, [noteId]);

  async function refreshLinks() {
    const [linkedProjects, linkedTasks] = await Promise.all([api.noteProjects(noteId), api.noteTasks(noteId)]);
    setProjects(linkedProjects.projects || []);
    setTasks(linkedTasks.tasks || []);
  }

  async function act(work) {
    setError("");
    try {
      await work();
      await refreshLinks();
    } catch (err) {
      setError(err.message || "That didn't work.");
    }
  }

  const source = note.source_type && note.source_type !== "manual" ? note.source_type : null;
  const editable = !session?.readOnly;

  return (
    <aside ref={panel} className="nw-info" aria-label="Note info">
      <header className="nw-info-head">
        <h2>Info</h2>
        <button type="button" className="nw-tool" onClick={onClose} aria-label="Close info">
          <Icon name="close" />
        </button>
      </header>
      <dl className="nw-info-facts">
        <dt>Edited</dt>
        <dd>{longDate(note.updated_at)}</dd>
        <dt>Created</dt>
        <dd>{longDate(note.created_at)}</dd>
        <dt>Folder</dt>
        <dd>{folderName}</dd>
        <dt>Length</dt>
        <dd>{counts.words} words, {counts.characters} characters</dd>
        {note.tags?.length > 0 && (
          <>
            <dt>Tags</dt>
            <dd>{note.tags.map((tag) => `#${tag}`).join(" ")}</dd>
          </>
        )}
        {source && (
          <>
            <dt>From</dt>
            <dd>
              {SOURCE_LABELS[source] ?? source}
              {note.source_title ? `: ${note.source_title}` : ""}
              {note.source_url && (
                <>
                  {" "}
                  <a href={note.source_url} target="_blank" rel="noopener noreferrer nofollow">Open</a>
                </>
              )}
            </dd>
          </>
        )}
      </dl>

      <label className="nw-info-summary">
        <span>Summary</span>
        <textarea
          value={summary}
          disabled={!editable || !session?.key}
          placeholder="A line about this note"
          onChange={(event) => setSummary(event.target.value)}
          onBlur={() => {
            if ((summary || "") !== (note.summary || "")) controller.setSummary(session.key, summary);
          }}
          rows={2}
        />
      </label>

      {noteId && (
        <>
          <section className="nw-info-section">
            <h3>Projects</h3>
            {projects?.length ? (
              <ul>
                {projects.map((project) => (
                  <li key={project.id}>
                    <span>{project.title}</span>
                    <button type="button" onClick={() => act(() => api.detachNoteFromProject(project.id, noteId))}>Remove</button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="nw-info-empty">Not in a project.</p>
            )}
            <select
              value=""
              aria-label="Add to project"
              onChange={(event) => event.target.value && act(() => api.attachNoteToProject(event.target.value, noteId))}
            >
              <option value="">Add to project…</option>
              {allProjects
                .filter((project) => !projects?.some((linked) => linked.id === project.id))
                .map((project) => (
                  <option key={project.id} value={project.id}>{project.title}</option>
                ))}
            </select>
          </section>

          <section className="nw-info-section">
            <h3>Tasks</h3>
            {tasks?.length ? (
              <ul>
                {tasks.map((task) => (
                  <li key={task.id}>
                    <button type="button" className="nw-info-link" onClick={() => onOpenTask?.(task.id)}>{task.title}</button>
                    <button type="button" onClick={() => act(() => api.detachNoteFromTask(task.id, noteId))}>Remove</button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="nw-info-empty">Not linked to a task.</p>
            )}
            <select
              value=""
              aria-label="Link to task"
              onChange={(event) => event.target.value && act(() => api.attachNoteToTask(event.target.value, noteId))}
            >
              <option value="">Link to task…</option>
              {allTasks
                .filter((task) => !tasks?.some((linked) => linked.id === task.id))
                .map((task) => (
                  <option key={task.id} value={task.id}>{task.title}</option>
                ))}
            </select>
          </section>

          <div className="nw-info-section nw-info-files">
            <FileAttachments linkType="note" targetId={noteId} onOpenFile={onOpenFile} />
          </div>

          <section className="nw-info-section">
            <h3>Versions</h3>
            {versions?.length ? (
              <ul>
                {versions.map((version) => (
                  <li key={version.id}>
                    <span>
                      {VERSION_LABELS[version.kind] ?? "Earlier version"}
                      <small>{longDate(version.created_at)}</small>
                    </span>
                    {editable && (
                      <button type="button" onClick={() => controller.restoreVersion(version.id)}>Restore</button>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="nw-info-empty">No earlier versions.</p>
            )}
          </section>
        </>
      )}
      {error && <p className="nw-info-error" role="alert">{error}</p>}
    </aside>
  );
}

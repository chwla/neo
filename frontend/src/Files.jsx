import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import ArtifactsPanel from "./ArtifactsPanel.jsx";
import PatchApplications from "./PatchApplications.jsx";
import Icon from "./WorkspaceIcon.jsx";

function size(value) {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function shortTime(iso) {
  if (!iso) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  return value.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function Files({ onBack, initialFileId = null }) {
  const [files, setFiles] = useState([]);
  const [selected, setSelected] = useState(null);
  const [links, setLinks] = useState([]);
  const [query, setQuery] = useState("");
  const [extension, setExtension] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [projects, setProjects] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [patchObjective, setPatchObjective] = useState("");
  const [patchProjectId, setPatchProjectId] = useState("");
  const [patchTaskId, setPatchTaskId] = useState("");
  const [artifactRefresh, setArtifactRefresh] = useState(0);
  const [applicationRefresh, setApplicationRefresh] = useState(0);
  const [tab, setTab] = useState("preview");
  const input = useRef(null);

  const load = useCallback(async () => {
    const data = await api.filesList({ q: query, extension });
    setFiles(data.files || []);
  }, [query, extension]);

  async function open(fileId) {
    const data = await api.file(fileId);
    setSelected(data.file); setLinks(data.links || []); setError(""); setNotice("");
  }

  useEffect(() => { load().catch((err) => setError(err.message)); }, [load]);
  useEffect(() => {
    Promise.all([api.projectsList({ limit: 100 }), api.tasksList({ limit: 100 })])
      .then(([projectData, taskData]) => {
        setProjects(projectData.projects || []); setTasks(taskData.tasks || []);
      }).catch(() => {});
  }, []);
  useEffect(() => { if (initialFileId) open(initialFileId).catch((err) => setError(err.message)); }, [initialFileId]);

  async function upload(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true); setError("");
    try { const data = await api.uploadFile(file); await load(); await open(data.file.id); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); event.target.value = ""; }
  }

  async function summarize() {
    setBusy(true);
    try { await api.summarizeFile(selected.id); await open(selected.id); await load(); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  async function remove() {
    if (!window.confirm(`Delete ${selected.display_name}? The original upload will remain stored safely.`)) return;
    await api.deleteFile(selected.id); setSelected(null); setLinks([]); await load();
  }

  async function proposePatch() {
    if (!selected || !patchObjective.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const data = await api.proposePatch({
        objective: patchObjective.trim(), file_ids: [selected.id],
        project_id: patchProjectId || null, task_id: patchTaskId || null,
      });
      setArtifactRefresh((value) => value + 1);
      setNotice(data.artifact.artifact_type === "patch_proposal"
        ? "Patch proposal created for review. It has not been applied."
        : "A review analysis was created because a reliable diff was not available.");
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  const extensions = [...new Set(files.map((item) => item.extension).filter(Boolean))].sort();
  const name = selected ? selected.metadata?.relative_path || selected.display_name : "";
  const TABS = [
    ["preview", "Preview", null],
    ["summary", "Summary", null],
    ["links", "Links", links.length],
    ["patch", "Patch", null],
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
            <span className="ws-rail-count">{files.length} file{files.length === 1 ? "" : "s"}</span>
          </div>
          <h1 className="ws-rail-title">Files</h1>
          <input ref={input} type="file" hidden onChange={upload} />
          <button className="ws-primary" type="button" disabled={busy} onClick={() => input.current?.click()}>
            <Icon name="upload" />
            Upload file
          </button>
        </header>

        <div className="ws-filters">
          <div className="ws-search">
            <Icon name="search" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search names and text"
              aria-label="Search files"
            />
            {query && (
              <button className="ws-search-clear" type="button" onClick={() => setQuery("")} aria-label="Clear search">
                ×
              </button>
            )}
          </div>
          <div className="ws-filter-row">
            <select value={extension} onChange={(event) => setExtension(event.target.value)} aria-label="Filter by type">
              <option value="">All file types</option>
              {extensions.map((item) => <option key={item}>{item}</option>)}
            </select>
          </div>
        </div>

        <div className="ws-list">
          {files.length === 0 ? (
            <div className="ws-list-empty">
              <p>{query || extension ? "No files match." : "No files yet."}</p>
            </div>
          ) : files.map((item) => (
            <button
              type="button"
              key={item.id}
              className={`ws-row ${selected?.id === item.id ? "active" : ""}`}
              onClick={() => open(item.id)}
            >
              <span className="ws-row-head">
                <span className="ws-row-title">{item.metadata?.relative_path || item.display_name}</span>
                <time className="ws-row-time">{shortTime(item.updated_at)}</time>
              </span>
              <span className="ws-row-meta">
                <span className="ws-badge mute">{item.extension || "file"}</span>
                <span className="ws-row-more">{size(item.size_bytes)}</span>
              </span>
            </button>
          ))}
        </div>
      </aside>

      <section className="ws-main">
        {!selected ? (
          <div className="ws-blank">
            <div className="ws-blank-mark"><Icon name="file" /></div>
            <h2>No file open</h2>
            <p>Pick a file to read its text, review its summary, or draft a review-only patch proposal.</p>
            <button className="ws-primary" type="button" onClick={() => input.current?.click()}>
              <Icon name="upload" />
              Upload file
            </button>
            {error && <div className="ws-error">{error}</div>}
          </div>
        ) : (
          <>
            <div className="ws-toolbar">
              <div className="ws-toolbar-state">
                <span className="ws-toolbar-title">{name}</span>
                <span className="ws-meta">{selected.mime_type || "Unknown type"}</span>
                <span className="ws-meta">{size(selected.size_bytes)}</span>
              </div>
              <div className="ws-toolbar-actions">
                <a className="ws-action" href={api.fileDownloadUrl(selected.id)}>
                  <Icon name="download" />
                  Download
                </a>
                <button className="ws-action danger" type="button" onClick={remove}>
                  <Icon name="trash" />
                  Delete
                </button>
              </div>
            </div>

            <nav className="ws-tabs" aria-label="File sections">
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

            <div className="ws-stage">
              <div className="ws-doc wide">
                {tab === "preview" && (
                  <section className="ws-section">
                    {selected.extracted_text
                      ? <pre className="ws-pre">{selected.extracted_text}</pre>
                      : <p className="ws-empty-line">Preview is not supported for this file type.</p>}
                  </section>
                )}

                {tab === "summary" && (
                  <section className="ws-section">
                    <div className="ws-section-head">
                      <h3 className="ws-section-title">Summary</h3>
                      <button
                        className="ws-action"
                        type="button"
                        disabled={busy || !selected.extracted_text}
                        onClick={summarize}
                      >
                        <Icon name="sparkle" />
                        {selected.summary ? "Regenerate" : "Summarize"}
                      </button>
                    </div>
                    {selected.summary
                      ? <p className="ws-help">{selected.summary}</p>
                      : <p className="ws-empty-line">No summary yet.</p>}

                    {selected.metadata?.source === "local_repo" && (
                      <>
                        <div className="ws-section-head">
                          <h3 className="ws-section-title">Repository source</h3>
                        </div>
                        <div className="ws-tiles">
                          <div className="ws-tile"><strong>Repo</strong><span>{selected.metadata.repo_name || selected.metadata.repo_id}</span></div>
                          <div className="ws-tile"><strong>Path</strong><span>{selected.metadata.relative_path}</span></div>
                          <div className="ws-tile"><strong>Original</strong><span>{selected.metadata.original_path}</span></div>
                          <div className="ws-tile"><strong>SHA-256</strong><span>{selected.sha256}</span></div>
                        </div>
                      </>
                    )}
                  </section>
                )}

                {tab === "links" && (
                  <section className="ws-section">
                    <div className="ws-section-head">
                      <h3 className="ws-section-title">Linked to</h3>
                    </div>
                    {links.length ? (
                      <div className="ws-tiles">
                        {links.map((link) => (
                          <div className="ws-tile" key={link.id}>
                            <strong>{link.title || link.target_id}</strong>
                            <span className="ws-badge mute">{link.link_type}</span>
                          </div>
                        ))}
                      </div>
                    ) : <p className="ws-empty-line">Not attached to a project, task or note yet.</p>}
                  </section>
                )}

                {tab === "patch" && (
                  <>
                    <section className="ws-section">
                      <div className="ws-section-head">
                        <h3 className="ws-section-title">Create patch proposal</h3>
                      </div>
                      <p className="ws-help">
                        Creates a review-only unified diff artifact. It will not modify this file.
                      </p>
                      <textarea
                        className="ws-textarea"
                        value={patchObjective}
                        onChange={(event) => setPatchObjective(event.target.value)}
                        placeholder="Describe the proposed change"
                        aria-label="Patch objective"
                      />
                      <div className="ws-field-row">
                        <label className="ws-field">
                          <span>Project</span>
                          <select value={patchProjectId} onChange={(event) => setPatchProjectId(event.target.value)}>
                            <option value="">No project</option>
                            {projects.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
                          </select>
                        </label>
                        <label className="ws-field">
                          <span>Task</span>
                          <select value={patchTaskId} onChange={(event) => setPatchTaskId(event.target.value)}>
                            <option value="">No task</option>
                            {tasks.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
                          </select>
                        </label>
                      </div>
                      <button
                        className="ws-primary"
                        type="button"
                        disabled={busy || !patchObjective.trim() || !selected.extracted_text}
                        onClick={proposePatch}
                      >
                        Create patch proposal
                      </button>
                      {notice && <div className="ws-notice">{notice}</div>}
                    </section>

                    <ArtifactsPanel
                      taskId={patchTaskId || null}
                      projectId={patchProjectId || null}
                      refreshKey={artifactRefresh}
                      showAll
                      onApplied={async () => {
                        await open(selected.id); await load(); setApplicationRefresh((value) => value + 1);
                      }}
                    />
                    <PatchApplications
                      fileId={selected.id}
                      repoId={selected.metadata?.repo_id || null}
                      refreshKey={applicationRefresh}
                    />
                  </>
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

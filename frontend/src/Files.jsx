import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import ArtifactsPanel from "./ArtifactsPanel.jsx";
import PatchApplications from "./PatchApplications.jsx";

function size(value) {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}

export default function Files({ onBack, initialFileId = null }) {
  const [files, setFiles] = useState([]);
  const [selected, setSelected] = useState(null);
  const [links, setLinks] = useState([]);
  const [query, setQuery] = useState("");
  const [extension, setExtension] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [projects, setProjects] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [patchObjective, setPatchObjective] = useState("");
  const [patchProjectId, setPatchProjectId] = useState("");
  const [patchTaskId, setPatchTaskId] = useState("");
  const [artifactRefresh, setArtifactRefresh] = useState(0);
  const [applicationRefresh, setApplicationRefresh] = useState(0);
  const input = useRef(null);

  const load = useCallback(async () => {
    const data = await api.filesList({ q: query, extension });
    setFiles(data.files || []);
  }, [query, extension]);

  async function open(fileId) {
    const data = await api.file(fileId);
    setSelected(data.file); setLinks(data.links || []); setError("");
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
    setBusy(true); setError("");
    try {
      const data = await api.proposePatch({
        objective: patchObjective.trim(), file_ids: [selected.id],
        project_id: patchProjectId || null, task_id: patchTaskId || null,
      });
      setArtifactRefresh((value) => value + 1);
      setError(data.artifact.artifact_type === "patch_proposal"
        ? "Patch proposal created for review. It has not been applied."
        : "A review analysis was created because a reliable diff was not available.");
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  const extensions = [...new Set(files.map((item) => item.extension).filter(Boolean))].sort();
  return (
    <main className="files-layout">
      <section className="files-list-pane">
        <div className="files-header"><button type="button" className="neo-button secondary" onClick={onBack}>Back</button><h2>Files</h2></div>
        <input ref={input} type="file" hidden onChange={upload} />
        <button type="button" className="neo-button" disabled={busy} onClick={() => input.current?.click()}>Upload File</button>
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search names and text" />
        <select value={extension} onChange={(event) => setExtension(event.target.value)}><option value="">All file types</option>{extensions.map((item) => <option key={item}>{item}</option>)}</select>
        <div className="files-list">{files.map((item) => <button type="button" key={item.id} className={selected?.id === item.id ? "selected" : ""} onClick={() => open(item.id)}>
          <strong>{item.metadata?.relative_path || item.display_name}</strong><span>{item.extension || "file"} · {size(item.size_bytes)}</span><small>{new Date(item.updated_at).toLocaleString()}</small>
        </button>)}</div>
      </section>
      <section className="files-detail-pane">
        {!selected ? <div className="files-empty">Select a file to preview it.</div> : <>
          <div className="files-detail-header"><div><h2>{selected.metadata?.relative_path || selected.display_name}</h2><p>{selected.mime_type || "Unknown type"} · {size(selected.size_bytes)}</p></div>
            <div><a className="neo-button secondary" href={api.fileDownloadUrl(selected.id)}>Download</a><button type="button" className="neo-button danger" onClick={remove}>Delete</button></div></div>
          <section><h3>Summary</h3>{selected.summary ? <p>{selected.summary}</p> : <p>No summary yet.</p>}<button type="button" disabled={busy || !selected.extracted_text} onClick={summarize}>Summarize</button></section>
          <section><h3>Links</h3>{links.length ? links.map((link) => <span className="file-link-badge" key={link.id}>{link.link_type}: {link.title || link.target_id}</span>) : <p>Not attached yet.</p>}</section>
          {selected.metadata?.source === "local_repo" && <section><h3>Repository source</h3><p>Repo: {selected.metadata.repo_name || selected.metadata.repo_id}</p><p>Path: {selected.metadata.relative_path}</p><p>Original repo: {selected.metadata.original_path}</p><p>Current SHA-256: <code>{selected.sha256}</code></p></section>}
          <section className="patch-proposal-form"><h3>Create Patch Proposal</h3>
            <p>Creates a review-only unified diff artifact. It will not modify this file.</p>
            <textarea value={patchObjective} onChange={(event) => setPatchObjective(event.target.value)} placeholder="Describe the proposed change" rows={3} />
            <div><select value={patchProjectId} onChange={(event) => setPatchProjectId(event.target.value)}><option value="">No project</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select>
              <select value={patchTaskId} onChange={(event) => setPatchTaskId(event.target.value)}><option value="">No task</option>{tasks.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select>
              <button type="button" disabled={busy || !patchObjective.trim() || !selected.extracted_text} onClick={proposePatch}>Create Patch Proposal</button></div>
          </section>
          <ArtifactsPanel taskId={patchTaskId || null} projectId={patchProjectId || null} refreshKey={artifactRefresh} showAll onApplied={async () => {
            await open(selected.id); await load(); setApplicationRefresh((value) => value + 1);
          }} />
          <PatchApplications fileId={selected.id} repoId={selected.metadata?.repo_id || null} refreshKey={applicationRefresh} />
          <section className="file-preview"><h3>Preview</h3>{selected.extracted_text ? <pre>{selected.extracted_text}</pre> : <p>Preview not supported.</p>}</section>
        </>}
        {error ? <div className="task-error">{error}</div> : null}
      </section>
    </main>
  );
}

import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import CodebaseIndex from "./CodebaseIndex.jsx";
import GitCheckpoints from "./GitCheckpoints.jsx";
import TestRunner from "./TestRunner.jsx";

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function Repos({ onBack, onOpenFile, projectId = null, compact = false }) {
  const [repos, setRepos] = useState([]);
  const [selected, setSelected] = useState(null);
  const [files, setFiles] = useState([]);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [query, setQuery] = useState("");
  const [extension, setExtension] = useState("");
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [projects, setProjects] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");

  const loadRepos = useCallback(async () => {
    const data = await api.reposList({ projectId });
    setRepos(data.repos || []);
  }, [projectId]);

  const loadFiles = useCallback(async (repoId) => {
    if (!repoId) return;
    const data = await api.repoFiles(repoId, { q: query, extension, language });
    setFiles(data.files || []);
  }, [extension, language, query]);

  useEffect(() => { loadRepos().catch((error) => setMessage(error.message)); }, [loadRepos]);
  useEffect(() => {
    if (!compact) api.projectsList({ limit: 100 }).then((data) => setProjects(data.projects || [])).catch(() => {});
  }, [compact]);
  useEffect(() => {
    if (selected?.id) loadFiles(selected.id).catch((error) => setMessage(error.message));
  }, [loadFiles, selected?.id]);

  async function register(event) {
    event.preventDefault();
    if (!path.trim() || !confirm) return;
    setBusy(true); setMessage("");
    try {
      const data = await api.registerRepo({
        path: path.trim(), name: name.trim() || null,
        project_id: projectId || selectedProjectId || null, confirm: true,
      });
      setPath(""); setName(""); setConfirm(false); setSelected(data.repo);
      await loadRepos(); await loadFiles(data.repo.id);
      setMessage("Repository copied into Neo's managed workspace. The original was not changed.");
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  async function remove() {
    if (!selected || !window.confirm(`Remove ${selected.name} from Neo's repo index?`)) return;
    await api.deleteRepo(selected.id); setSelected(null); setFiles([]); await loadRepos();
  }

  const extensions = [...new Set(files.map((item) => item.relative_path.split(".").pop()).filter(Boolean))].sort();
  const languages = [...new Set(files.map((item) => item.language).filter(Boolean))].sort();
  const content = <>
    <section className="repos-register">
      <div className="repos-title-row">{!compact && <button type="button" className="neo-button secondary" onClick={onBack}>Back</button>}<h2>{compact ? "Repositories" : "Repo Workspace"}</h2></div>
      <form onSubmit={register}>
        <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="Absolute path to a project folder" />
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Display name (optional)" />
        {!compact && <select value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}><option value="">No project</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}</select>}
        <label><input type="checkbox" checked={confirm} onChange={(event) => setConfirm(event.target.checked)} /> Copy supported text files into Neo-managed storage. The original folder stays untouched.</label>
        <button type="submit" disabled={busy || !path.trim() || !confirm}>Register Repository</button>
      </form>
      {message && <p className="repos-message">{message}</p>}
    </section>
    <section className="repos-browser">
      <div className="repos-list">
        <h3>Registered repositories</h3>
        {repos.length === 0 && <p>No repositories registered.</p>}
        {repos.map((repo) => <button type="button" className={selected?.id === repo.id ? "selected" : ""} key={repo.id} onClick={() => setSelected(repo)}>
          <strong>{repo.name}</strong><span>{repo.indexed_file_count} files · {formatBytes(repo.total_bytes)}</span><small>{repo.status}</small>
        </button>)}
      </div>
      <div className="repos-files">
        {!selected ? <p>Select a repository to browse its managed copy.</p> : <>
          <div className="repos-detail-header"><div><h3>{selected.name}</h3><p>{selected.indexed_file_count} indexed · {formatBytes(selected.total_bytes)} · {selected.metadata.ignored_files || 0} ignored files · {selected.metadata.ignored_dirs || 0} ignored folders · indexed {selected.indexed_at ? new Date(selected.indexed_at).toLocaleString() : "pending"}</p></div><button type="button" className="neo-button danger" onClick={remove}>Remove</button></div>
          <div className="repos-filters"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search paths and content" /><select value={extension} onChange={(event) => setExtension(event.target.value)}><option value="">All extensions</option>{extensions.map((item) => <option key={item}>{item}</option>)}</select><select value={language} onChange={(event) => setLanguage(event.target.value)}><option value="">All languages</option>{languages.map((item) => <option key={item}>{item}</option>)}</select></div>
          <div className="repo-file-list">{files.map((item) => <button type="button" key={item.id} onClick={() => onOpenFile?.(item.file_id)}><strong>{item.relative_path}</strong><span>{item.language || "Text"} · {formatBytes(item.size_bytes)}</span></button>)}</div>
          <CodebaseIndex repo={selected} repoFiles={files} onOpenFile={onOpenFile} compact={compact} />
          <TestRunner repo={selected} compact={compact} />
          <GitCheckpoints repo={selected} compact={compact} />
        </>}
      </div>
    </section>
  </>;
  return compact ? <div className="project-repos">{content}</div> : <main className="repos-layout">{content}</main>;
}

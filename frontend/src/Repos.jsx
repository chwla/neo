import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import CodebaseIndex from "./CodebaseIndex.jsx";
import GitCheckpoints from "./GitCheckpoints.jsx";
import TestRunner from "./TestRunner.jsx";
import Icon from "./WorkspaceIcon.jsx";

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
  const [registering, setRegistering] = useState(false);
  const [tab, setTab] = useState("files");

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
      setPath(""); setName(""); setConfirm(false); setSelected(data.repo); setRegistering(false);
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

  const registerForm = (
    <form className="ws-register" onSubmit={register}>
      <label className="ws-field">
        <span>Folder path</span>
        <input
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="/absolute/path/to/project"
        />
      </label>
      <label className="ws-field">
        <span>Display name</span>
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Optional" />
      </label>
      {!compact && (
        <label className="ws-field">
          <span>Project</span>
          <select value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}>
            <option value="">No project</option>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select>
        </label>
      )}
      <label className="ws-consent">
        <input type="checkbox" checked={confirm} onChange={(event) => setConfirm(event.target.checked)} />
        <span>Copy supported text files into Neo-managed storage. The original folder stays untouched.</span>
      </label>
      <button className="ws-primary" type="submit" disabled={busy || !path.trim() || !confirm}>
        {busy ? "Registering…" : "Register repository"}
      </button>
    </form>
  );

  const detail = !selected ? (
    <div className="ws-blank">
      <div className="ws-blank-mark"><Icon name="repo" /></div>
      <h2>No repository selected</h2>
      <p>Register a folder to copy its text files into Neo's managed workspace, then browse, index and test it here.</p>
    </div>
  ) : (
    <>
      <div className="ws-toolbar">
        <div className="ws-toolbar-state">
          <span className="ws-toolbar-title">{selected.name}</span>
          <span className="ws-meta">{selected.indexed_file_count} indexed</span>
          <span className="ws-meta">{formatBytes(selected.total_bytes)}</span>
          <span className="ws-badge mute">{selected.status}</span>
        </div>
        <div className="ws-toolbar-actions">
          <button className="ws-action danger" type="button" onClick={remove}>
            <Icon name="trash" />
            Remove
          </button>
        </div>
      </div>

      <nav className="ws-tabs" aria-label="Repository sections">
        {[["files", "Files", files.length], ["index", "Index", null], ["tests", "Tests", null], ["git", "Git", null]].map(
          ([key, label, count]) => (
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
          ),
        )}
      </nav>

      <div className="ws-stage">
        <div className="ws-doc wide">
          {tab === "files" && (
            <section className="ws-section">
              <div className="ws-filter-row ws-repo-filters">
                <div className="ws-search">
                  <Icon name="search" />
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Search paths and content"
                    aria-label="Search repository files"
                  />
                </div>
                <select value={extension} onChange={(event) => setExtension(event.target.value)} aria-label="Extension">
                  <option value="">All extensions</option>
                  {extensions.map((item) => <option key={item}>{item}</option>)}
                </select>
                <select value={language} onChange={(event) => setLanguage(event.target.value)} aria-label="Language">
                  <option value="">All languages</option>
                  {languages.map((item) => <option key={item}>{item}</option>)}
                </select>
              </div>
              {files.length === 0 ? (
                <p className="ws-empty-line">No files match.</p>
              ) : (
                <div className="ws-tiles">
                  {files.map((item) => (
                    <button type="button" key={item.id} className="ws-tile" onClick={() => onOpenFile?.(item.file_id)}>
                      <strong>{item.relative_path}</strong>
                      <span className="ws-badge mute">{item.language || "text"}</span>
                      <span>{formatBytes(item.size_bytes)}</span>
                    </button>
                  ))}
                </div>
              )}
              <p className="ws-help">
                {selected.metadata.ignored_files || 0} ignored files · {selected.metadata.ignored_dirs || 0} ignored
                folders · indexed {selected.indexed_at ? new Date(selected.indexed_at).toLocaleString() : "pending"}
              </p>
            </section>
          )}
          {tab === "index" && <CodebaseIndex repo={selected} repoFiles={files} onOpenFile={onOpenFile} compact={compact} />}
          {tab === "tests" && <TestRunner repo={selected} compact={compact} />}
          {tab === "git" && <GitCheckpoints repo={selected} compact={compact} />}
        </div>
      </div>
    </>
  );

  // Inside a project the page chrome belongs to the project, so render a flat section.
  if (compact) {
    return (
      <div className="ws-embedded">
        <section className="ws-section">
          <div className="ws-section-head">
            <h3 className="ws-section-title">Repositories</h3>
            <button className="ws-action" type="button" onClick={() => setRegistering((value) => !value)}>
              <Icon name="plus" />
              {registering ? "Cancel" : "Register"}
            </button>
          </div>
          {registering && registerForm}
          {repos.length === 0 ? (
            <p className="ws-empty-line">No repositories registered.</p>
          ) : (
            <div className="ws-tiles">
              {repos.map((repo) => (
                <button
                  type="button"
                  key={repo.id}
                  className="ws-tile"
                  onClick={() => setSelected(selected?.id === repo.id ? null : repo)}
                >
                  <strong>{repo.name}</strong>
                  <span>{repo.indexed_file_count} files</span>
                  <span>{formatBytes(repo.total_bytes)}</span>
                </button>
              ))}
            </div>
          )}
          {message && <div className="ws-notice">{message}</div>}
        </section>
        {selected && (
          <>
            <CodebaseIndex repo={selected} repoFiles={files} onOpenFile={onOpenFile} compact />
            <TestRunner repo={selected} compact />
            <GitCheckpoints repo={selected} compact />
          </>
        )}
      </div>
    );
  }

  return (
    <div className="ws">
      <aside className="ws-rail">
        <header className="ws-rail-head">
          <div className="ws-rail-top">
            <button className="ws-back" type="button" onClick={onBack}>
              <Icon name="back" />
              Chat
            </button>
            <span className="ws-rail-count">{repos.length} repo{repos.length === 1 ? "" : "s"}</span>
          </div>
          <h1 className="ws-rail-title">Repositories</h1>
          <button className="ws-primary" type="button" onClick={() => setRegistering((value) => !value)}>
            <Icon name="plus" />
            {registering ? "Cancel" : "Register repository"}
          </button>
        </header>

        {registering && <div className="ws-filters">{registerForm}</div>}

        <div className="ws-list">
          {repos.length === 0 ? (
            <div className="ws-list-empty">
              <p>No repositories registered.</p>
            </div>
          ) : repos.map((repo) => (
            <button
              type="button"
              key={repo.id}
              className={`ws-row ${selected?.id === repo.id ? "active" : ""}`}
              onClick={() => setSelected(repo)}
            >
              <span className="ws-row-head">
                <span className="ws-row-title">{repo.name}</span>
              </span>
              <span className="ws-row-meta">
                <span className="ws-badge mute">{repo.status}</span>
                <span className="ws-row-more">{repo.indexed_file_count} files · {formatBytes(repo.total_bytes)}</span>
              </span>
            </button>
          ))}
          {message && <div className="ws-notice">{message}</div>}
        </div>
      </aside>

      <section className="ws-main">{detail}</section>
    </div>
  );
}

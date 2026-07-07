import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

const WARNING = "Git operations affect only Neo’s managed workspace copy.\n\nThe original repository is never modified. No remote Git operations are supported.";

function shortSha(value) { return value ? value.slice(0, 12) : "—"; }

export default function GitCheckpoints({ repo, compact = false }) {
  const [status, setStatus] = useState(null);
  const [checkpoints, setCheckpoints] = useState([]);
  const [diff, setDiff] = useState("");
  const [selected, setSelected] = useState(null);
  const [title, setTitle] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    if (!repo?.id) return;
    const [statusData, checkpointData] = await Promise.all([
      api.gitStatus(repo.id), api.gitCheckpoints(repo.id),
    ]);
    setStatus(statusData); setCheckpoints(checkpointData.checkpoints || []);
  }, [repo?.id]);
  useEffect(() => { load().catch((error) => setNotice(error.message)); }, [load]);

  async function initialize() {
    if (!window.confirm(`${WARNING}\n\nInitialize local checkpointing and create the initial checkpoint?`)) return;
    setBusy(true); setNotice("");
    try { await api.initGit(repo.id); await load(); setNotice("Initial managed-workspace checkpoint created."); }
    catch (error) { setNotice(error.message); } finally { setBusy(false); }
  }
  async function viewDiff(path = "") {
    setBusy(true); setNotice("");
    try { const data = await api.gitDiff(repo.id, path); setDiff(data.diff || "No unstaged diff."); }
    catch (error) { setNotice(error.message); } finally { setBusy(false); }
  }
  async function create(event) {
    event.preventDefault();
    if (!window.confirm(`${WARNING}\n\nCreate checkpoint “${title}”?`)) return;
    setBusy(true); setNotice("");
    try {
      await api.createGitCheckpoint(repo.id, { title: title.trim(), message: message.trim() || null });
      setTitle(""); setMessage(""); setDiff(""); await load(); setNotice("Checkpoint created.");
    } catch (error) { setNotice(error.message); } finally { setBusy(false); }
  }
  async function open(checkpointId) {
    try { const data = await api.gitCheckpoint(checkpointId); setSelected(data); }
    catch (error) { setNotice(error.message); }
  }
  async function restore(item) {
    if (!window.confirm(`${WARNING}\n\nRestore managed workspace files from “${item.title}”? Current uncommitted changes will be replaced. This does not affect the original repository.`)) return;
    setBusy(true); setNotice("");
    try { await api.restoreGitCheckpoint(item.id); setSelected(null); setDiff(""); await load(); setNotice("Managed workspace restored. Code intelligence is marked stale until rebuilt."); }
    catch (error) { setNotice(error.message); } finally { setBusy(false); }
  }

  return <section className={`git-checkpoints ${compact ? "compact" : ""}`}>
    <div className="git-title"><div><h3>Git / Checkpoints</h3><p>Local checkpoints for Neo’s managed copy only. No remotes.</p></div>{status?.initialized ? <button type="button" disabled={busy} onClick={() => load()}>Refresh status</button> : <button type="button" disabled={busy || status?.available === false} onClick={initialize}>Init Git</button>}</div>
    {status?.available === false && <div className="task-error">{status.error}</div>}
    {status?.initialized && <><div className="git-status"><span><strong>{status.clean ? "Clean" : "Changed"}</strong><small>HEAD {shortSha(status.head)} · {status.default_branch}</small></span>{!status.clean && <button type="button" disabled={busy} onClick={() => viewDiff()}>View full diff</button>}</div>
      {status.changed_files?.length > 0 && <div className="git-changed-files">{status.changed_files.map((item) => <button type="button" key={`${item.status}-${item.path}`} onClick={() => viewDiff(item.path)}><span>{item.path}</span><small>{item.status}{item.staged ? " · staged" : ""}</small></button>)}</div>}
      {!compact && <form className="git-checkpoint-form" onSubmit={create}><input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Checkpoint title" required /><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Checkpoint message (optional)" rows={2} /><button type="submit" disabled={busy || !title.trim() || status.clean}>Create checkpoint</button></form>}
    </>}
    {diff && <div className="git-diff"><div><strong>Workspace diff</strong><button type="button" onClick={() => setDiff("")}>Close</button></div><pre>{diff}</pre></div>}
    <div className="git-history"><h4>Checkpoint history</h4>{checkpoints.length ? checkpoints.map((item) => <button type="button" key={item.id} onClick={() => open(item.id)}><strong>{item.title}</strong><span className={`git-checkpoint-status ${item.status}`}>{item.status}</span><small>{shortSha(item.commit_sha)} · {item.changed_files.length} files · {new Date(item.created_at).toLocaleString()}</small></button>) : <p>No checkpoints yet.</p>}</div>
    {selected && <div className="git-checkpoint-detail"><div><strong>{selected.checkpoint.title}</strong><button type="button" onClick={() => setSelected(null)}>Close</button></div><p>Commit: <code>{selected.checkpoint.commit_sha}</code><br />Status: {selected.checkpoint.status}<br />{selected.checkpoint.message || "No message."}</p><ul>{selected.checkpoint.changed_files.map((item) => <li key={item.path}>{item.path}</li>)}</ul><pre>{selected.checkpoint.stats?.summary || "No stats."}</pre><button type="button" className="neo-button danger" disabled={busy} onClick={() => restore(selected.checkpoint)}>Restore managed copy</button></div>}
    {notice && <div className={notice.includes("created") || notice.includes("restored") ? "repos-message" : "task-error"}>{notice}</div>}
  </section>;
}

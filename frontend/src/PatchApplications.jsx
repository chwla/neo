import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

function shortHash(value) { return value ? value.slice(0, 12) : "—"; }

export default function PatchApplications({ fileId = null, taskId = null, projectId = null, agentRunId = null, artifactId = null, refreshKey = 0 }) {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!fileId && !taskId && !projectId && !agentRunId && !artifactId) return;
    const data = await api.patchApplications({ fileId, taskId, projectId, agentRunId, artifactId });
    const enriched = await Promise.all((data.applications || []).map(async (application) => {
      try { const artifact = await api.artifact(application.artifact_id); return { ...application, artifact_title: artifact.artifact.title }; }
      catch { return { ...application, artifact_title: "Patch application" }; }
    }));
    setItems(enriched);
  }, [fileId, taskId, projectId, agentRunId, artifactId, refreshKey]);

  useEffect(() => { load().catch((err) => setError(err.message)); }, [load]);

  async function open(applicationId) {
    try { const data = await api.patchApplication(applicationId); setSelected(data.application); }
    catch (err) { setError(err.message); }
  }

  return <section className="patch-applications">
    <div className="file-attachments-title">Patch Applications</div>
    {items.length ? <div className="patch-application-list">{items.map((item) => <button type="button" key={item.id} onClick={() => open(item.id)}>
      <strong>{item.artifact_title}</strong><span className={`patch-application-status ${item.status}`}>{item.status}</span>
      <small>{new Date(item.applied_at || item.created_at).toLocaleString()} · {shortHash(item.original_sha256)} → {shortHash(item.new_sha256)}</small>
    </button>)}</div> : <p className="task-help">No patch applications yet.</p>}
    {selected ? <div className="patch-application-detail">
      <div><strong>Application snapshot</strong><button type="button" onClick={() => setSelected(null)}>Close</button></div>
      <p>Status: {selected.status}<br />Original: {selected.original_sha256}<br />Current: {selected.new_sha256 || "—"}</p>
      <div className="patch-snapshot-downloads"><a href={api.patchApplicationDownloadUrl(selected.id, "original")}>Download original</a>{selected.new_content ? <a href={api.patchApplicationDownloadUrl(selected.id, "current")}>Download current</a> : null}</div>
      {selected.error ? <div className="task-error">{selected.error}</div> : null}
      <div className="patch-snapshot-grid"><label>Before<pre>{selected.original_content}</pre></label><label>After<pre>{selected.new_content || "Not applied"}</pre></label></div>
    </div> : null}
    {error ? <div className="task-error">{error}</div> : null}
  </section>;
}

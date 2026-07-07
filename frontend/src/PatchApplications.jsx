import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

function shortHash(value) { return value ? value.slice(0, 12) : "—"; }

const TEST_WARNING = "Run this saved test command inside Neo’s managed workspace copy? It will not run in the original repository, use a shell, Git, installs, or destructive commands.";

export default function PatchApplications({ fileId = null, taskId = null, projectId = null, agentRunId = null, artifactId = null, repoId = null, refreshKey = 0 }) {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const [testCommands, setTestCommands] = useState([]);
  const [testCommandId, setTestCommandId] = useState("");
  const [testRun, setTestRun] = useState(null);
  const [testBusy, setTestBusy] = useState(false);
  const [gitBusy, setGitBusy] = useState(false);

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
  useEffect(() => {
    if (!repoId) return;
    api.testCommands(repoId).then((data) => {
      const enabled = (data.commands || []).filter((item) => item.enabled);
      setTestCommands(enabled); setTestCommandId((current) => current || enabled[0]?.id || "");
    }).catch((err) => setError(err.message));
  }, [repoId]);

  async function open(applicationId) {
    try {
      const data = await api.patchApplication(applicationId); setSelected(data.application);
      const runData = await api.testRuns({ patchApplicationId: applicationId, limit: 1 });
      setTestRun(runData.runs?.[0] || null);
    }
    catch (err) { setError(err.message); }
  }

  async function runTests() {
    if (!selected || !testCommandId || !window.confirm(TEST_WARNING)) return;
    setTestBusy(true); setError("");
    try {
      const data = await api.runTestCommand(testCommandId, {
        confirm: true, patch_application_id: selected.id,
        task_id: selected.task_id || null, agent_run_id: selected.agent_run_id || null,
      });
      setTestRun(data.run);
    } catch (err) { setError(err.message); } finally { setTestBusy(false); }
  }

  async function createCheckpoint() {
    if (!selected || !repoId || !window.confirm("Create a local Git checkpoint for this applied patch? Only Neo’s managed workspace copy is affected; no remote operation is performed.")) return;
    setGitBusy(true); setError("");
    try {
      await api.createGitCheckpoint(repoId, {
        title: `After ${selected.artifact_title || "applied patch"}`,
        message: testRun ? `Applied patch; linked test status: ${testRun.status}.` : "Applied patch checkpoint.",
        task_id: selected.task_id || null,
        agent_run_id: selected.agent_run_id || null,
        patch_application_id: selected.id,
        test_run_id: testRun?.id || null,
      });
      setError("Checkpoint created for this patch application.");
    } catch (err) { setError(err.message); } finally { setGitBusy(false); }
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
      {repoId && <div className="patch-test-runner"><strong>Run tests</strong><p>Tests are never started automatically after patch application.</p><div><select value={testCommandId} onChange={(event) => setTestCommandId(event.target.value)}><option value="">Select saved command</option>{testCommands.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><button type="button" disabled={testBusy || !testCommandId} onClick={runTests}>{testBusy ? "Running…" : "Run tests"}</button></div>{testRun && <p>Latest attached run: <span className={`test-status ${testRun.status}`}>{testRun.status}</span> · exit {testRun.exit_code ?? "—"}<br /><code>{JSON.stringify(testRun.command)}</code></p>}</div>}
      {repoId && selected.status === "applied" && <div className="patch-git-checkpoint"><strong>Git checkpoint</strong><p>No checkpoint is created automatically.</p><button type="button" disabled={gitBusy} onClick={createCheckpoint}>{gitBusy ? "Creating…" : "Create checkpoint"}</button></div>}
      {selected.error ? <div className="task-error">{selected.error}</div> : null}
      <div className="patch-snapshot-grid"><label>Before<pre>{selected.original_content}</pre></label><label>After<pre>{selected.new_content || "Not applied"}</pre></label></div>
    </div> : null}
    {error ? <div className={error.includes("created") ? "repos-message" : "task-error"}>{error}</div> : null}
  </section>;
}

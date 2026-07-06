import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import PatchApplications from "./PatchApplications.jsx";

export default function ArtifactsPanel({ taskId = null, projectId = null, agentRunId = null, refreshKey = 0, showAll = false, onApplied = null }) {
  const [artifacts, setArtifacts] = useState([]);
  const [selected, setSelected] = useState(null);
  const [message, setMessage] = useState("");
  const [validation, setValidation] = useState(null);
  const [applyBusy, setApplyBusy] = useState(false);
  const [applicationRefresh, setApplicationRefresh] = useState(0);

  const load = useCallback(async () => {
    if (!taskId && !projectId && !agentRunId && !showAll) return;
    const data = await api.artifactsList({ taskId, projectId, agentRunId });
    setArtifacts(data.artifacts || []);
  }, [taskId, projectId, agentRunId, refreshKey, showAll]);

  useEffect(() => { load().catch((err) => setMessage(err.message)); }, [load]);

  async function open(id) {
    const data = await api.artifact(id);
    setSelected(data.artifact); setValidation(null); setMessage("");
  }

  async function copy() {
    await navigator.clipboard.writeText(selected.content);
    setMessage("Artifact copied.");
  }

  async function validatePatch() {
    setApplyBusy(true); setMessage("");
    try {
      const result = await api.validatePatchApply(selected.id);
      setValidation(result);
      setMessage(result.valid ? "Patch validation passed. Review hashes before applying." : result.errors.join(" "));
    } catch (err) { setMessage(err.message); setValidation(null); }
    finally { setApplyBusy(false); }
  }

  async function applyPatch() {
    if (!validation?.valid || !window.confirm("Apply this reviewed patch to Neo's workspace copy? This does not modify your local project or run tests.")) return;
    setApplyBusy(true); setMessage("");
    try {
      const result = await api.applyPatch(selected.id);
      setValidation(null);
      setMessage("Patch applied to the workspace copy. No code or tests were run.");
      setApplicationRefresh((value) => value + 1);
      onApplied?.(result.file, result.application);
    } catch (err) { setMessage(err.message); }
    finally { setApplyBusy(false); }
  }

  return (
    <section className="artifacts-panel">
      <div className="file-attachments-title">Artifacts</div>
      {artifacts.length ? <div className="artifact-list">{artifacts.map((item) => (
        <button type="button" key={item.id} onClick={() => open(item.id)}>
          <strong>{item.title}</strong><span>{item.artifact_type.replaceAll("_", " ")}</span>
        </button>
      ))}</div> : <p className="task-help">No artifacts yet.</p>}
      {selected ? <div className="artifact-viewer">
        <div className="artifact-viewer-header"><div><strong>{selected.title}</strong><span>{selected.artifact_type.replaceAll("_", " ")}</span></div><div>
          <button type="button" onClick={copy}>Copy</button>
          <a className="neo-button secondary" href={api.artifactDownloadUrl(selected.id)}>Download</a>
          <button type="button" onClick={() => setSelected(null)}>Close</button>
        </div></div>
        <pre>{selected.content}</pre>
        {selected.artifact_type === "patch_proposal" ? <div className="patch-apply-controls">
          <strong>Controlled Patch Apply</strong>
          <p>This will modify the workspace copy of the file. It will not run tests or apply changes to your local project filesystem.</p>
          {validation?.target_files?.map((target) => <div className="patch-hash-review" key={target.file_id}>
            <span>Target: {target.filename}</span><span>Proposal hash: {target.proposal_sha256}</span><span>Current hash: {target.current_sha256}</span>
          </div>)}
          {validation?.errors?.length ? <div className="task-error">{validation.errors.join(" ")}</div> : null}
          <div><button type="button" onClick={validatePatch} disabled={applyBusy}>Validate Patch</button>
            <button type="button" onClick={applyPatch} disabled={applyBusy || !validation?.valid}>Apply Patch</button></div>
          <PatchApplications artifactId={selected.id} refreshKey={applicationRefresh} />
        </div> : null}
      </div> : null}
      {message ? <div className="agent-message">{message}</div> : null}
    </section>
  );
}

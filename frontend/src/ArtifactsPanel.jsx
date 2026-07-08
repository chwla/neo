import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import PatchApplications from "./PatchApplications.jsx";

function patchSections(content) {
  const fenced = String(content || "").match(/```diff\s*\n([\s\S]*?)```/i)?.[1] || "";
  return fenced.split(/(?=^diff --git )/m).filter(Boolean).map((diff) => {
    const match = diff.match(/^diff --git a\/(\S+) b\/(\S+)/m);
    return { path: match?.[2] || "unknown", changeType: diff.includes("new file mode 100644") ? "create" : "modify", diff: diff.trim() };
  });
}

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
    if (!validation?.valid || !window.confirm("Atomically apply every validated file in this reviewed patch to Neo's managed workspace copy? If any file fails, all changes are rolled back. This does not modify the original repository or run tests.")) return;
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
          <p>{selected.metadata?.schema_version === 2 ? `${selected.metadata.files?.length || 0} files will be validated and applied atomically.` : "One file will be validated and applied."} It will not run tests or modify the original repository.</p>
          {selected.metadata?.files?.length ? <div className="patch-file-list">{selected.metadata.files.map((file) => <div key={file.relative_path}><strong>{file.relative_path}</strong> <span>{file.change_type}</span></div>)}</div> : null}
          <div className="patch-file-diffs">{patchSections(selected.content).map((section) => <div key={section.path}><strong>{section.path}</strong> <span>{section.changeType}</span><pre>{section.diff}</pre></div>)}</div>
          {validation?.target_files?.map((target) => <div className="patch-hash-review" key={target.relative_path}>
            <span>{target.change_type}: {target.relative_path}</span><span>Proposal hash: {target.proposal_sha256 || "expected absent"}</span><span>Current hash: {target.current_sha256 || "absent"}</span><span>{target.valid ? "Validated" : target.errors?.join(" ")}</span>
          </div>)}
          {validation?.warnings?.map((warning) => <div className="agent-message" key={warning}>{warning}</div>)}
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

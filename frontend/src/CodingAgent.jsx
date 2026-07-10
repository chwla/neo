import { useEffect, useMemo, useState } from "react";

import { api } from "./api.js";
import RecoveryPanel from "./RecoveryPanel.jsx";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function label(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function CodingAgent({ initialTaskId = "", initialProjectId = "", compact = false }) {
  const [objective, setObjective] = useState("");
  const [projectId, setProjectId] = useState(initialProjectId || "");
  const [repoId, setRepoId] = useState("");
  const [taskId, setTaskId] = useState(initialTaskId || "");
  const [maxIterations, setMaxIterations] = useState(3);
  const [projects, setProjects] = useState([]);
  const [repos, setRepos] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [agentDefinitions, setAgentDefinitions] = useState([]);
  const [agentDefinitionId, setAgentDefinitionId] = useState("general");
  const [runs, setRuns] = useState([]);
  const [detail, setDetail] = useState(null);
  const [targetFileId, setTargetFileId] = useState("");
  const [testCommandId, setTestCommandId] = useState("");
  const [revision, setRevision] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function exportBundle() {
    if (!detail?.coding_run?.id) return;
    setBusy(true);
    try {
      const result = await api.exportBundle({ bundle_type: "coding_run", entity_id: detail.coding_run.id, include_files: true, include_patch_text: true, include_test_output: true, redact_secrets: true });
      window.location.assign(`/api/bundles/exports/${result.bundle.id}/download`);
      setMessage("Sanitized bundle created. It contains no executable import actions.");
    } catch (error) { setMessage(`Export error: ${error.message}`); } finally { setBusy(false); }
  }

  useEffect(() => { setTaskId(initialTaskId || ""); }, [initialTaskId]);
  useEffect(() => { setProjectId(initialProjectId || ""); }, [initialProjectId]);
  useEffect(() => {
    Promise.all([
      api.projectsList({ limit: 100 }),
      api.tasksList({ limit: 100 }),
      api.reposList({ limit: 100 }),
      api.agentDefinitions(false),
    ])
      .then(([projectData, taskData, repoData, agentData]) => {
        setProjects(projectData.projects || []);
        setTasks(taskData.tasks || []);
        setRepos(repoData.repos || []);
        setAgentDefinitions(agentData.definitions || []);
      })
      .catch((error) => setMessage(error.message));
  }, []);
  useEffect(() => {
    if (!initialTaskId) { setRuns([]); return; }
    api.codingRuns({ taskId: initialTaskId, limit: 20 })
      .then((data) => setRuns(data.coding_runs || []))
      .catch((error) => setMessage(error.message));
  }, [initialTaskId, detail?.coding_run?.updated_at]);

  const availableRepos = useMemo(
    () => repos.filter((repo) => !projectId || repo.project_id === projectId),
    [repos, projectId],
  );
  const availableTasks = useMemo(
    () => tasks.filter((task) => !projectId || task.project_id === projectId),
    [tasks, projectId],
  );
  const action = detail?.current_action_request;

  useEffect(() => {
    const targets = action?.payload?.target_files || [];
    setTargetFileId(targets.length === 1 ? targets[0].file_id : "");
    const commands = action?.payload?.test_commands || [];
    setTestCommandId(commands.length === 1 ? commands[0].id : "");
  }, [action?.id]);

  async function perform(work, success) {
    setBusy(true); setMessage("");
    try { const next = await work(); setDetail(next); if (success) setMessage(success); }
    catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  async function start(event) {
    event.preventDefault();
    if (!objective.trim()) { setMessage("Enter a coding objective."); return; }
    await perform(() => api.startCodingRun({
      objective: objective.trim(), task_id: taskId || null, project_id: projectId || null,
      repo_id: repoId || null, max_iterations: Number(maxIterations),
      agent_definition_id: agentDefinitionId || null,
    }), "Patch proposal ready for review. Nothing was applied automatically.");
  }

  async function approve(options = {}) {
    if (!action) return;
    const decidedAction = action;
    setBusy(true); setMessage("");
    try {
      const next = await api.approveCodingAction(decidedAction.id, options);
      setDetail(next);
      const result = next.action_requests.find((item) => item.id === decidedAction.id);
      setMessage(result?.status === "failed"
        ? `${decidedAction.title} failed safely: ${result.error}`
        : `${label(decidedAction.action_type)} completed.`);
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  async function reject(reason) {
    if (!action) return;
    await perform(() => api.rejectCodingAction(action.id, reason), "Action rejected; no protected action ran.");
  }

  async function skip(reason) {
    if (!action) return;
    setBusy(true); setMessage("");
    try {
      const rejected = await api.rejectCodingAction(action.id, reason);
      const skipAction = rejected.current_action_request;
      if (!skipAction || !skipAction.action_type.startsWith("skip_")) throw new Error("Skip action was not available.");
      setDetail(await api.approveCodingAction(skipAction.id, {}));
      setMessage(`${label(skipAction.action_type)} completed by explicit request.`);
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  async function revise() {
    if (!revision.trim()) { setMessage("Enter revision instructions."); return; }
    await perform(() => api.reviseCodingPatch(detail.coding_run.id, revision.trim()), "Revised proposal ready; it remains unapplied.");
    setRevision("");
  }

  return <section className={`coding-agent ${compact ? "compact" : ""}`}>
    <div className="coding-agent-title"><div><h3>Multi-Step Coding Agent</h3><p>Objective → reviewed patch → approved test → approved local checkpoint.</p></div>{detail && !TERMINAL.has(detail.coding_run.status) ? <button type="button" disabled={busy} onClick={() => perform(() => api.cancelCodingRun(detail.coding_run.id), "Coding run cancelled; logs were preserved.")}>Cancel Run</button> : null}</div>
    {runs.length ? <div className="coding-agent-runs"><strong>Coding Runs</strong>{runs.map((run) => <button type="button" key={run.id} onClick={() => perform(() => api.codingRun(run.id))}><span>{run.objective}</span><small>{label(run.status)} · iteration {run.current_iteration}/{run.max_iterations}</small></button>)}</div> : null}
    {!detail ? <form className="coding-agent-form" onSubmit={start}>
      <textarea value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="Implement, fix, or refactor…" rows={compact ? 2 : 3} />
      <div className="coding-agent-selectors">
        <label>Project<select value={projectId} onChange={(event) => { setProjectId(event.target.value); setRepoId(""); }}><option value="">Optional project</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
        <label>Repository<select value={repoId} onChange={(event) => setRepoId(event.target.value)}><option value="">Select or infer sole repo</option>{availableRepos.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
        <label>Task<select value={taskId} onChange={(event) => setTaskId(event.target.value)}><option value="">Create planned task</option>{availableTasks.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
        <label>Agent<select value={agentDefinitionId} onChange={(event) => setAgentDefinitionId(event.target.value)}><option value="general">General</option>{agentDefinitions.map((agent) => <option key={agent.id} value={agent.id}>{agent.display_name || agent.name} · {label(agent.agent_type)}</option>)}</select></label>
        <label>Max iterations<input type="number" min="1" max="10" value={maxIterations} onChange={(event) => setMaxIterations(event.target.value)} /></label>
      </div>
      <button className="neo-button" type="submit" disabled={busy}>Run Coding Agent</button>
      <p className="task-help">Starting creates a plan and proposal only. Apply, tests, and checkpoints each require approval.</p>
    </form> : <div className="coding-agent-detail">
      <div className="coding-agent-status"><strong>{detail.coding_run.objective}</strong><span className={`agent-status ${detail.coding_run.status}`}>{label(detail.coding_run.status)}</span><small>Iteration {detail.coding_run.current_iteration}/{detail.coding_run.max_iterations}</small></div>
      <button type="button" disabled={busy} onClick={exportBundle}>Export bundle</button>
      {detail.agent_definition ? <details open className="agent-run-card"><summary><strong>Active agent: {detail.agent_definition.display_name || detail.agent_definition.name}</strong></summary><p>{detail.agent_definition.description || "No description."}</p><p><strong>Permissions:</strong> {Object.entries(detail.agent_definition.permissions || {}).filter(([, value]) => value === true).map(([key]) => label(key)).join(", ") || "Read-only/default"}</p><p className="task-help">Agents cannot bypass approvals; patch apply, tests, and checkpoints remain explicit user actions.</p></details> : null}
      {detail.role_agents ? <details className="agent-run-card"><summary><strong>Subagents used</strong></summary><ul>{Object.entries(detail.role_agents).map(([role, agent]) => <li key={role}><strong>{label(role)}:</strong> {agent.display_name || agent.name}</li>)}</ul></details> : null}
      {detail.delegations?.length ? <details className="agent-run-card"><summary><strong>Delegation timeline</strong></summary><ol>{detail.delegations.map((item) => <li key={item.id}><strong>{label(item.status)}</strong> · {item.objective}</li>)}</ol></details> : null}
      {detail.tool_calls?.length ? <details className="agent-run-card"><summary><strong>Tool calls</strong></summary><ol>{detail.tool_calls.map((call) => <li key={call.id}><strong>{call.tool_id}</strong> · {label(call.status)} · approval {call.approval_status}{call.error ? ` · ${call.error}` : ""}</li>)}</ol></details> : null}
      <div><strong>Files considered</strong><ul>{detail.coding_run.selected_files.map((item) => <li key={item.file_id}><code>{item.relative_path}</code> — {item.reason} <small>{label(item.source)}</small></li>)}</ul></div>
      {detail.coding_run.metadata?.resolved_rules ? <details><summary><strong>Applied rules</strong></summary><p>{(detail.coding_run.metadata.applied_profiles || []).map(item => item.name).join(", ") || "Built-in safety rules only"}</p>{(detail.coding_run.metadata.rule_warnings || []).map((warning,index)=><div className="task-error" key={index}>{warning}</div>)}<p><strong>Forbidden paths:</strong> {(detail.coding_run.metadata.resolved_rules.forbidden_paths || []).join(", ")}</p><p><strong>Test preferences:</strong> {(detail.coding_run.metadata.resolved_rules.test_preferences || []).map(item=>item.command_hint.join(" ")).join(", ") || "None"}</p></details> : null}
      {detail.patch_artifact ? <div className="coding-agent-patch"><strong>Patch proposal</strong><pre>{detail.patch_artifact.content}</pre></div> : null}
      {detail.patch_application ? <div><p><strong>Patch application:</strong> {label(detail.patch_application.status)} · managed copy only</p>{detail.patch_application.files?.length ? <ul>{detail.patch_application.files.map((file) => <li key={file.id}><code>{file.relative_path}</code> — {label(file.change_type)} / {label(file.status)}</li>)}</ul> : null}</div> : null}
      {detail.test_run ? <div><strong>Test result: {label(detail.test_run.status)}</strong><pre>{detail.test_run.combined_output || detail.test_run.error || "No output."}</pre></div> : null}
      {detail.checkpoint ? <p><strong>Checkpoint:</strong> <code>{detail.checkpoint.commit_sha}</code></p> : null}
      {action ? <div className="coding-agent-action"><strong>Waiting for: {action.title}</strong><p>{action.description}</p>
        {action.action_type === "apply_patch" ? <>{action.payload.atomic ? <div><strong>Atomic multi-file apply</strong><p>All listed files change together or all are rolled back.</p><ul>{(action.payload.target_files || []).map((item) => <li key={item.relative_path}><code>{item.relative_path}</code> — {label(item.change_type)}</li>)}</ul></div> : <label>Target file<select value={targetFileId} onChange={(event) => setTargetFileId(event.target.value)}><option value="">Select one target</option>{(action.payload.target_files || []).map((item) => <option key={item.file_id} value={item.file_id}>{item.relative_path || item.filename}</option>)}</select></label>}<div className="coding-agent-buttons"><button type="button" disabled={busy || (!action.payload.atomic && !targetFileId)} onClick={() => window.confirm("Atomically apply this reviewed patch only to Neo’s managed workspace copy? The original repository is not modified, and no tests or checkpoints run automatically.") && approve(action.payload.atomic ? {} : { file_id: targetFileId })}>Approve and Apply</button><button type="button" disabled={busy} onClick={() => reject("Patch rejected by user.")}>Reject Patch</button></div><textarea value={revision} onChange={(event) => setRevision(event.target.value)} placeholder="Revision instructions" rows={2} /><button type="button" disabled={busy || !revision.trim()} onClick={revise}>Ask Agent to Revise</button></> : null}
        {action.action_type === "revise_patch" ? <><textarea value={revision} onChange={(event) => setRevision(event.target.value)} placeholder="How should the patch change?" rows={2} /><button type="button" disabled={busy || !revision.trim()} onClick={revise}>Create Revised Proposal</button></> : null}
        {action.action_type === "run_tests" ? <><label>Saved test command<select value={testCommandId} onChange={(event) => setTestCommandId(event.target.value)}><option value="">Select command</option>{(action.payload.test_commands || []).map((item) => <option key={item.id} value={item.id}>{item.name} · {item.command.join(" ")}</option>)}</select></label><div className="coding-agent-buttons"><button type="button" disabled={busy || !testCommandId} onClick={() => window.confirm("This runs the selected saved command only inside Neo’s managed workspace copy. Continue?") && approve({ test_command_id: testCommandId })}>Run selected test</button><button type="button" disabled={busy} onClick={() => skip("Tests skipped by user.")}>Skip Tests</button></div></> : null}
        {action.action_type === "skip_tests" ? <button type="button" disabled={busy} onClick={() => approve({})}>Confirm Skip Tests</button> : null}
        {action.action_type === "create_checkpoint" ? <div className="coding-agent-buttons"><button type="button" disabled={busy} onClick={() => window.confirm("This creates a local checkpoint in Neo’s managed copy only. It will not push or contact a remote. Continue?") && approve({})}>Create Checkpoint</button><button type="button" disabled={busy} onClick={() => skip("Checkpoint skipped by user.")}>Skip Checkpoint</button></div> : null}
        {action.action_type === "skip_checkpoint" ? <button type="button" disabled={busy} onClick={() => approve({})}>Confirm Skip Checkpoint</button> : null}
      </div> : null}
      {detail.agent_run.final_output ? <div className="coding-agent-summary"><strong>Final summary</strong><pre>{detail.agent_run.final_output}</pre></div> : null}
      {detail.coding_run.error ? <div className="task-error">{detail.coding_run.error}</div> : null}
      <RecoveryPanel
        runType="coding_agent"
        runId={detail.coding_run.id}
        embeddedEvents={detail.recovery_events || []}
        onUpdated={() => api.codingRun(detail.coding_run.id).then(setDetail).catch(() => null)}
      />
      {TERMINAL.has(detail.coding_run.status) ? <button type="button" onClick={() => { setDetail(null); setMessage(""); }}>Start another coding run</button> : null}
    </div>}
    {message ? <div className={message.toLowerCase().includes("error") ? "task-error" : "agent-message"}>{message}</div> : null}
  </section>;
}

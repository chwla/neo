import { useEffect, useState } from "react";

import { api } from "./api.js";
import RelatedMemories from "./RelatedMemories.jsx";

const TERMINAL = new Set(["done", "stopped"]);

function label(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

export default function AgenticRuns() {
  const [runs, setRuns] = useState([]);
  const [detail, setDetail] = useState(null);
  const [selectedStep, setSelectedStep] = useState(null);
  const [showContext, setShowContext] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [form, setForm] = useState({
    objective: "",
    run_type: "task",
    project_id: "",
    task_id: "",
    repo_id: "",
    max_steps: 20,
  });

  async function load() {
    const result = await api.agenticRuns();
    setRuns(result.agentic_runs || []);
  }

  useEffect(() => {
    load().catch((error) => setMessage(error.message));
  }, []);

  async function perform(action, success) {
    setBusy(true);
    setMessage("");
    try {
      const result = await action();
      setDetail(result);
      await load();
      setMessage(success);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function start(event) {
    event.preventDefault();
    if (!form.objective.trim()) return;
    await perform(
      () => api.startAgenticRun({
        objective: form.objective.trim(),
        run_type: form.run_type,
        project_id: form.project_id.trim() || null,
        task_id: form.task_id.trim() || null,
        repo_id: form.repo_id.trim() || null,
        max_steps: Number(form.max_steps),
        require_approval_for_actions: true,
      }),
      "Agentic run planned. No unsafe action executed.",
    );
  }

  async function open(id) {
    await perform(() => api.agenticRun(id), "Run detail loaded.");
    setSelectedStep(null);
    setShowContext(false);
  }

  const run = detail?.agentic_run;
  const state = detail?.state || {};

  return <section className="agentic-workspace">
    <header className="agentic-header">
      <div>
        <p className="settings-kicker">Planner · Executor · Verifier · Reflector</p>
        <h2>Agentic Runs</h2>
        <p className="dialog-caption">Structured workflows that preserve every existing approval gate.</p>
      </div>
      {run ? <button type="button" onClick={() => setDetail(null)}>New run</button> : null}
    </header>

    {!run ? <div className="agentic-grid">
      <form className="settings-section agentic-start" onSubmit={start}>
        <h3>Start a run</h3>
        <label>Objective<textarea rows={5} value={form.objective} onChange={(event) => setForm({ ...form, objective: event.target.value })} placeholder="Describe the outcome and completion criteria..." /></label>
        <div className="settings-row">
          <label>Type<select value={form.run_type} onChange={(event) => setForm({ ...form, run_type: event.target.value })}><option value="task">Task</option><option value="coding">Coding</option><option value="research">Research</option></select></label>
          <label>Maximum steps<input type="number" min="1" max="100" value={form.max_steps} onChange={(event) => setForm({ ...form, max_steps: event.target.value })} /></label>
        </div>
        <div className="settings-row">
          <label>Project ID<input value={form.project_id} onChange={(event) => setForm({ ...form, project_id: event.target.value })} /></label>
          <label>Task ID<input value={form.task_id} onChange={(event) => setForm({ ...form, task_id: event.target.value })} /></label>
          <label>Repo ID<input value={form.repo_id} onChange={(event) => setForm({ ...form, repo_id: event.target.value })} /></label>
        </div>
        <button type="submit" disabled={busy || !form.objective.trim()}>Plan agentic run</button>
      </form>
      <section className="settings-section">
        <h3>Recent runs</h3>
        <div className="agentic-run-list">
          {runs.length ? runs.map((item) => <button type="button" key={item.id} onClick={() => open(item.id)}>
            <span><strong>{item.objective}</strong><small>{label(item.run_type)} · {label(item.status)}</small></span><span aria-hidden="true">→</span>
          </button>) : <p>No agentic runs yet.</p>}
        </div>
      </section>
    </div> : <div className="agentic-detail">
      <section className="agentic-summary settings-section">
        <div><span className="agentic-phase">{state.current_phase}</span><span>{label(run.run_type)} · {label(run.status)}</span></div>
        <h3>{run.objective}</h3>
        <p><strong>Current step:</strong> {state.current_step || "Complete"}</p>
        <p><strong>Next action:</strong> {state.next_action || "None"}</p>
        <div className="settings-actions">
          {!TERMINAL.has(run.status) && state.current_phase !== "BLOCKED" ? <button type="button" disabled={busy} onClick={() => perform(() => api.stepAgenticRun(run.id), "Step processed and verified.")}>Run next step</button> : null}
          {!TERMINAL.has(run.status) ? <button type="button" disabled={busy} onClick={() => perform(() => api.continueAgenticRun(run.id), "Persisted approval state checked.")}>Continue</button> : null}
          {!TERMINAL.has(run.status) ? <button type="button" disabled={busy} onClick={() => perform(() => api.reflectAgenticRun(run.id), "Reflection recorded.")}>Reflect</button> : null}
          {!TERMINAL.has(run.status) ? <button type="button" disabled={busy} onClick={() => perform(() => api.stopAgenticRun(run.id), "Run stopped safely.")}>Stop</button> : null}
          <button type="button" onClick={() => setShowContext((value) => !value)}>{showContext ? "Hide context" : "View context"}</button>
        </div>
      </section>

      {detail.blockers?.length ? <section className="settings-error"><strong>Blocked</strong>{detail.blockers.map((item, index) => <p key={index}>{item.message}</p>)}</section> : null}

      <div className="agentic-detail-grid">
        <section className="settings-section"><h3>Plan</h3><ol className="agentic-plan">{detail.plan.map((item) => <li key={item.step_index} className={item.step_index === state.current_step_index ? "active" : ""}><strong>{item.title}</strong><span>{item.phase} · {label(item.action_class)}</span><small>{item.verification_method}</small></li>)}</ol></section>
        <section className="settings-section"><h3>Completion criteria</h3><ul>{detail.completion_criteria.map((item) => <li key={item}>{item}</li>)}</ul><h3>Context budget</h3><p>{detail.context_budget.estimated_token_count || 0} / {detail.context_budget.max_tokens || 0} estimated tokens</p><p>{detail.context_budget.included_items?.length || 0} included · {detail.context_budget.excluded_items?.length || 0} excluded</p></section>
      </div>

      {showContext ? <section className="settings-section"><h3>Budgeted context</h3>{detail.context_budget.included_items?.map((item, index) => <details key={`${item.kind}-${index}`}><summary>{item.kind} · {item.estimated_tokens} tokens</summary><pre>{typeof item.content === "string" ? item.content : JSON.stringify(item.content, null, 2)}</pre></details>)}</section> : null}

      <section className="settings-section"><h3>Step timeline</h3><div className="agentic-timeline">{detail.steps.map((step) => <button type="button" key={step.id} onClick={() => setSelectedStep(step)}><span className="agentic-phase">{step.phase}</span><span><strong>{step.title}</strong><small>{label(step.status)} · verification {step.verification?.passed ? "passed" : "not passed"}</small></span></button>)}</div></section>

      {selectedStep ? <section className="settings-section agentic-step-detail"><h3>Step detail</h3><p><strong>{selectedStep.phase} · {selectedStep.title}</strong></p><h4>Tool and action decisions</h4><pre>{JSON.stringify(selectedStep.tool_calls || [], null, 2)}</pre><h4>Verification</h4><pre>{JSON.stringify(selectedStep.verification || {}, null, 2)}</pre><h4>Reflection</h4><pre>{JSON.stringify(selectedStep.reflection || {}, null, 2)}</pre>{selectedStep.error ? <p className="settings-error">{selectedStep.error}</p> : null}</section> : null}

      {state.failures?.length ? <section className="settings-section"><h3>Failures and recovery</h3><pre>{JSON.stringify({ failures: state.failures, recovery_attempts: state.recovery_attempts }, null, 2)}</pre></section> : null}
      {detail.final_report ? <section className="settings-section"><h3>Grounded final report</h3><pre>{detail.final_report}</pre></section> : null}
      {state.web_search_run_id ? <section className="settings-section"><h3>Web search used</h3><p>Evidence run: {state.web_search_run_id}</p><button type="button" onClick={async () => setMessage(JSON.stringify(await api.webSearchRunDetail(state.web_search_run_id), null, 2))}>View cited evidence</button></section> : null}
      <RelatedMemories scopeType={state.task_id ? "task" : "project"} scopeId={state.task_id || state.project_id || run.id} />
    </div>}
    {message ? <p className={message.toLowerCase().includes("error") ? "settings-error" : "settings-status"}>{message}</p> : null}
  </section>;
}

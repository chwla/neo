import { useEffect, useState } from "react";

import { api } from "./api.js";

function label(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function RecoveryPanel({ runType, runId, embeddedSummary = null, embeddedEvents = [], onUpdated = null }) {
  const [summary, setSummary] = useState(embeddedSummary);
  const [events, setEvents] = useState(embeddedEvents || []);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [forkObjective, setForkObjective] = useState("");

  async function load() {
    if (!runType || !runId) return;
    try {
      const data = await api.recoveryRun(runType, runId);
      setSummary(data.summary);
      setEvents(data.summary?.events || []);
    } catch (error) {
      setMessage(error.message);
    }
  }

  useEffect(() => {
    if (embeddedSummary) setSummary(embeddedSummary);
    if (embeddedEvents?.length) setEvents(embeddedEvents);
    load();
  }, [runType, runId]);

  async function perform(work, success) {
    setBusy(true); setMessage("");
    try {
      const data = await work();
      setSummary(data.summary);
      setEvents(data.summary?.events || data.detail?.recovery_events || []);
      if (onUpdated) await onUpdated(data);
      setMessage(success);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  if (!runType || !runId) return null;
  const canResume = summary?.recoverability === "resumable";
  const canRetry = ["retry_or_fork", "needs_review"].includes(summary?.recoverability);
  const canFork = Boolean(summary);

  return <section className="recovery-panel">
    <div className="recovery-title">
      <div><strong>Recovery / Resume</strong><p>Resume never applies patches, runs tests, or creates checkpoints without approval.</p></div>
      <button type="button" disabled={busy} onClick={load}>Refresh</button>
    </div>
    {summary ? <div className="recovery-summary">
      <span className={`agent-status ${summary.status}`}>{label(summary.status)}</span>
      <span>{label(summary.recoverability)}</span>
      <p>{summary.explanation}</p>
      {summary.pending_action ? <p><strong>Waiting for:</strong> {summary.pending_action.title || summary.pending_action.step_type}</p> : null}
      {summary.last_successful_step ? <p><strong>Last successful step:</strong> {summary.last_successful_step.title}</p> : null}
      {summary.last_failed_or_interrupted_step ? <p><strong>Last stopped step:</strong> {summary.last_failed_or_interrupted_step.title}</p> : null}
      {summary.forked_from_run_id ? <p><strong>Forked from:</strong> <code>{summary.forked_from_run_id}</code></p> : null}
      {summary.forks?.length ? <p><strong>Forks:</strong> {summary.forks.map((item) => item.id).join(", ")}</p> : null}
    </div> : <p>Loading recovery state…</p>}
    <div className="coding-agent-buttons">
      <button type="button" disabled={busy || !canResume} onClick={() => perform(() => api.resumeRecoveryRun(runType, runId), "Run resumed safely; approval gates were preserved.")}>Resume</button>
      <button type="button" disabled={busy || !canRetry} onClick={() => perform(() => api.retryRecoveryRun(runType, runId), "Retry requested; no protected action was auto-approved.")}>Retry Safe Step</button>
      <button type="button" disabled={busy || !canFork} onClick={() => perform(() => api.forkRecoveryRun(runType, runId, { objective_override: forkObjective || null }), "Fork created; original run was not modified.")}>Fork</button>
    </div>
    <input value={forkObjective} onChange={(event) => setForkObjective(event.target.value)} placeholder="Optional fork objective override" />
    {events.length ? <div className="recovery-events"><strong>Recovery event timeline</strong><ul>{events.map((event) => <li key={event.id}><span>{label(event.event_type)}</span> · {label(event.status_before)} → {label(event.status_after)} · {new Date(event.created_at).toLocaleString()}</li>)}</ul></div> : <p>No recovery events yet.</p>}
    {message ? <div className={message.toLowerCase().includes("error") ? "task-error" : "agent-message"}>{message}</div> : null}
    <p className="task-help">Fork creates a new run; it does not modify the original run. Pending approvals remain pending after restart.</p>
  </section>;
}

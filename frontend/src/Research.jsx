import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

const controls = ["general", "technical", "business", "market", "academic", "coding"];

export default function Research({ onBack }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("technical");
  const [depth, setDepth] = useState("standard");
  const [fresh, setFresh] = useState(true);
  const [plan, setPlan] = useState(null);
  const [runs, setRuns] = useState([]);
  const [active, setActive] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadRuns = useCallback(async () => {
    try { setRuns((await api.researchModeRuns()).runs || []); } catch (err) { setError(err.message); }
  }, []);
  useEffect(() => { loadRuns(); }, [loadRuns]);

  async function preview() {
    if (!question.trim()) return;
    setBusy(true); setError("");
    try { setPlan(await api.researchModePlan({ question, mode, freshness_required: fresh, depth })); }
    catch (err) { setError(err.message || "Could not create research plan."); }
    finally { setBusy(false); }
  }
  async function run() {
    if (!question.trim()) return;
    setBusy(true); setError("");
    try {
      const result = await api.researchModeRun({ question, mode, depth, freshness_required: fresh, max_search_runs: depth === "deep" ? 4 : 2, max_sources: depth === "deep" ? 20 : 12, include_memory: true, include_conflict_analysis: true });
      setActive(result); setPlan(result.plan); await loadRuns();
    } catch (err) { setError(err.message || "Research run failed."); }
    finally { setBusy(false); }
  }
  async function select(run) {
    setBusy(true); setError("");
    try { setActive(await api.researchModeDetail(run.id)); } catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  async function action(kind) {
    if (!active) return;
    setBusy(true); setError("");
    try {
      if (kind === "validate") { const validation = await api.researchModeValidate(active.id); setActive({ ...active, citation_validation: validation }); }
      else { setActive(await (kind === "refresh" ? api.researchModeRefresh(active.id) : api.researchModeContinue(active.id))); await loadRuns(); }
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  return <div className="research-layout">
    <main className="research-main">
      <div className="research-header"><button className="research-back" onClick={onBack} type="button">← Chat</button><h2 className="research-title">Enterprise Research Mode</h2></div>
      <section className="research-input-area">
        <textarea className="research-query" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask an evidence-grounded research question…" />
        <div className="research-controls"><select value={mode} onChange={(event) => setMode(event.target.value)}>{controls.map((item) => <option key={item}>{item}</option>)}</select><select value={depth} onChange={(event) => setDepth(event.target.value)}><option>quick</option><option>standard</option><option>deep</option></select><label><input type="checkbox" checked={fresh} onChange={(event) => setFresh(event.target.checked)} /> Current sources</label><button className="research-cancel-btn" type="button" disabled={busy || !question.trim()} onClick={preview}>Plan preview</button><button className="research-start-btn" type="button" disabled={busy || !question.trim()} onClick={run}>{busy ? "Working…" : "Run research"}</button></div>
      </section>
      {error && <div className="research-error">{error}</div>}
      {plan && <section className="research-meta-bar"><h3>Research plan</h3><p>{plan.objective}</p><ul>{(plan.subquestions || []).map((item) => <li key={item}>{item}</li>)}</ul><small>Requirements: {(plan.required_sources || []).join(", ")}</small></section>}
      {active && <Report run={active} busy={busy} onAction={action} />}
    </main>
    <aside className="research-sidebar"><div className="research-sidebar-header"><h3 className="research-sidebar-title">Research history</h3></div>{runs.length ? <div className="research-jobs-list">{runs.map((item) => <button type="button" onClick={() => select(item)} className={`research-job-item ${active?.id === item.id ? "active" : ""}`} key={item.id}><span className="research-job-query">{item.question}</span><span className="research-job-meta">{item.status} · {(item.confidence?.overall || 0).toFixed(0)}</span></button>)}</div> : <p className="research-sidebar-empty">No research runs yet.</p>}</aside>
  </div>;
}

function Report({ run, busy, onAction }) {
  const report = run.report?.content_text || run.report_text;
  return <section className="research-report">
    <div className="research-report-meta"><span className={`research-status-badge ${run.status}`}>{run.status}</span><span>Confidence: {Math.round((run.confidence?.overall || 0) * 100)}%</span><span>Citations: {run.citation_validation?.passed ? "validated" : "needs review"}</span></div>
    <div className="research-report-actions"><button className="research-save-note-btn" disabled={busy} onClick={() => onAction("validate")}>Validate citations</button><button className="research-save-note-btn" disabled={busy} onClick={() => onAction("continue")}>Continue</button><button className="research-save-note-btn" disabled={busy} onClick={() => onAction("refresh")}>Refresh</button></div>
    <h3>Evidence</h3><div className="research-meta-bar">{(run.evidence || []).map((item) => <p key={item.id}><strong>{item.citation_label || "Memory"}</strong> · quality {Math.round((item.quality_score || 0) * 100)}% · {item.evidence_text}<small>Score: {JSON.stringify(item.metadata?.score_breakdown || {})}</small></p>) || "No evidence recorded."}</div>
    <h3>Claims</h3><div className="research-meta-bar">{(run.claims || []).map((item) => <p key={item.id}><strong>{item.status}</strong> · {item.claim} {(item.citation_ids || []).join(" ")}</p>) || "No claims recorded."}</div>
    <h3>Conflicts</h3><div className="research-meta-bar">{(run.conflicts || []).length ? run.conflicts.map((item) => <p key={item.id || item.topic}><strong>{item.severity}</strong> · {item.topic}: {item.recommended_resolution}</p>) : "No material conflicts detected."}</div>
    <article className="research-report-body">{report || "This run did not produce a final report."}</article>
  </section>;
}

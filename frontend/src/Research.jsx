import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

const controls = ["general", "technical", "business", "market", "academic", "coding"];
const terminalStatuses = new Set(["completed", "failed", "cancelled"]);

function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

export default function Research({ onBack, memoryEnabled, memoryIncognito }) {
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
    try {
      setRuns((await api.researchList()).jobs || []);
    } catch (err) {
      setError(err.message);
    }
  }, []);
  useEffect(() => { loadRuns(); }, [loadRuns]);

  async function preview() {
    if (!question.trim()) return;
    setBusy(true); setError("");
    try {
      setPlan(await api.researchModePlan({
        question,
        mode,
        freshness_required: fresh,
        depth,
      }));
    } catch (err) {
      setError(err.message || "Could not create research plan.");
    } finally {
      setBusy(false);
    }
  }

  async function waitForRun(jobId) {
    for (;;) {
      const job = await api.researchJob(jobId);
      setActive(job);
      if (job.plan) setPlan(job.plan);
      if (terminalStatuses.has(job.status)) return job;
      await delay(750);
    }
  }

  async function run() {
    if (!question.trim()) return;
    setBusy(true); setError("");
    try {
      const started = await api.researchStart({
        query: question,
        depth,
        memory_enabled: memoryEnabled,
        incognito: memoryIncognito,
      });
      await waitForRun(started.job_id);
      await loadRuns();
    } catch (err) {
      setError(err.message || "Research run failed.");
    } finally {
      setBusy(false);
    }
  }

  async function select(runItem) {
    setBusy(true); setError("");
    try {
      const job = await api.researchJob(runItem.id);
      setActive(job);
      if (job.plan) setPlan(job.plan);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
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
      {active && <Report run={active} />}
    </main>
    <aside className="research-sidebar"><div className="research-sidebar-header"><h3 className="research-sidebar-title">Research history</h3></div>{runs.length ? <div className="research-jobs-list">{runs.map((item) => <button type="button" onClick={() => select(item)} className={`research-job-item ${active?.id === item.id ? "active" : ""}`} key={item.id}><span className="research-job-query">{item.user_query}</span><span className="research-job-meta">{item.status} · {item.progress_percent || 0}%</span></button>)}</div> : <p className="research-sidebar-empty">No research runs yet.</p>}</aside>
  </div>;
}

function Report({ run }) {
  return <section className="research-report">
    <div className="research-report-meta"><span className={`research-status-badge ${run.status}`}>{run.status}</span><span>{run.progress_percent || 0}%</span><span>{run.current_step || "Queued"}</span></div>
    {run.error && <div className="research-error">{run.error}</div>}
    <h3>Evidence</h3><div className="research-meta-bar">{(run.evidence_chunks || []).length ? run.evidence_chunks.map((item, index) => <p key={item.id || index}><strong>{item.source_title || "Source evidence"}</strong> · {item.content || item.text || item.evidence_text}</p>) : "No evidence recorded yet."}</div>
    <h3>Sources</h3><div className="research-meta-bar">{(run.sources || []).length ? run.sources.map((item, index) => <p key={item.id || index}><strong>{item.title || "Source"}</strong>{item.url ? <> · <a href={item.url} target="_blank" rel="noreferrer">Open</a></> : null}</p>) : "No sources recorded yet."}</div>
    <article className="research-report-body">{run.report || "The report is still being prepared."}</article>
  </section>;
}

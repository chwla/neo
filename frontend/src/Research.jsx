import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import Icon from "./WorkspaceIcon.jsx";

const MODES = ["general", "technical", "business", "market", "academic", "coding"];
const DEPTHS = ["quick", "standard", "deep"];
const terminalStatuses = new Set(["completed", "failed", "cancelled"]);

function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function statusTone(status) {
  if (status === "completed") return "accent";
  if (status === "failed" || status === "cancelled") return "danger";
  return "warn";
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

  return (
    <div className="ws ws-research">
      <section className="ws-main">
        <div className="ws-toolbar">
          <div className="ws-toolbar-state">
            <button className="ws-back" type="button" onClick={onBack}>
              <Icon name="back" />
              Chat
            </button>
            <span className="ws-toolbar-title">Research</span>
            {active && <span className={`ws-badge ${statusTone(active.status)}`}>{active.status}</span>}
          </div>
          <div className="ws-toolbar-actions">
            <button className="ws-action" type="button" disabled={busy || !question.trim()} onClick={preview}>
              <Icon name="layers" />
              Plan preview
            </button>
            <button className="ws-save" type="button" disabled={busy || !question.trim()} onClick={run}>
              {busy ? "Working…" : "Run research"}
            </button>
          </div>
        </div>

        <div className="ws-stage">
          <div className="ws-doc">
            <section className="ws-section">
              <textarea
                className="ws-research-query"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                placeholder="Ask an evidence-grounded research question…"
                aria-label="Research question"
              />
              <div className="ws-field-row">
                <label className="ws-field">
                  <span>Mode</span>
                  <select value={mode} onChange={(event) => setMode(event.target.value)}>
                    {MODES.map((item) => <option key={item}>{item}</option>)}
                  </select>
                </label>
                <label className="ws-field">
                  <span>Depth</span>
                  <select value={depth} onChange={(event) => setDepth(event.target.value)}>
                    {DEPTHS.map((item) => <option key={item}>{item}</option>)}
                  </select>
                </label>
                <label className="ws-field">
                  <span>Sources</span>
                  <button
                    type="button"
                    className={`ws-toggle ${fresh ? "on" : ""}`}
                    aria-pressed={fresh}
                    onClick={() => setFresh((value) => !value)}
                  >
                    Current sources only
                  </button>
                </label>
              </div>
            </section>

            {error && <div className="ws-error">{error}</div>}

            {plan && (
              <section className="ws-section">
                <div className="ws-section-head">
                  <h3 className="ws-section-title">Research plan</h3>
                </div>
                <p className="ws-help">{plan.objective}</p>
                {(plan.subquestions || []).length > 0 && (
                  <div className="ws-tiles">
                    {(plan.subquestions || []).map((item) => (
                      <div className="ws-tile" key={item}><strong>{item}</strong></div>
                    ))}
                  </div>
                )}
                {(plan.required_sources || []).length > 0 && (
                  <div className="ws-row-meta ws-plan-sources">
                    {(plan.required_sources || []).map((item) => (
                      <span className="ws-chip" key={item}>{item}</span>
                    ))}
                  </div>
                )}
              </section>
            )}

            {active ? <Report run={active} /> : !plan && (
              <div className="ws-blank ws-blank-inline">
                <div className="ws-blank-mark"><Icon name="search" /></div>
                <h2>No research run open</h2>
                <p>Ask a question above. Plan preview shows the sub-questions and required sources before anything runs.</p>
              </div>
            )}
          </div>
        </div>
      </section>

      <aside className="ws-rail ws-rail-right">
        <header className="ws-rail-head">
          <div className="ws-rail-top">
            <span className="ws-rail-count">History</span>
            <span className="ws-rail-count">{runs.length}</span>
          </div>
        </header>
        <div className="ws-list">
          {runs.length ? runs.map((item) => (
            <button
              type="button"
              onClick={() => select(item)}
              className={`ws-row ${active?.id === item.id ? "active" : ""}`}
              key={item.id}
            >
              <span className="ws-row-head">
                <span className="ws-row-title">{item.user_query}</span>
              </span>
              <span className="ws-row-meta">
                <span className={`ws-badge ${statusTone(item.status)}`}>{item.status}</span>
                <span className="ws-row-more">{item.progress_percent || 0}%</span>
              </span>
            </button>
          )) : (
            <div className="ws-list-empty"><p>No research runs yet.</p></div>
          )}
        </div>
      </aside>
    </div>
  );
}

function Report({ run }) {
  const evidence = run.evidence_chunks || [];
  const sources = run.sources || [];
  return (
    <>
      <section className="ws-section">
        <div className="ws-section-head">
          <h3 className="ws-section-title">Progress</h3>
          <span className={`ws-badge ${statusTone(run.status)}`}>{run.status}</span>
          <span className="ws-meta">{run.current_step || "Queued"}</span>
        </div>
        <div className="ws-progress" role="progressbar" aria-valuenow={run.progress_percent || 0}>
          <span style={{ width: `${Math.min(100, Math.max(0, run.progress_percent || 0))}%` }} />
        </div>
        {run.error && <div className="ws-error">{run.error}</div>}
      </section>

      <section className="ws-section">
        <div className="ws-section-head">
          <h3 className="ws-section-title">Evidence</h3>
          <span className="ws-meta">{evidence.length}</span>
        </div>
        {evidence.length ? (
          <div className="ws-tiles">
            {evidence.map((item, index) => (
              <div className="ws-evidence" key={item.id || index}>
                <strong>{item.source_title || "Source evidence"}</strong>
                <p>{item.content || item.text || item.evidence_text}</p>
              </div>
            ))}
          </div>
        ) : <p className="ws-empty-line">No evidence recorded yet.</p>}
      </section>

      <section className="ws-section">
        <div className="ws-section-head">
          <h3 className="ws-section-title">Sources</h3>
          <span className="ws-meta">{sources.length}</span>
        </div>
        {sources.length ? (
          <div className="ws-tiles">
            {sources.map((item, index) => (
              <div className="ws-tile" key={item.id || index}>
                <strong>{item.title || "Source"}</strong>
                {item.url && (
                  <a className="ws-action" href={item.url} target="_blank" rel="noreferrer">
                    <Icon name="external" />
                    Open
                  </a>
                )}
              </div>
            ))}
          </div>
        ) : <p className="ws-empty-line">No sources recorded yet.</p>}
      </section>

      <section className="ws-section">
        <div className="ws-section-head">
          <h3 className="ws-section-title">Report</h3>
        </div>
        <article className="ws-report-body">{run.report || "The report is still being prepared."}</article>
      </section>
    </>
  );
}

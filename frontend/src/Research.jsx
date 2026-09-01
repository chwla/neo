import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import Icon from "./WorkspaceIcon.jsx";

const MODES = [
  ["technical", "Technical"],
  ["general", "General"],
  ["business", "Business"],
  ["market", "Market"],
  ["academic", "Academic"],
  ["coding", "Coding"],
];

const DEPTHS = [
  ["quick", "Quick", "3–5 queries · 5 sources"],
  ["standard", "Standard", "5–8 queries · 10 sources"],
  ["deep", "Deep", "8–12 queries · 20 sources"],
];

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function statusTone(status) {
  if (status === "completed") return "ok";
  if (status === "failed" || status === "cancelled") return "bad";
  return "run";
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function shortDate(iso) {
  if (!iso) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  return value.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function relativeTime(iso) {
  const time = Date.parse(iso || "");
  if (Number.isNaN(time)) return "";
  const diff = Date.now() - time;
  if (diff < 60000) return "just now";
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  if (diff < 7 * 86400000) return `${Math.floor(diff / 86400000)}d ago`;
  return shortDate(iso);
}

/**
 * Evidence-grounded research.
 *
 * The run is watched, not waited on. The old screen awaited the whole job
 * inside the click handler, so a deep run -- minutes of work -- left every
 * control disabled and no way to stop it. Here the poll lives in an effect
 * keyed on the open run, which means the run can be cancelled, the history
 * browsed and a second question composed while the first is still fetching.
 *
 * What comes back is shown as what it is: the report as Markdown, evidence
 * grouped under the sub-question it answers, and sources with the domain,
 * date and relevance the ranker already computed and the old screen dropped.
 */
export default function Research({ onBack, onOpenNote, memoryEnabled, memoryIncognito }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("technical");
  const [depth, setDepth] = useState("standard");
  const [fresh, setFresh] = useState(true);
  const [plan, setPlan] = useState(null);
  const [planning, setPlanning] = useState(false);

  const [runs, setRuns] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [run, setRun] = useState(null);
  const [starting, setStarting] = useState(false);
  const [tab, setTab] = useState("report");
  const [error, setError] = useState("");
  const [savedNote, setSavedNote] = useState(null);
  const [composing, setComposing] = useState(true);
  const [confirmClear, setConfirmClear] = useState(false);
  const questionRef = useRef(null);

  const loadRuns = useCallback(async () => {
    try {
      setRuns((await api.researchList(40)).jobs || []);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  /* Watch the open run instead of blocking on it. */
  useEffect(() => {
    if (!activeId) return undefined;
    let alive = true;
    let handle = null;
    async function tick() {
      try {
        const job = await api.researchJob(activeId);
        if (!alive) return;
        setRun(job);
        if (job.plan) setPlan(job.plan);
        if (TERMINAL.has(job.status)) {
          loadRuns();
          return;
        }
      } catch (err) {
        if (alive) setError(err.message);
        return;
      }
      handle = setTimeout(tick, 1000);
    }
    tick();
    return () => {
      alive = false;
      if (handle) clearTimeout(handle);
    };
  }, [activeId, loadRuns]);

  const live = Boolean(run && !TERMINAL.has(run.status));
  const sources = useMemo(() => run?.sources || [], [run]);
  const evidence = useMemo(() => run?.evidence_chunks || [], [run]);

  const fetched = sources.filter((source) => source.fetched);
  const stats = [
    ["Queries", (run?.generated_queries || []).length],
    ["Sources", sources.length],
    ["Fetched", fetched.length],
    ["Evidence", evidence.length],
  ];

  /* Evidence answers a sub-question; showing it grouped is showing the answer. */
  const grouped = useMemo(() => {
    const buckets = new Map();
    for (const chunk of evidence) {
      const heading = chunk.supports_subquestion || "General findings";
      if (!buckets.has(heading)) buckets.set(heading, []);
      buckets.get(heading).push(chunk);
    }
    for (const list of buckets.values()) {
      list.sort((a, b) => (b.relevance_score || 0) - (a.relevance_score || 0));
    }
    return [...buckets.entries()];
  }, [evidence]);

  const rankedSources = useMemo(
    () =>
      [...sources].sort(
        (a, b) =>
          (b.relevance_score || 0) + (b.quality_score || 0) -
          ((a.relevance_score || 0) + (a.quality_score || 0)),
      ),
    [sources],
  );

  const reportHtml = useMemo(() => (run?.report ? renderMarkdown(run.report) : ""), [run?.report]);

  async function preview() {
    if (!question.trim()) return;
    setPlanning(true);
    setError("");
    try {
      setPlan(await api.researchModePlan({
        question,
        mode,
        freshness_required: fresh,
        depth,
      }));
    } catch (err) {
      setError(err.message || "Could not create a research plan.");
    } finally {
      setPlanning(false);
    }
  }

  async function start() {
    if (!question.trim() || starting) return;
    setStarting(true);
    setError("");
    setSavedNote(null);
    try {
      const started = await api.researchStart({
        query: question,
        depth,
        memory_enabled: memoryEnabled,
        incognito: memoryIncognito,
      });
      setRun(null);
      setActiveId(started.job_id);
      setComposing(false);
      setTab("report");
      await loadRuns();
    } catch (err) {
      setError(err.message || "Research run failed to start.");
    } finally {
      setStarting(false);
    }
  }

  async function cancel() {
    if (!activeId) return;
    try {
      await api.researchCancel(activeId);
    } catch (err) {
      setError(err.message);
    }
  }

  function open(id) {
    setError("");
    setSavedNote(null);
    setComposing(false);
    setTab("report");
    setActiveId(id);
  }

  async function saveToNote() {
    if (!run) return;
    setError("");
    try {
      const data = await api.researchSaveToNote(run.id, {
        title: run.user_query,
        tags: ["research"],
      });
      setSavedNote(data);
    } catch (err) {
      setError(err.message || "Could not save this report as a note.");
    }
  }

  async function clearHistory() {
    setConfirmClear(false);
    try {
      await api.researchClear();
      setActiveId(null);
      setRun(null);
      setComposing(true);
      await loadRuns();
    } catch (err) {
      setError(err.message);
    }
  }

  function newRun() {
    setComposing(true);
    setActiveId(null);
    setRun(null);
    setPlan(null);
    setSavedNote(null);
    setError("");
    window.requestAnimationFrame(() => questionRef.current?.focus());
  }

  const tabs = [
    ["report", "Report", run?.report ? 1 : 0],
    ["evidence", "Evidence", evidence.length],
    ["sources", "Sources", sources.length],
    ["plan", "Plan", (plan?.subquestions || []).length],
  ];

  return (
    <div className="rs">
      <section className="rs-main">
        <header className="rs-bar">
          <button className="rs-back" type="button" onClick={onBack}>
            <Icon name="back" />
            Chat
          </button>
          <span className="rs-bar-title">Research</span>
          {run && !composing && (
            <span className={`rs-status ${statusTone(run.status)}`}>
              {live && <span className="rs-pulse" />}
              {run.status}
            </span>
          )}
          <div className="rs-bar-actions">
            {live && (
              <button className="rs-btn danger" type="button" onClick={cancel}>
                <Icon name="stop" />
                Stop
              </button>
            )}
            {run?.report && !live && (
              <button className="rs-btn" type="button" onClick={saveToNote}>
                <Icon name="note" />
                Save as note
              </button>
            )}
            <button className="rs-btn primary" type="button" onClick={newRun}>
              <Icon name="plus" />
              New question
            </button>
          </div>
        </header>

        <div className="rs-scroll">
          {error && (
            <div className="rs-error">
              <Icon name="warning" />
              <span>{error}</span>
              <button type="button" onClick={() => setError("")} aria-label="Dismiss">
                <Icon name="close" />
              </button>
            </div>
          )}

          {savedNote && (
            <div className="rs-notice">
              <Icon name="check" />
              <span>{savedNote.already_saved ? "Already saved as a note." : "Saved as a note."}</span>
              {onOpenNote && (
                <button type="button" onClick={() => onOpenNote(savedNote.note.id)}>
                  Open note
                  <Icon name="next" />
                </button>
              )}
            </div>
          )}

          {composing || !run ? (
            <div className="rs-composer">
              <div className="rs-composer-head">
                <h1>What do you want answered?</h1>
                <p>Neo searches, fetches and reads sources, then writes a report grounded in what it found. Preview the plan first if you want to see the sub-questions before anything runs.</p>
              </div>

              <textarea
                ref={questionRef}
                className="rs-question"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") start();
                }}
                placeholder="e.g. How do the current serverless Postgres providers compare on cold start and pricing?"
                aria-label="Research question"
                rows={4}
              />

              <div className="rs-depths" role="radiogroup" aria-label="Depth">
                {DEPTHS.map(([value, label, detail]) => (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={depth === value}
                    className={`rs-depth ${depth === value ? "on" : ""}`.trim()}
                    onClick={() => setDepth(value)}
                  >
                    <strong>{label}</strong>
                    <span>{detail}</span>
                  </button>
                ))}
              </div>

              <div className="rs-options">
                <label className="rs-option">
                  <span>Lens</span>
                  <select value={mode} onChange={(event) => setMode(event.target.value)}>
                    {MODES.map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  className={`rs-toggle ${fresh ? "on" : ""}`.trim()}
                  aria-pressed={fresh}
                  onClick={() => setFresh((value) => !value)}
                >
                  <Icon name="clock" />
                  Current sources only
                </button>
                <div className="rs-composer-actions">
                  <button className="rs-btn" type="button" disabled={planning || !question.trim()} onClick={preview}>
                    <Icon name="layers" />
                    {planning ? "Planning…" : "Preview plan"}
                  </button>
                  <button className="rs-btn primary lg" type="button" disabled={starting || !question.trim()} onClick={start}>
                    <Icon name="play" />
                    {starting ? "Starting…" : "Run research"}
                    <kbd>⌘⏎</kbd>
                  </button>
                </div>
              </div>

              {plan && <PlanView plan={plan} />}
            </div>
          ) : (
            <article className="rs-run">
              <h1 className="rs-question-title">{run.user_query}</h1>
              <div className="rs-run-facts">
                <span className={`rs-status ${statusTone(run.status)}`}>
                  {live && <span className="rs-pulse" />}
                  {run.status}
                </span>
                <span>{run.depth} depth</span>
                <span>{relativeTime(run.created_at)}</span>
              </div>

              {(live || run.status === "failed") && (
                <div className="rs-progress-card">
                  <div className="rs-progress-top">
                    <span className="rs-step">{run.current_step || "Queued"}</span>
                    <span className="rs-pct">{run.progress_percent || 0}%</span>
                  </div>
                  <div className="rs-progress" role="progressbar" aria-valuenow={run.progress_percent || 0} aria-valuemin={0} aria-valuemax={100}>
                    <span style={{ width: `${Math.min(100, Math.max(0, run.progress_percent || 0))}%` }} />
                  </div>
                  <div className="rs-stats">
                    {stats.map(([label, value]) => (
                      <div className="rs-stat" key={label}>
                        <strong>{value}</strong>
                        <span>{label}</span>
                      </div>
                    ))}
                  </div>
                  {run.error && <p className="rs-run-error">{run.error}</p>}
                </div>
              )}

              <nav className="rs-tabs">
                {tabs.map(([value, label, count]) => (
                  <button
                    key={value}
                    type="button"
                    className={`rs-tab ${tab === value ? "on" : ""}`.trim()}
                    onClick={() => setTab(value)}
                  >
                    {label}
                    {count > 0 && value !== "report" && <em>{count}</em>}
                  </button>
                ))}
              </nav>

              {tab === "report" && (
                run.report ? (
                  <div
                    className="rs-report"
                    // The report is Neo's own Markdown, escaped by renderMarkdown.
                    dangerouslySetInnerHTML={{ __html: reportHtml }}
                  />
                ) : (
                  <div className="rs-placeholder">
                    <Icon name={live ? "refresh" : "book"} />
                    <p>{live ? "The report is written once the evidence is in. Evidence and sources fill in as they arrive." : "No report was produced for this run."}</p>
                  </div>
                )
              )}

              {tab === "evidence" && (
                grouped.length ? (
                  <div className="rs-groups">
                    {grouped.map(([heading, chunks]) => (
                      <section className="rs-group" key={heading}>
                        <h2>
                          {heading}
                          <em>{chunks.length}</em>
                        </h2>
                        {chunks.map((chunk, position) => (
                          <figure className="rs-evidence" key={`${chunk.source_id}-${position}`}>
                            <blockquote>{chunk.text || "(empty excerpt)"}</blockquote>
                            <figcaption>
                              {chunk.source_url ? (
                                <a href={chunk.source_url} target="_blank" rel="noreferrer">
                                  <Icon name="globe" />
                                  {chunk.source_title || hostOf(chunk.source_url) || "Source"}
                                </a>
                              ) : (
                                <span>{chunk.source_title || "Source"}</span>
                              )}
                              {chunk.evidence_category && chunk.evidence_category !== "general" && (
                                <span className="rs-chip">{chunk.evidence_category}</span>
                              )}
                              {chunk.claim_type && chunk.claim_type !== "general" && (
                                <span className="rs-chip">{chunk.claim_type}</span>
                              )}
                              <Meter value={chunk.relevance_score} label="relevance" />
                            </figcaption>
                          </figure>
                        ))}
                      </section>
                    ))}
                  </div>
                ) : (
                  <div className="rs-placeholder">
                    <Icon name="quote" />
                    <p>{live ? "Nothing extracted yet." : "No evidence was recorded for this run."}</p>
                  </div>
                )
              )}

              {tab === "sources" && (
                rankedSources.length ? (
                  <div className="rs-sources">
                    {rankedSources.map((source, position) => (
                      <a
                        className={`rs-source ${source.fetched ? "" : "unread"}`.trim()}
                        key={source.id ?? position}
                        href={source.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <span className="rs-source-rank">{position + 1}</span>
                        <span className="rs-source-body">
                          <strong>{source.title || hostOf(source.url) || source.url}</strong>
                          <span className="rs-source-meta">
                            <span className="rs-source-host">{source.domain || hostOf(source.url)}</span>
                            {source.published_date && <span>{shortDate(source.published_date)}</span>}
                            {source.evidence_count > 0 && <span>{source.evidence_count} excerpt{source.evidence_count === 1 ? "" : "s"}</span>}
                            {!source.fetched && <span className="rs-chip warn">{source.fetch_status || "not fetched"}</span>}
                          </span>
                          <span className="rs-source-scores">
                            <Meter value={source.relevance_score} label="relevance" />
                            <Meter value={source.quality_score} label="quality" />
                          </span>
                        </span>
                        <Icon name="external" />
                      </a>
                    ))}
                  </div>
                ) : (
                  <div className="rs-placeholder">
                    <Icon name="globe" />
                    <p>{live ? "Still searching." : "No sources were recorded for this run."}</p>
                  </div>
                )
              )}

              {tab === "plan" && (
                plan ? <PlanView plan={plan} queries={run.generated_queries} /> : (
                  <div className="rs-placeholder">
                    <Icon name="layers" />
                    <p>No plan was stored for this run.</p>
                  </div>
                )
              )}
            </article>
          )}
        </div>
      </section>

      <aside className="rs-rail">
        <header className="rs-rail-head">
          <h2>History</h2>
          {runs.length > 0 && (
            confirmClear ? (
              <span className="rs-rail-confirm">
                <button type="button" onClick={() => setConfirmClear(false)}>Cancel</button>
                <button type="button" className="danger" onClick={clearHistory}>Clear all</button>
              </span>
            ) : (
              <button type="button" className="rs-rail-clear" onClick={() => setConfirmClear(true)}>
                <Icon name="trash" />
              </button>
            )
          )}
        </header>
        <div className="rs-rail-list">
          {runs.length ? runs.map((item) => (
            <button
              type="button"
              key={item.id}
              className={`rs-histrow ${activeId === item.id && !composing ? "active" : ""}`.trim()}
              onClick={() => open(item.id)}
            >
              <span className="rs-histrow-top">
                <span className={`rs-dot ${statusTone(item.status)}`} />
                <span className="rs-histrow-title">{item.user_query}</span>
              </span>
              <span className="rs-histrow-meta">
                <span>{relativeTime(item.created_at)}</span>
                <span>{item.depth}</span>
                {!TERMINAL.has(item.status) && <span>{item.progress_percent || 0}%</span>}
                {item.has_report && <Icon name="book" />}
              </span>
            </button>
          )) : (
            <p className="rs-rail-empty">Runs you start show up here.</p>
          )}
        </div>
      </aside>
    </div>
  );
}

/** A 0–1 score as a bar, because a bare "0.72" says nothing at a glance. */
function Meter({ value, label }) {
  const score = Math.min(1, Math.max(0, Number(value) || 0));
  if (!score) return null;
  return (
    <span className="rs-meter" title={`${label} ${score.toFixed(2)}`}>
      <span className="rs-meter-track">
        <span className="rs-meter-fill" style={{ width: `${score * 100}%` }} />
      </span>
      <span className="rs-meter-label">{label}</span>
    </span>
  );
}

function PlanView({ plan, queries }) {
  return (
    <section className="rs-plan">
      {plan.objective && (
        <div className="rs-plan-objective">
          <h2>Objective</h2>
          <p>{plan.objective}</p>
        </div>
      )}
      {(plan.subquestions || []).length > 0 && (
        <div className="rs-plan-block">
          <h2>Sub-questions</h2>
          <ol className="rs-plan-list">
            {plan.subquestions.map((item) => <li key={item}>{item}</li>)}
          </ol>
        </div>
      )}
      {(queries || plan.queries || []).length > 0 && (
        <div className="rs-plan-block">
          <h2>Searches</h2>
          <div className="rs-plan-queries">
            {(queries || plan.queries).map((item) => (
              <code key={item}>{item}</code>
            ))}
          </div>
        </div>
      )}
      {(plan.required_sources || plan.source_preferences || []).length > 0 && (
        <div className="rs-plan-block">
          <h2>Preferred sources</h2>
          <div className="rs-chiprow">
            {(plan.required_sources || plan.source_preferences).map((item) => (
              <span className="rs-chip" key={item}>{item}</span>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

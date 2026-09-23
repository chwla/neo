import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import Icon from "./WorkspaceIcon.jsx";

/* Depth changes how thoroughly Neo investigates, not how long the report is. */
const DEPTHS = [
  ["quick", "Quick", "Core answer from the strongest sources; stops as soon as it's established"],
  ["standard", "Standard", "Every major aspect, cross-checked"],
  ["deep", "Deep", "Broader context, conflicts investigated, strictest verification"],
];

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

/* How a run ended, in the reader's terms. One chip, read from the run's completion status. */
const COMPLETION = {
  completed: ["Complete", ""],
  completed_with_gaps: ["Complete, with open questions", "warn"],
  ceiling_reached: ["Stopped at this depth's limit", "warn"],
  blocked: ["Search unavailable", "warn"],
  cancelled: ["Cancelled", ""],
  failed: ["Failed", "bad"],
};
/* Older runs stored only why the loop stopped; they are shown the same way. */
const LEGACY_COMPLETION = {
  coverage_sufficient: "completed",
  insufficient_evidence: "completed_with_gaps",
  budget_exhausted: "ceiling_reached",
  search_unavailable: "blocked",
  user_cancelled: "cancelled",
  failed: "failed",
};

/* What the run is doing now. The engine names the phase; this is how it reads. */
const PHASES = {
  planning: "Working out what to establish",
  finding_primary_evidence: "Looking for original sources",
  investigating: "Investigating",
  checking_conflicts: "Checking where sources disagree",
  expanding: "Following up what the evidence raised",
  verifying: "Checking every claim against its evidence",
  writing: "Writing the report",
};

const SUFFICIENCY = {
  established: ["Established", ""],
  hedged: ["Reported, not established", "warn"],
  insufficient: ["Not established", "warn"],
};
const BASIS = {
  decisive: "by the original record",
  convergent: "by independent sources that agree",
  reports: "by individual reports",
};
/* What became of the search for an original record, when it is worth saying. */
const ORIGIN = {
  sought_not_found: "no original record was found",
  located_unreadable: "the original record was found but could not be read",
};
const PROVENANCE = {
  A: ["verified publisher", ""],
  B: ["self-identified", ""],
  C: ["unverified", ""],
  D: ["weak", "warn"],
};
const CLAIM_FIT = {
  establishes: "establishes",
  supports: "supports",
  context: "context only",
  insufficient: "cannot support a finding",
};

function words(value) {
  return String(value || "").replaceAll("_", " ");
}

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
  return value.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
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

function isReviewed(source) {
  return source.fetch_outcome === "fetched" || source.fetch_outcome === "cached";
}

/** How a run ended, as one chip: its completion status, or what an older run recorded. */
export function completionOf(run) {
  const status = run?.metadata?.completion_status
    || LEGACY_COMPLETION[run?.metadata?.stop_reason]
    || (run?.status === "cancelled" ? "cancelled" : null);
  return status ? COMPLETION[status] : null;
}

/** What the run is doing now, in the reader's terms. */
export function phaseOf(run) {
  const log = run?.progress_log || [];
  const latest = log.length ? log[log.length - 1] : null;
  return [PHASES[latest?.phase] || run?.current_step || "Queued", latest];
}

/**
 * Where the answer stands, from the same target states the report is written from.
 *
 * Counted over the questions the plan asked, not the per-subject parts they were researched
 * through, so "3 of 4" means what a reader would count.
 */
export function standingOf(run, metrics, states) {
  const top = (states || []).filter((state) => !plannedChild(run?.plan, state.target_id));
  const core = top.filter((state) => state.tier === "core");
  const contested = (targetId) => (states || []).some((state) => {
    const target = (run?.plan?.targets || []).find((item) => item.id === state.target_id);
    return (state.target_id === targetId || target?.parent_id === targetId)
      && (state.needs || []).some((need) => need.material && need.kind === "conflict_material");
  });
  const established = core.filter((state) => state.sufficiency === "established" && !contested(state.target_id));
  const decisive = established.filter((state) => state.basis === "decisive").length;
  const open = Math.max(metrics?.open_gaps ?? 0, core.length - established.length);
  const parts = [];
  if (core.length) {
    parts.push(`${established.length} of ${core.length} core question${core.length === 1 ? "" : "s"} established`);
    if (established.length) {
      parts.push(`${decisive} by the original record, ${established.length - decisive} by independent sources`);
    }
  }
  if (metrics?.sources_used) {
    parts.push(`${metrics.sources_used} sources cited (${metrics.independent_domains} independent)`);
  }
  if (open) parts.push(`${open} open question${open === 1 ? "" : "s"}`);
  return parts;
}

export function confidenceReasonsOf(run) {
  // The standing line already counts settled questions, including material disagreements.
  return (run?.metadata?.confidence_reasons || [])
    .filter((reason) => !/^\d+ of \d+ core questions are established\.$/.test(reason));
}

export function minutes(seconds) {
  const value = Math.max(0, Math.round((seconds || 0) / 60));
  return value < 1 ? "under a minute" : `${value} min`;
}

/** Whether a target is one subject's share of a question asked of several. */
export function plannedChild(plan, targetId) {
  const target = (plan?.targets || []).find((item) => item.id === targetId);
  return Boolean(target?.parent_id);
}

/**
 * Evidence-grounded research.
 *
 * The run is watched, not waited on: the poll lives in an effect keyed on the open run, so
 * a long deep run can be cancelled while history stays browsable. What comes back is shown
 * as what it is: the verified report, the evidence its claims actually cite, the sources
 * that were used or set aside (with the reason), and the plan with how well each research
 * question ended up covered.
 */
export default function Research({ onOpenNote, memoryEnabled, memoryIncognito }) {
  const [question, setQuestion] = useState("");
  const [depth, setDepth] = useState("standard");
  const [preview, setPreview] = useState(null);
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

  useEffect(() => {
    if (!activeId) return undefined;
    let alive = true;
    let handle = null;
    async function tick() {
      try {
        const job = await api.researchJob(activeId);
        if (!alive) return;
        setRun(job);
        if (TERMINAL.has(job.status)) {
          loadRuns();
          return;
        }
      } catch (err) {
        if (alive) setError(err.message);
        return;
      }
      handle = setTimeout(tick, 1500);
    }
    tick();
    return () => {
      alive = false;
      if (handle) clearTimeout(handle);
    };
  }, [activeId, loadRuns]);

  const live = Boolean(run && !TERMINAL.has(run.status));
  const sources = useMemo(() => run?.sources || [], [run]);
  const evidence = useMemo(() => run?.evidence || [], [run]);
  const claims = useMemo(() => run?.claims || [], [run]);
  const trace = run?.trace || null;
  const metrics = run?.metadata?.metrics || null;
  const legacy = Boolean(run?.legacy && Object.keys(run.legacy).length);

  const accepted = useMemo(() => evidence.filter((item) => item.status === "accepted"), [evidence]);
  const reviewed = useMemo(() => sources.filter(isReviewed), [sources]);
  const targetStates = useMemo(() => trace?.target_states || [], [trace]);
  const openQuestions = metrics?.open_gaps ?? 0;

  const [phaseLabel, latest] = useMemo(() => phaseOf(run), [run]);
  const completion = useMemo(() => completionOf(run), [run]);
  const standing = useMemo(() => standingOf(run, metrics, targetStates), [run, metrics, targetStates]);

  const stats = [
    ["Searches", (run?.generated_queries || []).length],
    ["Read", reviewed.length],
    ["Evidence", accepted.length],
    ["Open questions", openQuestions],
  ];

  const reportHtml = useMemo(() => (run?.report ? renderMarkdown(run.report) : ""), [run?.report]);

  async function previewPlan() {
    if (!question.trim()) return;
    setPlanning(true);
    setError("");
    try {
      setPreview(await api.researchPlan({ question, depth }));
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
      setSavedNote(await api.researchSaveToNote(run.id, { title: run.user_query, tags: ["research"] }));
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
    setPreview(null);
    setSavedNote(null);
    setError("");
    window.requestAnimationFrame(() => questionRef.current?.focus());
  }

  const linkedEvidence = useMemo(() => {
    const ids = new Set(claims.flatMap((claim) => claim.evidence_ids || []));
    return accepted.filter((item) => ids.has(item.id));
  }, [claims, accepted]);

  const tabs = [
    ["report", "Report", 0],
    ["evidence", "Evidence", linkedEvidence.length],
    ["sources", "Sources", reviewed.length],
    ["plan", "Plan", (run?.plan?.targets || []).filter((t) => !t.parent_id).length],
  ];

  return (
    <div className="rs">
      <section className="rs-main">
        <header className="rs-bar">
          <span className="rs-bar-title">Research</span>
          <div className="rs-bar-actions">
            {live && (
              <button className="rs-btn danger" type="button" onClick={cancel}>
                <Icon name="stop" />
                Stop
              </button>
            )}
            {run?.report && run.status === "completed" && (
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
                <p>Neo works out what needs to be established, searches and reads sources, keeps only evidence that answers the question, and reports what it could and could not verify. Preview the plan to see the research questions first.</p>
              </div>

              <textarea
                ref={questionRef}
                className="rs-question"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") start();
                }}
                placeholder="Ask anything worth researching: a comparison, a decision, how something works, what the evidence says."
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
                <div className="rs-composer-actions">
                  <button className="rs-btn" type="button" disabled={planning || !question.trim()} onClick={previewPlan}>
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

              {preview && <PlanView plan={preview} />}
            </div>
          ) : (
            <article className="rs-run">
              <h1 className="rs-question-title">{run.user_query}</h1>
              <div className="rs-run-facts">
                <span className="rs-chip">{words(run.depth)}</span>
                <span>{relativeTime(run.created_at)}</span>
                {completion && !live && (
                  <span className={`rs-chip ${completion[1]}`.trim()}>{completion[0]}</span>
                )}
                {run.metadata?.confidence && !live && (
                  <span className={`rs-chip ${run.metadata.confidence === "low" ? "warn" : ""}`.trim()}>
                    {run.metadata.confidence} confidence
                  </span>
                )}
              </div>
              {!live && run.metadata?.answer && <p className="rs-run-answer">{run.metadata.answer}</p>}
              {!live && standing.length > 0 && <p className="rs-run-standing">{standing.join(" · ")}</p>}
              {!live && (run.metadata?.confidence_reasons || []).length > 0 && (
                <p className="rs-run-standing">{confidenceReasonsOf(run).join(" ")}</p>
              )}

              {(live || run.status === "failed") && (
                <div className="rs-progress-card">
                  <div className="rs-progress-top">
                    <span className="rs-step">{phaseLabel}</span>
                    {latest?.ceiling_seconds > 0 && (
                      <span className="rs-pct">
                        {minutes(latest.elapsed_seconds)} of up to {minutes(latest.ceiling_seconds)}
                      </span>
                    )}
                  </div>
                  <p className="rs-progress-note">
                    {latest?.core_targets
                      ? `${latest.core_established} of ${latest.core_targets} core questions established`
                      : run.current_step || "Queued"}
                  </p>
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
                    {count > 0 && <em>{count}</em>}
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
                  <Placeholder icon={live ? "refresh" : "book"}>
                    {live ? "The report is written once claims are verified. Evidence and sources fill in as they arrive." : "No report was produced for this run."}
                  </Placeholder>
                )
              )}

              {tab === "evidence" && (
                legacy ? (
                  <Placeholder icon="quote">This run was recorded before structured evidence existed; only its report is available.</Placeholder>
                ) : (
                  <EvidenceView plan={run.plan} evidence={accepted} linked={linkedEvidence} claims={claims} sources={sources} live={live} states={targetStates} />
                )
              )}

              {tab === "sources" && (
                legacy ? (
                  <Placeholder icon="globe">This run was recorded before source classification existed.</Placeholder>
                ) : (
                  <SourcesView sources={sources} live={live} />
                )
              )}

              {tab === "plan" && (
                run.plan ? (
                  <PlanView plan={run.plan} trace={trace} completion={completion} stopDetail={run.metadata?.stop_detail} />
                ) : (
                  <Placeholder icon="layers">{live ? "Planning…" : "No structured plan was stored for this run."}</Placeholder>
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
                <span>{words(item.depth)}</span>
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

function Placeholder({ icon, children }) {
  return (
    <div className="rs-placeholder">
      <Icon name={icon} />
      <p>{children}</p>
    </div>
  );
}

/** Evidence the verified claims cite, grouped by the question it answers. */
function EvidenceView({ plan, evidence, linked, claims, sources, live, states }) {
  const [showAll, setShowAll] = useState(false);
  const items = showAll ? evidence : linked;
  const sourcesById = useMemo(() => new Map(sources.map((source) => [source.id, source])), [sources]);
  const stateById = useMemo(
    () => new Map((states || []).map((state) => [state.target_id, state])),
    [states],
  );
  const claimsByEvidence = useMemo(() => {
    const map = new Map();
    for (const claim of claims) {
      for (const id of claim.evidence_ids || []) {
        if (!map.has(id)) map.set(id, []);
        map.get(id).push(claim);
      }
    }
    return map;
  }, [claims]);

  const groups = useMemo(() => {
    const targets = plan?.targets || [];
    const known = new Set(targets.map((target) => target.id));
    const result = targets
      .map((target) => [target, items.filter((item) => item.question_id === target.id)])
      .filter(([, list]) => list.length);
    const other = items.filter((item) => !known.has(item.question_id));
    if (other.length) result.push([{ id: "other", text: "Other evidence" }, other]);
    return result;
  }, [plan, items]);

  if (!evidence.length) {
    return <Placeholder icon="quote">{live ? "Nothing accepted yet." : "No evidence passed the relevance and grounding checks."}</Placeholder>;
  }

  return (
    <div className="rs-groups">
      {linked.length !== evidence.length && (
        <button type="button" className="rs-btn" onClick={() => setShowAll((value) => !value)}>
          {showAll ? `Show only evidence cited by claims (${linked.length})` : `Show all accepted evidence (${evidence.length})`}
        </button>
      )}
      {!groups.length && <Placeholder icon="quote">No claim cites accepted evidence yet.</Placeholder>}
      {groups.map(([target, list]) => {
        const state = stateById.get(target.id);
        const standing = state ? SUFFICIENCY[state.sufficiency] : null;
        return (
          <section className="rs-group" key={target.id}>
            <h2>
              {target.text}
              <em>{list.length}</em>
            </h2>
            {standing && (
              <p className="rs-group-note">
                <span className={`rs-chip ${standing[1]}`.trim()}>{standing[0]}</span>
                {BASIS[state.basis] ? ` ${BASIS[state.basis]}` : ""}
                {ORIGIN[state.origin_status] ? ` · ${ORIGIN[state.origin_status]}` : ""}
                {(state.reasons || []).length ? ` · ${state.reasons.join("; ")}` : ""}
              </p>
            )}
            {list.map((item) => {
              const source = sourcesById.get(item.source_id);
              const citing = claimsByEvidence.get(item.id) || [];
              const provenance = PROVENANCE[source?.assessment?.provenance] || null;
              return (
                <figure className="rs-evidence" key={item.id}>
                  <blockquote>{item.excerpt}</blockquote>
                  {citing.length > 0 && (
                    <p className="rs-evidence-claims">Supports: {citing.map((claim) => claim.text).join(" · ")}</p>
                  )}
                  <figcaption>
                    {source?.url ? (
                      <a href={source.url} target="_blank" rel="noreferrer">
                        <Icon name="globe" />
                        {source.title || source.host || hostOf(source.url)}
                      </a>
                    ) : (
                      <span>{source?.title || "Source"}</span>
                    )}
                    {provenance && <span className={`rs-chip ${provenance[1]}`.trim()}>{provenance[0]}</span>}
                    {CLAIM_FIT[item.claim_fit] && <span className="rs-chip">{CLAIM_FIT[item.claim_fit]}</span>}
                    {(item.freshness_fit === "stale" || item.freshness_fit === "aging") && (
                      <span className="rs-chip warn">{item.freshness_fit}</span>
                    )}
                    {item.superseded_by && <span className="rs-chip warn">replaced by a newer statement</span>}
                    {item.contradiction_group && <span className="rs-chip warn">conflicting</span>}
                  </figcaption>
                </figure>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}

/** Sources split by what happened to them, with the reason for anything set aside. */
function SourcesView({ sources, live }) {
  const used = sources.filter((source) => source.used);
  const reviewedOnly = sources.filter((source) => !source.used && isReviewed(source) && !source.rejection_reason && !source.duplicate_of);
  const setAside = sources.filter((source) => !source.used && (source.rejection_reason || source.duplicate_of || !isReviewed(source)));

  if (!sources.length) {
    return <Placeholder icon="globe">{live ? "Still searching." : "No sources were read for this run."}</Placeholder>;
  }

  const sections = [
    ["Cited in the report", used],
    ["Read, not cited", reviewedOnly],
    ["Set aside", setAside],
  ].filter(([, list]) => list.length);

  return (
    <div className="rs-groups">
      {sections.map(([heading, list]) => (
        <section className="rs-group" key={heading}>
          <h2>
            {heading}
            <em>{list.length}</em>
          </h2>
          <div className="rs-sources">
            {list.map((source) => (
              <a
                className={`rs-source ${isReviewed(source) ? "" : "unread"}`.trim()}
                key={source.id}
                href={source.url}
                target="_blank"
                rel="noreferrer"
              >
                <span className="rs-source-body">
                  <strong>{source.title || source.host || source.url}</strong>
                  <span className="rs-source-meta">
                    <span className="rs-source-host">{source.host || hostOf(source.url)}</span>
                    {source.assessment?.role && <span className="rs-chip">{words(source.assessment.role)}</span>}
                    {PROVENANCE[source.assessment?.provenance] && (
                      <span className={`rs-chip ${PROVENANCE[source.assessment.provenance][1]}`.trim()}>
                        {PROVENANCE[source.assessment.provenance][0]}
                      </span>
                    )}
                    {source.published_date && <span>{shortDate(source.published_date)}</span>}
                    {source.duplicate_of && <span className="rs-chip warn">copy of another source</span>}
                    {source.rejection_reason && <span className="rs-chip warn">{words(source.rejection_reason)}</span>}
                    {source.http_status && !isReviewed(source) && <span className="rs-chip warn">HTTP {source.http_status}</span>}
                  </span>
                </span>
                <Icon name="external" />
              </a>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

/** The plan: what has to be established for each aspect, and what research did about it. */
function PlanView({ plan, trace, completion, stopDetail }) {
  const understanding = plan?.understanding;
  const decomposition = plan?.decomposition;
  if (!understanding || !decomposition) {
    return <Placeholder icon="layers">This run predates structured research plans.</Placeholder>;
  }
  const entities = new Map(understanding.entities.map((entity) => [entity.id, entity.name]));
  const targets = plan.targets || [];
  const states = new Map((trace?.target_states || []).map((state) => [state.target_id, state]));
  const actions = trace?.actions || [];
  const steps = [...new Set(actions.map((action) => action.step))].sort((a, b) => a - b);

  function standingOf(target) {
    const state = states.get(target.id);
    if (!state) return null;
    const label = SUFFICIENCY[state.sufficiency];
    return label ? [label, state] : null;
  }

  return (
    <section className="rs-plan">
      <div className="rs-plan-objective">
        <h2>Task</h2>
        <p>
          {words(understanding.task_type)}
          {understanding.answer_shape ? ` · ${understanding.answer_shape}` : ""}
        </p>
        {completion && <p>{completion[0]}{stopDetail ? ` (${stopDetail})` : ""}</p>}
      </div>

      {understanding.entities.length > 0 && (
        <div className="rs-plan-block">
          <h2>Subjects</h2>
          <div className="rs-chiprow">
            {understanding.entities.map((entity) => (
              <span className="rs-chip" key={entity.id}>{entity.name}{entity.type ? ` · ${entity.type}` : ""}</span>
            ))}
          </div>
        </div>
      )}

      {(understanding.constraints.length > 0 || understanding.decision_criteria.length > 0) && (
        <div className="rs-plan-block">
          <h2>Constraints and criteria</h2>
          <ul className="rs-plan-list">
            {understanding.constraints.map((item) => <li key={item.id}>{item.text}</li>)}
            {understanding.decision_criteria.map((item) => <li key={item.id}>Decide on: {item.text}</li>)}
          </ul>
        </div>
      )}

      {decomposition.dimensions.map((dimension) => {
        const own = targets.filter((target) => target.dimension_id === dimension.id && !target.parent_id);
        if (!own.length) return null;
        return (
          <div className="rs-plan-block" key={dimension.id}>
            <h2>{dimension.name}</h2>
            <ol className="rs-plan-list">
              {own.map((target) => {
                const standing = standingOf(target);
                const children = targets.filter((item) => item.parent_id === target.id);
                return (
                  <li key={target.id}>
                    {target.text}
                    <span className="rs-chiprow">
                      <span className="rs-chip">{target.tier}</span>
                      <span className="rs-chip">{words(target.profile?.authority_kind)} evidence</span>
                      {target.profile?.recency?.kind && target.profile.recency.kind !== "timeless" && (
                        <span className="rs-chip">{words(target.profile.recency.kind)}</span>
                      )}
                      {standing && (
                        <span className={`rs-chip ${standing[0][1]}`.trim()}>
                          {standing[0][0]}
                          {BASIS[standing[1].basis] ? ` ${BASIS[standing[1].basis]}` : ""}
                        </span>
                      )}
                      {children.map((child) => {
                        const childStanding = standingOf(child);
                        const name = entities.get(child.subject_ids?.[0]) || child.subject_ids?.[0];
                        if (!childStanding) return null;
                        return (
                          <span
                            key={child.id}
                            className={`rs-chip ${childStanding[0][1]}`.trim()}
                          >
                            {name}: {childStanding[0][0].toLowerCase()}
                          </span>
                        );
                      })}
                    </span>
                  </li>
                );
              })}
            </ol>
          </div>
        );
      })}

      {actions.length > 0 && (
        <div className="rs-plan-block">
          <h2>What research did</h2>
          {steps.map((step) => (
            <div className="rs-plan-queries" key={step}>
              {steps.length > 1 && <span className="rs-chip">step {step}</span>}
              {actions.filter((action) => action.step === step).map((action) => (
                <code key={action.id}>
                  {action.query || "read a record the evidence cited"}
                  {` · ${words(action.intent)}`}
                  {action.vertical && action.vertical !== "web" ? ` · ${action.vertical}` : ""}
                </code>
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

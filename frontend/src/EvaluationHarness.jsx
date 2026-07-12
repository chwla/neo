import { useEffect, useState } from "react";
import { api } from "./api.js";

export default function EvaluationHarness() {
  const [suites, setSuites] = useState([]); const [runs, setRuns] = useState([]); const [selected, setSelected] = useState(null); const [detail, setDetail] = useState(null); const [error, setError] = useState("");
  const refresh = async () => { const [a,b] = await Promise.all([api.evalSuites(), api.evalRuns()]); setSuites(a.suites || []); setRuns(b.runs || []); };
  useEffect(() => { refresh().catch((e) => setError(e.message)); }, []);
  const run = async (suite) => { try { const value = await api.runEval(suite.id); setDetail(value.report); setSelected(value.run); await refresh(); } catch (e) { setError(e.message); } };
  const baseline = async () => { if (!selected) return; await api.setEvalBaseline(selected.id, "stable"); setDetail(await api.evalReport(selected.id)); };
  return <div className="provider-runtime">
    <div className="provider-runtime-header"><div><h2>Evaluation Harness</h2><p>Deterministic, offline fixture scoring for agents, research, memory, providers, context, and safety.</p></div></div>
    {error && <div className="neo-error">{error}</div>}
    <section><h3>Built-in and custom suites</h3><div className="provider-runtime-grid">{suites.map((suite) => <article className="provider-card" key={suite.id}><strong>{suite.name}</strong><p>{suite.description}</p><button onClick={() => run(suite)}>Run fixture suite</button></article>)}</div></section>
    <section><h3>Run history</h3>{runs.length === 0 ? <p>No evaluation runs yet.</p> : <div className="provider-table">{runs.map((run) => <button className="provider-row" key={run.id} onClick={async () => { setSelected(run); setDetail(await api.evalReport(run.id)); }}><span>{run.status}</span><span>Score {run.overall_score ?? "—"}</span><span>{run.hard_failure_count} hard failures</span></button>)}</div>}</section>
    {detail && <section><div className="provider-runtime-header"><h3>Evaluation report</h3><button onClick={baseline}>Set baseline</button></div><p>Overall score: {detail.run?.overall_score ?? detail.summary?.overall_score ?? selected?.overall_score ?? "—"}</p><h4>Case results</h4><div className="provider-table">{(detail.case_results || []).map((item) => <div className="provider-row" key={item.id}><span>{item.name}: {item.score}</span><span>{item.status}</span><span>{item.hard_failures?.join(", ") || "No hard failures"}</span></div>)}</div><h4>Metric breakdown</h4><pre>{JSON.stringify(detail.metric_breakdown || {}, null, 2)}</pre><h4>Hard failures</h4><p>{(detail.hard_failures || []).join(", ") || "None"}</p><h4>Baseline comparison</h4><pre>{JSON.stringify(detail.baseline_comparison || {}, null, 2)}</pre></section>}
  </div>;
}

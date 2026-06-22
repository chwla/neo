import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";

const DEPTH_OPTIONS = [
  { value: "quick", label: "Quick", desc: "3-5 queries, ~1 min" },
  { value: "standard", label: "Standard", desc: "5-8 queries, ~3 min" },
  { value: "deep", label: "Deep", desc: "8-12 queries, ~5 min" },
];

const STATUS_LABELS = {
  queued: "Queued",
  planning: "Planning",
  searching: "Searching",
  fetching: "Fetching",
  extracting: "Extracting",
  synthesizing: "Synthesizing",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function renderMarkdown(text) {
  if (!text) return "";

  let cleaned = text
    .replace(/"\s*target="_blank"[^"]*"?/g, "")
    .replace(/\s*rel="noopener"/g, "")
    .replace(/<a\s+href="([^"]*)"[^>]*>([^<]*)<\/a>/g, "$2")
    .replace(/\(Source:\s*\)/g, "")
    .replace(/Source:\s*\)/g, "")
    .replace(/Sources?:\s*,/g, "")
    .replace(/\(\s*\)/g, "")
    .replace(/\[\s*\]/g, "")
    .replace(/\[,\s*\]/g, "");

  let html = cleaned
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  html = html.replace(/^### (.+)$/gm, '<h3 class="report-h3">$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2 class="report-h2">$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1 class="report-h1">$1</h1>');

  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");

  const urlPlaceholders = [];
  html = html.replace(
    /\[(\d+)\]\s+(.+?)\s+—\s+(https?:\/\/\S+)/g,
    (match, num, title, url) => {
      const idx = urlPlaceholders.length;
      urlPlaceholders.push(`[${num}] ${title} — <a href="${url}" target="_blank" rel="noopener" class="report-link">${url}</a>`);
      return `%%URL_PLACEHOLDER_${idx}%%`;
    }
  );

  html = html.replace(
    /(https?:\/\/[^\s<%%]+)/g,
    (match, url) => {
      const idx = urlPlaceholders.length;
      urlPlaceholders.push(`<a href="${url}" target="_blank" rel="noopener" class="report-link">${url}</a>`);
      return `%%URL_PLACEHOLDER_${idx}%%`;
    }
  );

  html = html.replace(/^&gt;\s*(.+)$/gm, '<blockquote class="report-blockquote">$1</blockquote>');
  html = html.replace(/^[-*]\s+(.+)$/gm, '<li class="report-li">$1</li>');
  html = html.replace(/((?:<li[^>]*>.*<\/li>\n?)+)/g, '<ul class="report-ul">$1</ul>');

  html = html.replace(/\n{2,}/g, "</p><p>");
  html = "<p>" + html + "</p>";
  html = html.replace(/<p>\s*<(h[123]|ul|blockquote)/g, "<$1");
  html = html.replace(/<\/(h[123]|ul|blockquote)>\s*<\/p>/g, "</$1>");
  html = html.replace(/<p>\s*<\/p>/g, "");

  for (let i = 0; i < urlPlaceholders.length; i++) {
    html = html.replace(`%%URL_PLACEHOLDER_${i}%%`, urlPlaceholders[i]);
  }

  return html;
}

export default function Research({ onBack }) {
  const [query, setQuery] = useState("");
  const [depth, setDepth] = useState("standard");
  const [activeJobId, setActiveJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [report, setReport] = useState(null);
  const [fullJob, setFullJob] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const sseRef = useRef(null);
  const pollRef = useRef(null);

  const loadJobs = useCallback(async () => {
    try {
      const data = await api.researchList(20);
      setJobs(data.jobs || []);
    } catch (err) {
      console.error("Failed to load research jobs:", err);
    }
  }, []);

  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  const stopTracking = useCallback(() => {
    if (sseRef.current) {
      sseRef.current.close();
      sseRef.current = null;
    }
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => {
    return stopTracking;
  }, [stopTracking]);

  const startSSE = useCallback((jobId) => {
    stopTracking();
    const url = api.researchEvents(jobId);
    const source = new EventSource(url);
    sseRef.current = source;

    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "progress") {
          setJobStatus(data);
        }
        if (data.type === "complete") {
          setJobStatus((prev) => ({ ...prev, status: data.status }));
          source.close();
          sseRef.current = null;
          if (data.has_report || data.status === "completed") {
            loadReport(jobId);
          }
          loadJobs();
        }
      } catch {}
    };

    source.onerror = () => {
      source.close();
      sseRef.current = null;
      startPolling(jobId);
    };
  }, [stopTracking, loadJobs]);

  const startPolling = useCallback((jobId) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const status = await api.researchStatus(jobId);
        setJobStatus(status);
        if (TERMINAL_STATUSES.has(status.status)) {
          clearInterval(pollRef.current);
          pollRef.current = null;
          if (status.status === "completed") {
            loadReport(jobId);
          }
          loadJobs();
        }
      } catch {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }, 2000);
  }, [loadJobs]);

  async function loadReport(jobId) {
    try {
      const data = await api.researchReport(jobId);
      setReport(data);
    } catch (err) {
      setError(`Failed to load report: ${err.message}`);
    }
  }

  async function loadFullJob(jobId) {
    try {
      const data = await api.researchJob(jobId);
      setFullJob(data);
      return data;
    } catch (err) {
      setError(`Failed to load job: ${err.message}`);
      return null;
    }
  }

  async function handleStart() {
    if (!query.trim() || starting) return;
    setError("");
    setReport(null);
    setFullJob(null);
    setJobStatus(null);
    setStarting(true);

    try {
      const result = await api.researchStart({
        query: query.trim(),
        depth,
      });
      setActiveJobId(result.job_id);
      setJobStatus({ status: "queued", progress_percent: 0, current_step: "Queued" });
      startSSE(result.job_id);
    } catch (err) {
      setError(err.message || "Failed to start research");
    } finally {
      setStarting(false);
    }
  }

  async function handleCancel() {
    if (!activeJobId) return;
    try {
      await api.researchCancel(activeJobId);
      setJobStatus((prev) => ({ ...prev, status: "cancelled", current_step: "Cancelled" }));
      stopTracking();
      loadJobs();
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleOpenJob(job) {
    setError("");
    setReport(null);
    setFullJob(null);
    setActiveJobId(job.id);
    setQuery(job.user_query || "");

    if (job.status === "completed" && job.has_report) {
      setJobStatus({ status: "completed", progress_percent: 100, current_step: "Completed" });
      await loadReport(job.id);
      await loadFullJob(job.id);
    } else if (TERMINAL_STATUSES.has(job.status)) {
      const data = await loadFullJob(job.id);
      if (data) {
        setJobStatus({
          status: data.status,
          progress_percent: data.progress_percent,
          current_step: data.current_step || data.status,
        });
      }
    } else {
      setJobStatus({
        status: job.status,
        progress_percent: job.progress_percent || 0,
        current_step: job.current_step || job.status,
      });
      startSSE(job.id);
    }
  }

  const isRunning = jobStatus && !TERMINAL_STATUSES.has(jobStatus.status);
  const isCompleted = jobStatus?.status === "completed";
  const isFailed = jobStatus?.status === "failed";
  const isCancelled = jobStatus?.status === "cancelled";

  return (
    <div className="research-layout">
      <div className="research-main">
        <div className="research-header">
          <button className="research-back" onClick={onBack} type="button">&larr; Chat</button>
          <h2 className="research-title">Research Mode</h2>
        </div>

        <div className="research-input-area">
          <textarea
            className="research-query"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="What do you want to research? e.g. &quot;Research Tavily vs SearXNG for Neo&quot;"
            rows={2}
            disabled={isRunning}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleStart();
              }
            }}
          />

          <div className="research-controls">
            <div className="research-depth-group">
              {DEPTH_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  className={`research-depth-btn ${depth === opt.value ? "active" : ""}`}
                  onClick={() => setDepth(opt.value)}
                  disabled={isRunning}
                  title={opt.desc}
                  type="button"
                >
                  {opt.label}
                </button>
              ))}
            </div>

            <div className="research-actions">
              {isRunning ? (
                <button className="research-cancel-btn" onClick={handleCancel} type="button">
                  Cancel
                </button>
              ) : (
                <button
                  className="research-start-btn"
                  onClick={handleStart}
                  disabled={!query.trim() || starting}
                  type="button"
                >
                  {starting ? "Starting..." : "Start Research"}
                </button>
              )}
            </div>
          </div>
        </div>

        {error && <div className="research-error">{error}</div>}

        {jobStatus && !isCompleted && !isFailed && !isCancelled && (
          <ProgressPanel status={jobStatus} />
        )}

        {isCompleted && report && (
          <ReportViewer report={report} job={fullJob} />
        )}

        {isFailed && fullJob && (
          <div className="research-failed-panel">
            <div className="research-status-badge failed">Failed</div>
            <p className="research-error-detail">{fullJob.error || "Research failed with an unknown error."}</p>
            {fullJob.metadata && (
              <MetadataBar metadata={fullJob.metadata} />
            )}
          </div>
        )}

        {isCancelled && (
          <div className="research-cancelled-panel">
            <div className="research-status-badge cancelled">Cancelled</div>
            <p>Research was cancelled. Partial data may have been saved.</p>
            {fullJob?.metadata && <MetadataBar metadata={fullJob.metadata} />}
          </div>
        )}
      </div>

      <aside className="research-sidebar">
        <div className="research-sidebar-header">
          <h3 className="research-sidebar-title">Recent Research</h3>
          {jobs.length > 0 && (
            <button
              className="research-clear-btn"
              onClick={async () => {
                try {
                  await api.researchClear();
                  setJobs([]);
                  setActiveJobId(null);
                  setJobStatus(null);
                  setReport(null);
                  setFullJob(null);
                } catch (err) {
                  setError(err.message);
                }
              }}
              type="button"
              title="Clear all research jobs"
            >
              Clear All
            </button>
          )}
        </div>
        {jobs.length === 0 ? (
          <p className="research-sidebar-empty">No research jobs yet.</p>
        ) : (
          <div className="research-jobs-list">
            {jobs.map((job) => (
              <button
                key={job.id}
                className={`research-job-item ${job.id === activeJobId ? "active" : ""}`}
                onClick={() => handleOpenJob(job)}
                type="button"
              >
                <span className="research-job-query">{job.user_query}</span>
                <span className="research-job-meta">
                  <span className={`research-status-dot ${job.status}`} />
                  {STATUS_LABELS[job.status] || job.status}
                  {" · "}
                  {job.depth}
                  {job.created_at && (" · " + formatTime(job.created_at))}
                </span>
              </button>
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}

function ProgressPanel({ status }) {
  const pct = status.progress_percent || 0;
  const step = status.current_step || status.status || "";
  const label = STATUS_LABELS[status.status] || status.status;

  return (
    <div className="research-progress">
      <div className="research-progress-header">
        <span className={`research-status-badge ${status.status}`}>{label}</span>
        <span className="research-progress-pct">{pct}%</span>
      </div>

      <div className="research-progress-bar-track">
        <div className="research-progress-bar-fill" style={{ width: `${pct}%` }} />
      </div>

      <div className="research-progress-step">{step}</div>

      {(status.queries_done > 0 || status.sources_found > 0) && (
        <div className="research-progress-counts">
          {status.queries_done > 0 && <span>Queries: {status.queries_done}</span>}
          {status.sources_found > 0 && <span>Sources: {status.sources_found}</span>}
          {status.sources_fetched > 0 && <span>Fetched: {status.sources_fetched}</span>}
          {status.evidence_chunks > 0 && <span>Evidence: {status.evidence_chunks}</span>}
        </div>
      )}
    </div>
  );
}

function ReportViewer({ report, job }) {
  const html = renderMarkdown(report?.report || "");
  const meta = job?.metadata || report?.metadata || {};

  return (
    <div className="research-report">
      <div className="research-report-meta">
        <span>Sources: {report?.sources_count ?? meta.fetched_sources ?? "?"}/{meta.total_sources ?? "?"}</span>
        <span>Evidence: {report?.evidence_count ?? meta.evidence_chunks ?? "?"}</span>
        {job?.depth && <span>Depth: {job.depth}</span>}
        {job?.created_at && <span>{formatTime(job.created_at)}</span>}
      </div>

      <div
        className="research-report-body"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}

function MetadataBar({ metadata }) {
  return (
    <div className="research-meta-bar">
      {metadata.fetched_sources != null && <span>Fetched: {metadata.fetched_sources}/{metadata.total_sources}</span>}
      {metadata.evidence_chunks != null && <span>Evidence: {metadata.evidence_chunks}</span>}
      {metadata.queries_run != null && <span>Queries: {metadata.queries_run}</span>}
      {metadata.memory_used?.length > 0 && <span>Memory: {metadata.memory_used.join(", ")}</span>}
      {metadata.fetch_summary && (
        <span>Fetch: {metadata.fetch_summary.success} ok, {metadata.fetch_summary.failed} failed</span>
      )}
    </div>
  );
}

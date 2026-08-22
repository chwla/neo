import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import { renderMessageHtml } from "./chatPresentation.js";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

const STOP_REASON_COPY = {
  verified_complete: {
    label: "Completed and verified",
    tone: "ok",
    detail: "The agent's work was checked by a tool that passed.",
  },
  unverified_complete: {
    label: "Completed, not verified",
    tone: "warn",
    detail: "The agent reports it finished, but nothing confirmed the result. Review before trusting it.",
  },
  blocked: {
    label: "Blocked",
    tone: "warn",
    detail: "The agent needs something it could not get on its own.",
  },
  failed: { label: "Failed", tone: "bad", detail: "The run stopped on an error." },
  cancelled: { label: "Cancelled", tone: "muted", detail: "You stopped this run." },
  budget_exhausted: {
    label: "Stopped at its limit",
    tone: "warn",
    detail: "The agent hit a safety ceiling before finishing.",
  },
};

const MODES = [
  { id: "plan", label: "Plan", hint: "Read and propose only. Nothing is changed." },
  { id: "normal", label: "Normal", hint: "Reads run freely; changes ask first." },
  { id: "auto", label: "Auto", hint: "Changes and commands run without asking." },
];

function statusLabel(status) {
  return String(status || "").replace(/_/g, " ");
}

function ToolCard({ event }) {
  const [open, setOpen] = useState(false);
  const failed = event.status === "error" || event.status === "denied";
  const tone = failed ? "bad" : event.status === "proposed" ? "warn" : "ok";
  return (
    <div className={`agent-tool-card ${tone}`}>
      <button type="button" className="agent-tool-head" onClick={() => setOpen((value) => !value)}>
        <span className="agent-tool-name">{event.name}</span>
        <span className="agent-tool-summary">{event.summary || ""}</span>
        <span className={`agent-tool-status ${tone}`}>{statusLabel(event.status || "running")}</span>
        {event.duration_ms ? <span className="agent-tool-time">{event.duration_ms}ms</span> : null}
      </button>
      {open ? (
        <div className="agent-tool-body">
          {event.arguments ? <pre className="agent-tool-args">{JSON.stringify(event.arguments, null, 2)}</pre> : null}
          {event.content ? <pre className="agent-tool-output">{event.content}</pre> : null}
          {event.error ? <div className="agent-tool-error">{event.error}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

function ApprovalCard({ approval, busy, onDecide }) {
  const [scope, setScope] = useState("");
  const pathArgument = approval.arguments?.path;
  const suggestion = useMemo(() => {
    if (typeof pathArgument !== "string" || !pathArgument.includes("/")) return "";
    return `${pathArgument.slice(0, pathArgument.lastIndexOf("/") + 1)}`;
  }, [pathArgument]);

  return (
    <div className="agent-approval" role="alertdialog" aria-label="Approval required">
      <div className="agent-approval-head">
        <strong>Approval needed</strong>
        <span>{approval.reason}</span>
      </div>
      <div className="agent-approval-summary">{approval.summary || approval.tool_name}</div>
      <pre className="agent-approval-args">{JSON.stringify(approval.arguments, null, 2)}</pre>
      {approval.grantable ? (
        <label className="agent-approval-scope">
          <span>Allow always for paths starting with</span>
          <input
            value={scope}
            placeholder={suggestion || "app/services/"}
            onChange={(event) => setScope(event.target.value)}
            aria-label="Grant path prefix"
          />
        </label>
      ) : null}
      <div className="agent-approval-actions">
        <button type="button" className="neo-button primary" disabled={busy} onClick={() => onDecide("allow_once")}>
          Allow once
        </button>
        {approval.grantable ? (
          <button
            type="button"
            className="neo-button secondary"
            disabled={busy}
            onClick={() =>
              onDecide("allow_always", {
                kind: "path_prefix",
                argument: "path",
                value: (scope || suggestion).trim(),
              })
            }
          >
            Allow always in this folder
          </button>
        ) : null}
        <button type="button" className="neo-button" disabled={busy} onClick={() => onDecide("reject")}>
          Reject
        </button>
      </div>
      {approval.grantable ? null : (
        <p className="agent-approval-note">
          This action cannot be granted for the whole session; it is approved one call at a time.
        </p>
      )}
    </div>
  );
}

function TodoPanel({ items }) {
  if (!items?.length) return null;
  const done = items.filter((item) => item.status === "completed").length;
  return (
    <aside className="agent-todo" aria-label="Agent checklist">
      <div className="agent-todo-head">
        Checklist <span>{done}/{items.length}</span>
      </div>
      <ul>
        {items.map((item, index) => (
          <li key={`${item.title}-${index}`} className={`agent-todo-item ${item.status}`}>
            <span className="agent-todo-mark" aria-hidden="true">
              {item.status === "completed" ? "✓" : item.status === "in_progress" ? "◐" : "○"}
            </span>
            {item.title}
          </li>
        ))}
      </ul>
    </aside>
  );
}

function DeliveryPanel({ sessionId, delivery, onMessage }) {
  const [busy, setBusy] = useState(false);
  const [patch, setPatch] = useState("");
  if (!delivery || (!delivery.deliverable?.length && !delivery.blocked?.length)) return null;

  // The server decides which of the two shapes this is; the browser never
  // infers it from a path, because only the server knows the repository origin.
  const uploaded = delivery.mode === "download";

  async function run(mode) {
    setBusy(true);
    try {
      const result = await api.deliverAgentChanges(sessionId, { mode });
      if (mode === "patch") setPatch(result.patch || "(no changes)");
      else onMessage(`Wrote ${result.written?.length ?? 0} file(s) into your repository.`);
    } catch (error) {
      onMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function download(scope) {
    setBusy(true);
    try {
      const { blob, filename } = await api.downloadAgentChanges(sessionId, scope);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Revoking immediately can cancel the download in some browsers.
      window.setTimeout(() => URL.revokeObjectURL(url), 10000);
      onMessage(`Downloaded ${filename}.`);
    } catch (error) {
      onMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="agent-delivery">
      <div className="agent-delivery-head">
        {uploaded ? "Take these changes" : "Deliver to your repository"}
      </div>
      {uploaded ? (
        <p className="agent-delivery-note">
          This repository was uploaded, so Neo has no folder on your machine to write
          into. Download the files and put them where you want them.
        </p>
      ) : null}
      {delivery.deliverable?.length ? (
        <ul className="agent-delivery-files">
          {delivery.deliverable.map((item) => (
            <li key={item.path}>
              <code>{item.path}</code> — {item.status}
            </li>
          ))}
        </ul>
      ) : (
        <p className="agent-delivery-empty">No files are ready to deliver.</p>
      )}
      {delivery.blocked?.length ? (
        <ul className="agent-delivery-blocked">
          {delivery.blocked.map((item) => (
            <li key={item.path}>
              <code>{item.path}</code> — {item.reason}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="agent-delivery-actions">
        <button type="button" className="neo-button secondary" disabled={busy} onClick={() => run("patch")}>
          View diff
        </button>
        {uploaded ? (
          <>
            <button type="button" className="neo-button" disabled={busy || !delivery.deliverable?.length} onClick={() => download("changes")}>
              Download changed files
            </button>
            <button type="button" className="neo-button secondary" disabled={busy} onClick={() => download("workspace")}>
              Download workspace
            </button>
          </>
        ) : (
          <button type="button" className="neo-button" disabled={busy || !delivery.deliverable?.length} onClick={() => run("working_tree")}>
            Apply changes
          </button>
        )}
        <button type="button" className="neo-button secondary" disabled={busy} onClick={() => { setPatch(""); onMessage("Changes left in Neo's managed copy."); }}>
          Discard
        </button>
      </div>
      {patch ? <pre className="agent-delivery-patch">{patch}</pre> : null}
    </section>
  );
}

/**
 * A transcript of what the agent actually did, not a checklist of what it planned.
 *
 * The event log is the source of truth: it is append-only with a monotonic
 * sequence, so a reload reconnects with the last sequence it saw rather than
 * losing a run that is still executing on the server.
 */
export default function AgentSession({ sessionId, onClose, onMessage }) {
  const [session, setSession] = useState(null);
  const [entries, setEntries] = useState([]);
  const [approval, setApproval] = useState(null);
  const [delivery, setDelivery] = useState(null);
  const [busy, setBusy] = useState(false);
  const [followUp, setFollowUp] = useState("");
  const [error, setError] = useState("");
  const cursorRef = useRef(0);
  const bottomRef = useRef(null);

  const refresh = useCallback(async () => {
    const detail = await api.agentSession(sessionId);
    setSession(detail.session);
    setApproval(detail.pending_approval || null);
    setDelivery(detail.delivery || null);
    return detail;
  }, [sessionId]);

  const applyEvent = useCallback((event) => {
    cursorRef.current = Math.max(cursorRef.current, event.seq || 0);
    setEntries((current) => {
      if (event.type === "chunk" && event.content) {
        return [...current, { kind: "text", id: event.seq, content: event.content }];
      }
      if (event.type === "tool.call") {
        return [...current, { kind: "tool", id: event.call_id, ...event }];
      }
      if (event.type === "tool.result") {
        return current.map((entry) =>
          entry.kind === "tool" && entry.id === event.call_id ? { ...entry, ...event } : entry,
        );
      }
      return current;
    });
    if (event.type === "todo.updated") {
      setSession((current) => (current ? { ...current, todo: event.items || [] } : current));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function connect() {
      try {
        await refresh();
        if (cancelled) return;
        // The log is durable and starts at sequence 0, so streaming from the top
        // replays the whole run. That is why a reload shows history without a
        // separate fetch, and why a dropped connection resumes rather than restarts.
        setEntries([]);
        cursorRef.current = 0;
        while (!cancelled) {
          await api.streamAgentSession(sessionId, cursorRef.current, applyEvent, controller.signal);
          if (cancelled) return;
          const latest = await refresh();
          if (TERMINAL.has(latest.session.status)) return;
        }
      } catch (streamError) {
        if (!cancelled && streamError?.name !== "AbortError") setError(streamError.message);
      }
    }

    connect();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [sessionId, refresh, applyEvent]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [entries.length, approval]);

  async function decide(decision, predicate) {
    if (!approval) return;
    setBusy(true);
    setError("");
    try {
      await api.decideAgentApproval(sessionId, approval.id, decision, predicate);
      setApproval(null);
      await refresh();
    } catch (decisionError) {
      setError(decisionError.message);
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    try {
      await api.cancelAgentSession(sessionId);
      await refresh();
    } catch (stopError) {
      setError(stopError.message);
    } finally {
      setBusy(false);
    }
  }

  async function changeMode(mode) {
    try {
      await api.setAgentSessionMode(sessionId, mode);
      await refresh();
    } catch (modeError) {
      setError(modeError.message);
    }
  }

  async function send(event) {
    event.preventDefault();
    const content = followUp.trim();
    if (!content) return;
    setFollowUp("");
    setEntries((current) => [...current, { kind: "user", id: `u-${Date.now()}`, content }]);
    try {
      await api.sendAgentMessage(sessionId, content);
    } catch (sendError) {
      setError(sendError.message);
    }
  }

  if (!session) {
    return <div className="agent-session loading">Loading run…</div>;
  }

  const active = !TERMINAL.has(session.status);
  const outcome = session.stop_reason ? STOP_REASON_COPY[session.stop_reason] : null;

  return (
    <section className="agent-session">
      <header className="agent-session-head">
        <div className="agent-session-title">
          <button type="button" className="agent-session-back" onClick={onClose}>
            ← Back
          </button>
          <strong>{session.title}</strong>
          <span className={`agent-session-status ${session.status}`}>{statusLabel(session.status)}</span>
        </div>
        <div className="agent-session-controls">
          <div className="agent-mode-switch" role="group" aria-label="Permission mode">
            {MODES.map((mode) => (
              <button
                key={mode.id}
                type="button"
                title={mode.hint}
                disabled={!active}
                className={session.mode === mode.id ? "active" : ""}
                onClick={() => changeMode(mode.id)}
              >
                {mode.label}
              </button>
            ))}
          </div>
          {active ? (
            <button type="button" className="agent-stop" onClick={stop} disabled={busy}>
              Stop
            </button>
          ) : (
            <button
              type="button"
              className="neo-button secondary"
              onClick={async () => {
                try {
                  await api.exportAgentSession(sessionId, "note");
                  onMessage?.("Saved to a note.");
                } catch (exportError) {
                  setError(exportError.message);
                }
              }}
            >
              Save to note
            </button>
          )}
        </div>
      </header>

      <div className="agent-session-body">
        <div className="agent-transcript">
          <div className="agent-objective">{session.objective}</div>
          {entries.map((entry) => {
            if (entry.kind === "tool") return <ToolCard key={entry.id} event={entry} />;
            if (entry.kind === "user") {
              return (
                <div key={entry.id} className="agent-user-note">
                  {entry.content}
                </div>
              );
            }
            return (
              <div
                key={entry.id}
                className="agent-text"
                dangerouslySetInnerHTML={{ __html: renderMessageHtml(entry.content) }}
              />
            );
          })}

          {approval ? <ApprovalCard approval={approval} busy={busy} onDecide={decide} /> : null}

          {outcome ? (
            <div className={`agent-outcome ${outcome.tone}`} aria-live="polite">
              <strong>{outcome.label}</strong>
              <p>{outcome.detail}</p>
              {session.summary ? (
                <div
                  className="agent-summary"
                  dangerouslySetInnerHTML={{ __html: renderMessageHtml(session.summary) }}
                />
              ) : null}
              {session.evidence?.length ? (
                <ul className="agent-evidence">
                  {session.evidence.map((item, index) => (
                    <li key={index} className={item.passed ? "ok" : "bad"}>
                      {item.kind}: {item.passed ? "passed" : "failed"} — {item.detail}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}

          <DeliveryPanel sessionId={sessionId} delivery={delivery} onMessage={onMessage} />
          {error ? <div className="agent-session-error">{error}</div> : null}
          <div ref={bottomRef} />
        </div>

        <TodoPanel items={session.todo} />
      </div>

      <form className="agent-followup" onSubmit={send}>
        <input
          value={followUp}
          onChange={(event) => setFollowUp(event.target.value)}
          placeholder={active ? "Steer the agent…" : "This run has finished"}
          disabled={!active}
          aria-label="Send a message to the agent"
        />
        <button type="submit" className="neo-button primary" disabled={!active || !followUp.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}

export { ApprovalCard, ToolCard, TodoPanel, STOP_REASON_COPY };

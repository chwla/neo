import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import {
  formatDuration,
  formatMessageTime,
  formatResponseKind,
  formatTokens,
  renderMessageHtml,
} from "./chatPresentation.js";
import { MessageActionsMenu } from "./MessageActionsMenu.jsx";
import { PaperclipIcon } from "./icons.jsx";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
//: The agent is mid-step in these. `waiting_approval` is deliberately not one of
//: them: the run is open but the agent is waiting on you, so the composer offers
//: send rather than stop, exactly as chat does between answers.
const WORKING = new Set(["queued", "running"]);

const MODES = [
  { id: "plan", label: "Plan", hint: "Read and propose only. Nothing is changed." },
  { id: "normal", label: "Normal", hint: "Reads run freely; changes ask first." },
  { id: "auto", label: "Auto", hint: "Changes and commands run without asking." },
];

function statusLabel(status) {
  return String(status || "").replace(/_/g, " ");
}

/**
 * The model streams its narration in chunks. One bubble per chunk would shred a
 * paragraph across a dozen bubbles, so consecutive text runs are merged and only
 * a tool call breaks the run -- which is what the chat transcript would look
 * like if the agent were answering there.
 */
function groupEntries(entries) {
  const grouped = [];
  for (const entry of entries) {
    const previous = grouped[grouped.length - 1];
    if (entry.kind === "text" && previous?.kind === "text") {
      // Two turns merged into one bubble cost the sum of both, and the model
      // that answered last is the one the bubble is showing.
      grouped[grouped.length - 1] = {
        ...previous,
        ...entry,
        content: previous.content + entry.content,
        total_tokens: (previous.total_tokens || 0) + (entry.total_tokens || 0) || undefined,
        duration_ms: (previous.duration_ms || 0) + (entry.duration_ms || 0) || undefined,
      };
      continue;
    }
    grouped.push(entry);
  }
  return grouped;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    document.body.removeChild(area);
  }
}

function lastAssistantEntry(steps) {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (steps[index].kind === "text" && steps[index].content?.trim()) return steps[index];
  }
  return null;
}

function AgentBubble({ role, text, html, entry, onCopy, extraActions }) {
  const isUser = role === "user";
  const sentAt = formatMessageTime(entry?.created_at);
  const metadataItems = isUser
    ? []
    : [formatResponseKind(entry || {}), formatTokens(entry || {}), formatDuration(entry?.duration_ms)]
      .filter(Boolean);
  const body = text ?? "";

  return (
    <article className={`neo-chat-message ${isUser ? "user" : "assistant"}`}>
      <div className="message-stack">
        <span className="message-sender">{isUser ? "You" : "Neo"}</span>
        <div className="message-bubble">
          {html ? (
            /* Escaped by renderMessageHtml before any tag is emitted. */
            <div className="chat-content" dangerouslySetInnerHTML={{ __html: html }} />
          ) : (
            <div className="chat-content">{body}</div>
          )}
          <div className="message-footer">
            {sentAt ? <time className="message-time">{sentAt}</time> : null}
            {metadataItems.length > 0 ? (
              <span className="message-meta">
                {metadataItems.map((item) => <span key={item}>{item}</span>)}
              </span>
            ) : null}
            <MessageActionsMenu label={isUser ? "Message actions" : "Response actions"}>
              <button type="button" onClick={() => onCopy?.(entry?.content ?? body)}>
                Copy
              </button>
              {extraActions}
            </MessageActionsMenu>
          </div>
        </div>
      </div>
    </article>
  );
}

/** The same waiting bubble chat shows, so a working agent reads the same way. */
function AgentWorkingBubble() {
  return (
    <article className="neo-chat-message assistant thinking">
      <div className="message-stack">
        <span className="message-sender">Neo</span>
        <div className="message-bubble pending-message-bubble">
          <div className="pending-message-header">
            <span>Neo is working</span>
          </div>
        </div>
      </div>
    </article>
  );
}

/**
 * The follow-up bar, wearing the chat composer: one rounded card, the run's
 * settings behind "+", and a submit control that is a stop button while the
 * agent is still working.
 */
function AgentComposer({
  value,
  onChange,
  onSubmit,
  active,
  working,
  busy,
  mode,
  onModeChange,
  onStop,
  onLeaveAgentMode,
  workspace,
  agentName,
  attaching,
  onAttachFiles,
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  const menuButtonRef = useRef(null);
  const attachInputRef = useRef(null);

  useEffect(() => {
    if (!menuOpen) {
      return undefined;
    }

    function onPointerDown(event) {
      if (menuRef.current?.contains(event.target) || menuButtonRef.current?.contains(event.target)) {
        return;
      }
      setMenuOpen(false);
    }

    function onKeyDown(event) {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  return (
    <div className="chat-input-wrap agent-mode agent-followup-wrap">
      <div className="chat-input-shell">
        <form className="chat-input-form" onSubmit={onSubmit}>
          <div className="composer-head">
            <div className="composer-tools">
              <input
                ref={attachInputRef}
                type="file"
                multiple
                hidden
                onChange={(event) => {
                  const picked = Array.from(event.target.files || []);
                  event.target.value = "";
                  if (picked.length) onAttachFiles?.(picked);
                }}
              />
              <button
                ref={menuButtonRef}
                type="button"
                className={`composer-tool composer-menu-button${menuOpen ? " is-open" : ""}`}
                onClick={() => setMenuOpen((open) => !open)}
                aria-expanded={menuOpen}
                aria-haspopup="true"
                aria-controls="agent-composer-menu"
                aria-label={menuOpen ? "Close the composer menu" : "Open the composer menu"}
                title="More"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                  strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M12 5v14" />
                  <path d="M5 12h14" />
                </svg>
              </button>
              <div
                ref={menuRef}
                id="agent-composer-menu"
                className="composer-menu"
                aria-label="Run options"
                hidden={!menuOpen}
              >
                {/* A run is bound to the workspace and agent it started with, so
                    those read rather than change; mode and files still move. */}
                {workspace ? (
                  <label className="agent-chip">
                    <span className="agent-chip-label">Repo</span>
                    <select value="fixed" disabled aria-label="Repository for this run">
                      <option value="fixed">{workspace}</option>
                    </select>
                  </label>
                ) : null}
                <label className="agent-chip">
                  <span className="agent-chip-label">Mode</span>
                  <select
                    value={mode || "normal"}
                    onChange={(event) => {
                      setMenuOpen(false);
                      onModeChange(event.target.value);
                    }}
                    disabled={!active}
                    aria-label="Select permission mode"
                  >
                    {MODES.map((item) => (
                      <option key={item.id} value={item.id}>{item.label} · {item.hint}</option>
                    ))}
                  </select>
                </label>
                <label className="agent-chip">
                  <span className="agent-chip-label">Agent</span>
                  <select value="fixed" disabled aria-label="Agent for this run">
                    <option value="fixed">{agentName || "General"}</option>
                  </select>
                </label>
                <button
                  type="button"
                  className="composer-menu-action chat-attach-button"
                  onClick={() => {
                    setMenuOpen(false);
                    attachInputRef.current?.click();
                  }}
                  disabled={!active || attaching}
                  title="Attach files to your next message"
                  aria-label="Attach files"
                >
                  <PaperclipIcon />
                  <span>{attaching ? "Attaching…" : "Attach files"}</span>
                </button>
              </div>
            </div>
            <textarea
              value={value}
              onChange={(event) => onChange(event.target.value)}
              placeholder={active ? "Steer the agent …" : "This run has finished"}
              rows={1}
              disabled={!active}
              aria-label="Send a message to the agent"
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
          </div>
          <div className="composer-foot">
            <div className="composer-actions">
              <div className="chat-mode-switch" role="tablist" aria-label="Interaction mode">
                <button type="button" role="tab" aria-selected={false} onClick={onLeaveAgentMode}>
                  Chat
                </button>
                <button type="button" role="tab" aria-selected className="active">
                  Agent
                </button>
              </div>
              {/* A run that is still going gets the stop control in the send
                  button's slot, exactly as a streaming chat answer does. */}
              {working ? (
                <button
                  type="button"
                  className="send-button stop-button"
                  onClick={onStop}
                  disabled={busy}
                  aria-label="Stop the agent"
                  title="Stop the agent"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <rect x="7" y="7" width="10" height="10" rx="1.5" />
                  </svg>
                </button>
              ) : (
                <button
                  type="submit"
                  className="neo-button send-button"
                  disabled={!active || !value.trim()}
                  aria-label="Send to the agent"
                  title="Send to the agent"
                >
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
                    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="M12 19V5" />
                    <path d="m5 12 7-7 7 7" />
                  </svg>
                </button>
              )}
            </div>
          </div>
        </form>
      </div>
    </div>
  );
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
  // A file at the repository root has no enclosing folder to prefix-match, and
  // an empty prefix matches nothing -- which is why granting one used to be
  // refused outright. Its folder *is* the repository, so say that and grant it
  // as such rather than offering a scope that cannot work.
  const atRepoRoot = typeof pathArgument === "string" && !pathArgument.includes("/");
  const typedScope = scope.trim();
  const grantScope = typedScope || suggestion;
  const grantsWholeRepo = atRepoRoot && !typedScope;

  function grantPredicate() {
    return grantsWholeRepo
      ? { kind: "any" }
      : { kind: "path_prefix", argument: "path", value: grantScope };
  }

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
          <span>
            {grantsWholeRepo
              ? `${pathArgument} is at the repository root — narrow the scope, or leave this empty`
              : "Allow always for paths starting with"}
          </span>
          <input
            value={scope}
            placeholder={suggestion || "leave empty for the whole repository"}
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
            onClick={() => onDecide("allow_always", grantPredicate())}
          >
            {grantsWholeRepo
              ? `Allow always in this repository`
              : `Allow always in ${grantScope}`}
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

/**
 * One streamed event as the transcript's entry.
 *
 * The whole event is kept, not just its prose: `created_at` stamps the bubble
 * and the turn's provider, model, token count and duration are what its footer
 * shows. Keeping only `content` here is what left the agent's messages bare.
 */
function entryFromEvent(event) {
  if (event.type === "chunk" && event.content) return { ...event, kind: "text", id: event.seq };
  if (event.type === "tool.call") return { ...event, kind: "tool", id: event.call_id };
  return null;
}

function DiffView({ patch, onClose }) {
  if (!patch) return null;
  return (
    <section className="agent-diff">
      <div className="agent-diff-head">
        <span>Diff</span>
        <button type="button" onClick={onClose} aria-label="Close the diff">×</button>
      </div>
      <pre className="agent-diff-body">{patch}</pre>
    </section>
  );
}

function SessionUnavailable({ error, onClose }) {
  return (
    <div className="agent-session unavailable">
      <p className="agent-session-error">This run could not be opened: {error}</p>
      <button type="button" className="agent-session-back" onClick={onClose}>
        ← Back
      </button>
    </div>
  );
}

/**
 * A transcript of what the agent actually did, not a checklist of what it planned.
 *
 * The event log is the source of truth: it is append-only with a monotonic
 * sequence, so a reload reconnects with the last sequence it saw rather than
 * losing a run that is still executing on the server.
 */
export default function AgentSession({ sessionId, onClose, onMessage, onLeaveAgentMode }) {
  const [session, setSession] = useState(null);
  const [entries, setEntries] = useState([]);
  const [approval, setApproval] = useState(null);
  const [delivery, setDelivery] = useState(null);
  const [busy, setBusy] = useState(false);
  const [followUp, setFollowUp] = useState("");
  const [traceOpen, setTraceOpen] = useState(false);
  const [attachments, setAttachments] = useState([]);
  const [attaching, setAttaching] = useState(false);
  const [deliveryBusy, setDeliveryBusy] = useState(false);
  const [patch, setPatch] = useState("");
  const [undone, setUndone] = useState(false);
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
      const entry = entryFromEvent(event);
      if (entry) {
        return [...current, entry];
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

  async function deliver(mode) {
    setDeliveryBusy(true);
    try {
      const result = await api.deliverAgentChanges(sessionId, { mode });
      if (mode === "patch") setPatch(result.patch || "(no changes)");
      else onMessage?.(`Wrote ${result.written?.length ?? 0} file(s) into your repository.`);
    } catch (deliverError) {
      onMessage?.(deliverError.message);
    } finally {
      setDeliveryBusy(false);
    }
  }

  async function undoRun() {
    if (!window.confirm("Undo every change this run made to your folder?")) return;
    setDeliveryBusy(true);
    try {
      const result = await api.undoAgentRun(sessionId);
      const reversed = (result.restored?.length ?? 0) + (result.removed?.length ?? 0);
      const skipped = result.skipped || [];
      setUndone(skipped.length === 0);
      setPatch("");
      onMessage?.(
        skipped.length
          ? `Undid ${reversed} file(s). Left alone: ${skipped
              .map((item) => item.path)
              .join(", ")} — changed after the run finished.`
          : `Undid ${reversed} file(s).`,
      );
      await refresh();
    } catch (undoError) {
      onMessage?.(undoError.message);
    } finally {
      setDeliveryBusy(false);
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

  async function attachFiles(files) {
    setAttaching(true);
    try {
      const uploaded = [];
      for (const file of files) {
        const data = await api.uploadFile(file);
        uploaded.push(data.file);
      }
      setAttachments((current) => [...current, ...uploaded]);
    } catch (attachError) {
      setError(attachError.message);
    } finally {
      setAttaching(false);
    }
  }

  function withAttachments(text) {
    if (!attachments.length) return text;
    const LIMIT = 4000;
    const blocks = attachments.map((file) => {
      const name = file.metadata?.relative_path || file.display_name;
      const body = file.extracted_text || "";
      if (!body) return `--- Attached file: ${name} (no extractable text) ---`;
      const clipped = body.slice(0, LIMIT);
      return `--- Attached file: ${name} ---\n${clipped}${body.length > LIMIT ? "\n…(truncated)" : ""}`;
    });
    return `${text}\n\n${blocks.join("\n\n")}`;
  }

  async function send(event) {
    event.preventDefault();
    const content = withAttachments(followUp.trim());
    if (!followUp.trim()) return;
    setFollowUp("");
    setEntries((current) => [
      ...current,
      { kind: "user", id: `u-${Date.now()}`, content, created_at: new Date().toISOString() },
    ]);
    setAttachments([]);
    try {
      await api.sendAgentMessage(sessionId, content);
    } catch (sendError) {
      setError(sendError.message);
    }
  }

  if (!session) {
    // A run that never loads must stay escapable. Without this the view renders
    // over the whole app with no way back, and a stale id -- one left behind by
    // another profile -- is indistinguishable from a run that is merely slow.
    if (error) return <SessionUnavailable error={error} onClose={onClose} />;
    return <div className="agent-session loading">Loading run…</div>;
  }

  const active = !TERMINAL.has(session.status);
  const working = WORKING.has(session.status);
  const steps = groupEntries(entries);
  // A finished run collapses to its answer. The steps that produced it are the
  // working, not the result, so they stay one click away rather than on show.
  const finished = !active;
  const showSteps = !finished || traceOpen;
  // The model's closing narration *is* the answer -- it is the turn that ended
  // the run by asking for no further tool. `session.summary` is the
  // adjudicator's one-line gist, so it is a fallback, not the reply.
  const answer = lastAssistantEntry(steps);
  const summary = answer?.content || session.summary || "";
  // Whatever is shown as the answer is not also part of the working.
  const traceSteps = answer ? steps.filter((entry) => entry !== answer) : steps;
  const hasChanges = Boolean(delivery?.deliverable?.length || delivery?.blocked?.length);
  const canUndo = hasChanges && delivery?.mode === "live" && Boolean(delivery?.undoable);

  return (
    <section className="agent-session">
      <header className="agent-session-head">
        <div className="agent-session-title">
          {/* No back control: the composer's Chat tab leaves agent mode, and the
              sidebar switches runs. A run that cannot load keeps one, because
              that view has no other way out. */}
          <strong>{session.title}</strong>
          <span className={`agent-session-status ${session.status}`}>{statusLabel(session.status)}</span>
        </div>
        <div className="agent-session-controls">
          {active ? null : (
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
          <AgentBubble
            role="user"
            text={session.objective}
            entry={{ created_at: session.created_at, content: session.objective }}
            onCopy={copyText}
          />
          {showSteps ? (
            <div className={`agent-steps${working ? " is-working" : ""}`}>
              <span className="agent-steps-rail" aria-hidden="true">
                <span className="agent-steps-dot" />
              </span>
              {(finished ? traceSteps : steps).map((entry) => {
                if (entry.kind === "tool") return <ToolCard key={entry.id} event={entry} />;
                if (entry.kind === "user") {
                  return (
                    <AgentBubble
                      key={entry.id}
                      role="user"
                      text={entry.content}
                      entry={entry}
                      onCopy={copyText}
                    />
                  );
                }
                return (
                  <AgentBubble
                    key={entry.id}
                    role="assistant"
                    html={renderMessageHtml(entry.content)}
                    entry={entry}
                    onCopy={copyText}
                  />
                );
              })}
              {working && !approval ? <AgentWorkingBubble /> : null}
            </div>
          ) : null}
          {finished && summary ? (
            <AgentBubble
              role="assistant"
              html={renderMessageHtml(summary)}
              entry={{ ...(answer || {}), content: summary }}
              onCopy={copyText}
              extraActions={
                <>
                  {traceSteps.length > 0 ? (
                    <button type="button" onClick={() => setTraceOpen((open) => !open)}>
                      {traceOpen ? "Hide thinking" : "View thinking"}
                    </button>
                  ) : null}
                  {/* Only offered when this run actually produced changes, so the
                      menu never shows an action that would do nothing. */}
                  {hasChanges ? (
                    <button type="button" disabled={deliveryBusy} onClick={() => deliver("patch")}>
                      View diff
                    </button>
                  ) : null}
                  {hasChanges && delivery?.mode !== "live" && delivery?.deliverable?.length ? (
                    <button
                      type="button"
                      disabled={deliveryBusy}
                      onClick={() => deliver("working_tree")}
                    >
                      Apply changes
                    </button>
                  ) : null}
                  {canUndo ? (
                    <button
                      type="button"
                      className="row-actions-danger"
                      disabled={deliveryBusy || undone}
                      onClick={undoRun}
                    >
                      Undo this run
                    </button>
                  ) : null}
                </>
              }
            />
          ) : null}

          {approval ? <ApprovalCard approval={approval} busy={busy} onDecide={decide} /> : null}

          <DiffView patch={patch} onClose={() => setPatch("")} />
          {error ? <div className="agent-session-error">{error}</div> : null}
          <div ref={bottomRef} />
        </div>

        <TodoPanel items={session.todo} />
      </div>

      <AgentComposer
        value={followUp}
        onChange={setFollowUp}
        onSubmit={send}
        active={active}
        working={WORKING.has(session.status)}
        busy={busy}
        mode={session.mode}
        onModeChange={changeMode}
        onStop={stop}
        onLeaveAgentMode={onLeaveAgentMode}
        workspace={delivery?.root || session.repo_id || ""}
        agentName={session.agent_definition_snapshot?.display_name
          || session.agent_definition_snapshot?.name
          || "General"}
        attaching={attaching}
        onAttachFiles={attachFiles}
      />
    </section>
  );
}

export { AgentBubble, ApprovalCard, DiffView, SessionUnavailable, ToolCard, TodoPanel, entryFromEvent, groupEntries };

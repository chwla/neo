import { useMemo, useState } from "react";
import {
  formatDuration,
  formatMessageTime,
  formatResponseKind,
  formatTokens,
  renderMessageHtml,
} from "./chatPresentation.js";
import { MessageActionsMenu } from "./MessageActionsMenu.jsx";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
//: The agent is mid-step in these. `waiting_approval` is deliberately not one of
//: them: the run is open but the agent is waiting on you, so the composer offers
//: send rather than stop, exactly as chat does between answers.
const WORKING = new Set(["queued", "running"]);

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

const EXECUTOR_NAMES = { claude_code: "Claude Code", codex: "Codex", neo: "Neo" };

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

/**
 * One stored tool call as the trace's entry.
 *
 * A finished run reopened in a chat has no live events to replay -- the log was
 * consumed while it ran -- so its trace is rebuilt from the audit rows instead.
 * They carry the same fields the events did, under the store's column names.
 */
function entryFromToolCall(row) {
  return {
    kind: "tool",
    id: row.call_id || row.id,
    name: row.tool_name || row.name,
    arguments: row.arguments,
    status: row.status,
    content: row.content,
    error: row.error,
    duration_ms: row.duration_ms,
  };
}

/**
 * What to draw for one run: whatever is streaming, or what was recorded.
 *
 * Live wins because a run still going is replayed from its first event, so the
 * stream is the complete picture whenever it has anything at all. The stored
 * calls are the first paint of a run nobody watched finish.
 */
function traceEntries(run, liveEntries) {
  if (liveEntries?.length) return groupEntries(liveEntries);
  return (run?.tool_calls || []).map(entryFromToolCall);
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
              ? `${pathArgument} is at the repository root. Narrow the scope, or leave this empty`
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
  // A chain hands work from one engine to the next inside a single turn. The
  // divider is the only thing that makes that visible, and it rides the same
  // event stream as everything else rather than needing a second channel.
  if (event.type === "step.started") {
    return { ...event, kind: "step", id: `step-${event.index ?? event.seq}` };
  }
  return null;
}

/** Names the engine that produced a turn, so a long session stays legible. */
/**
 * A cost the CLI actually reported, never one inferred.
 *
 * `undefined`/`null` means the engine does not report cost (Codex), and must
 * render nothing at all -- a "$0.00" there would read as "this was free" rather
 * than "unknown". A genuine sub-cent charge gets "<$0.01" for the same reason:
 * rounding a real cost down to zero tells the same lie.
 */
function formatCost(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  if (value === 0) return "$0.00";
  return value < 0.01 ? "<$0.01" : `$${value.toFixed(2)}`;
}

function formatExecutorTokens(meta) {
  const total = (meta?.prompt_tokens || 0) + (meta?.completion_tokens || 0);
  if (total > 0) return `${total.toLocaleString()} tokens`;
  const usage = meta?.usage || {};
  const fallback = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  return fallback > 0 ? `${fallback.toLocaleString()} tokens` : null;
}

function ExecutorBadge({ executor, meta }) {
  if (!executor || executor === "neo") return null;
  const name = EXECUTOR_NAMES[executor] || executor;
  // Each fact is shown only where the engine genuinely reports it, so a badge
  // never implies a measurement that was not taken.
  const facts = [formatCost(meta?.total_cost_usd), formatExecutorTokens(meta)].filter(Boolean);
  if (meta?.num_turns) facts.push(`${meta.num_turns} turns`);
  return (
    <span className="agent-executor-badge" title={`This turn ran on ${name}`}>
      {name}
      {facts.length ? <span className="agent-executor-facts"> · {facts.join(" · ")}</span> : null}
    </span>
  );
}

/** "handed to Codex", drawn between two stretches of work in one turn. */
function StepDivider({ event }) {
  const name = event.name || EXECUTOR_NAMES[event.executor] || event.executor;
  const label = event.index > 0 ? `handed to ${name}` : name;
  return (
    <div className="agent-step-divider" role="separator">
      <span className="agent-step-arrow" aria-hidden="true">→</span>
      <span className="agent-step-name">{label}</span>
      {event.role ? <span className="agent-step-role">{event.role}</span> : null}
    </div>
  );
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

/**
 * What the agent did, drawn inside the conversation that asked for it.
 *
 * This is the working, not the result: the answer is the anchor message's own
 * bubble, rendered by the chat transcript like any other reply. Keeping the two
 * apart is what lets a finished run read as one turn -- an answer with its trace
 * folded away -- while a running one shows every step as it happens.
 *
 * A run in flight is expanded because watching is the point; a finished one
 * collapses, because by then the steps are how the answer was reached rather
 * than what the reader came for.
 */
export default function AgentTurn({ run, entries, traceOpen, busy, onDecide, patch, onClosePatch }) {
  const session = run?.session;
  if (!session) return null;
  const active = !TERMINAL.has(session.status);
  const working = WORKING.has(session.status);
  const approval = run?.pending_approval || null;
  const steps = traceEntries(run, entries);
  // While the run is live its closing narration is the only answer there is --
  // the anchor row is still empty -- so the trace shows it. Once finished, the
  // answer moved onto the row and showing it here too would print it twice.
  const answer = active ? null : lastAssistantEntry(steps);
  const traceSteps = answer ? steps.filter((entry) => entry !== answer) : steps;
  const showSteps = active || traceOpen;

  const executor = session.executor || "neo";
  const executorMeta = (session.external_meta || {})[executor] || {};

  return (
    <div className={`agent-turn${active ? " is-active" : ""}`}>
      <ExecutorBadge executor={executor} meta={executorMeta} />
      {showSteps ? (
        <div className={`agent-steps${working ? " is-working" : ""}`}>
          <span className="agent-steps-rail" aria-hidden="true">
            <span className="agent-steps-dot" />
          </span>
          {traceSteps.map((entry) => {
            if (entry.kind === "step") return <StepDivider key={entry.id} event={entry} />;
            if (entry.kind === "tool") return <ToolCard key={entry.id} event={entry} />;
            return (
              <AgentBubble
                key={entry.id}
                role={entry.kind === "user" ? "user" : "assistant"}
                html={entry.kind === "user" ? undefined : renderMessageHtml(entry.content)}
                text={entry.kind === "user" ? entry.content : undefined}
                entry={entry}
                onCopy={copyText}
              />
            );
          })}
          {working && !approval ? <AgentWorkingBubble /> : null}
        </div>
      ) : null}

      {approval ? <ApprovalCard approval={approval} busy={busy} onDecide={onDecide} /> : null}
      <DiffView patch={patch} onClose={onClosePatch} />
      <TodoPanel items={session.todo} />
    </div>
  );
}

export {
  AgentBubble,
  ApprovalCard,
  ExecutorBadge,
  formatCost,
  StepDivider,
  DiffView,
  ToolCard,
  TodoPanel,
  entryFromEvent,
  entryFromToolCall,
  groupEntries,
  lastAssistantEntry,
  statusLabel,
  traceEntries,
  TERMINAL,
  WORKING,
};

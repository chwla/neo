import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { entryFromEvent } from "./AgentTurn.jsx";

const TERMINAL_EVENTS = new Set(["run.completed", "run.failed", "run.cancelled"]);

/**
 * The live state of whichever turn a chat is currently producing.
 *
 * Both kinds write to one log, so one shape describes both: `text` is the reply
 * as it arrives, `entries` are an agent turn's steps, and `sessionId` says which
 * kind is running. A thread that is idle has `kind: null`, and the transcript
 * renders from its message rows alone.
 */
const IDLE = {
  kind: null,
  generationId: null,
  sessionId: null,
  messageId: null,
  text: "",
  thinking: "",
  statusText: "",
  entries: [],
  todo: null,
  approval: null,
  sessionStatus: null,
};

function reduce(state, event) {
  const type = event.type;
  // The first event of a turn says which kind it is and what it belongs to.
  const identity =
    event.agent_session_id || event.generation_id
      ? {
        kind: event.agent_session_id ? "agent" : "chat",
        sessionId: event.agent_session_id || null,
        generationId: event.generation_id || null,
        messageId: event.message_id ?? state.messageId,
      }
      : {};
  // A new turn clears the previous one: the finished turn is a message row by
  // now, and carrying its text forward would print it twice.
  const turnChanged =
    (identity.sessionId && identity.sessionId !== state.sessionId) ||
    (identity.generationId && identity.generationId !== state.generationId);
  const base = turnChanged ? { ...IDLE, ...identity } : { ...state, ...identity };

  if (type === "approval.required") {
    // The run has stopped and is waiting on a person. This used to be noticed
    // only when the stream closed, which made approving feel like it arrived
    // late; the event says so the moment it happens.
    //
    // The event carries the approval's id as `approval_id`, but the REST
    // session payload this is merged with (see mergeLiveRun in App.jsx) calls
    // the same field `id`. Normalizing here keeps `pending_approval.id`
    // reliable regardless of which source populated it -- without it, a
    // decision made from the live event alone posts to `/approvals/undefined`.
    return {
      ...base,
      approval: { ...event, id: event.approval_id },
      sessionStatus: "waiting_approval",
    };
  }
  // Anything else means the run moved on, so a decided approval stops standing.
  const moved = { ...base, approval: null };

  switch (type) {
    case "run.started":
      return { ...moved, sessionStatus: "running" };
    case "chunk":
      return moved.kind === "agent"
        ? { ...moved, entries: [...moved.entries, entryFromEvent(event)].filter(Boolean) }
        : { ...moved, text: moved.text + (event.content || "") };
    case "replace":
      // The whole answer, re-stated. A reply that had to be validated and
      // rewritten arrives this way, and appending it would show both drafts.
      return { ...moved, text: event.content || "" };
    case "thinking":
      return { ...moved, thinking: moved.thinking + (event.content || "") };
    case "run.status":
      return { ...moved, statusText: event.content || "" };
    case "todo.updated":
      return { ...moved, todo: event.items || [] };
    case "tool.call":
      return {
        ...moved,
        sessionStatus: "running",
        entries: [...moved.entries, entryFromEvent(event)].filter(Boolean),
      };
    case "tool.result":
      return {
        ...moved,
        entries: moved.entries.map((entry) =>
          entry.kind === "tool" && entry.id === event.call_id ? { ...entry, ...event } : entry,
        ),
      };
    default:
      return moved;
  }
}

/**
 * Watch one chat, whatever it is doing.
 *
 * Streaming and resumption are the same mechanism: the log is append-only with a
 * monotonic sequence, so a reload asks for everything after the last sequence it
 * saw. The server decides where a fresh reader starts -- the end of a settled
 * thread, or the beginning of a turn still in flight -- because only it knows
 * whether anything is running.
 *
 * The tail closes on a terminal event or when nothing is generating, and the
 * loop reconnects until the caller says the turn is over. That is what makes a
 * dropped connection resume rather than restart, and what lets a browser that
 * was closed mid-run come back to a finished one.
 */
export function useChatStream(chatId, startAfter, { onTurnEnd } = {}) {
  const [live, setLive] = useState(IDLE);
  const cursorRef = useRef(0);
  const endRef = useRef(onTurnEnd);
  endRef.current = onTurnEnd;

  const reset = useCallback(() => setLive(IDLE), []);

  useEffect(() => {
    if (!chatId) {
      setLive(IDLE);
      return undefined;
    }
    let cancelled = false;
    const controller = new AbortController();
    cursorRef.current = startAfter || 0;
    setLive(IDLE);

    function apply(event) {
      cursorRef.current = Math.max(cursorRef.current, event.seq || 0);
      if (event.type === "idle") return;
      setLive((current) => reduce(current, event));
      if (TERMINAL_EVENTS.has(event.type)) {
        // The turn is a message row now, so the transcript is reloaded and the
        // live state stands down rather than lingering as a duplicate bubble.
        endRef.current?.(event);
        setLive(IDLE);
      }
    }

    async function connect() {
      while (!cancelled) {
        try {
          await api.streamChatEvents(chatId, cursorRef.current, apply, controller.signal);
        } catch (error) {
          if (cancelled || error?.name === "AbortError") return;
          // A tail that cannot be held open is not a reason to stop watching:
          // the next turn still has to be seen. Back off and reconnect.
          await new Promise((resolve) => setTimeout(resolve, 1500));
          continue;
        }
        if (cancelled) return;
        // The server closed the tail because nothing is generating. Reopening it
        // is how the next turn -- which the user has not started yet -- arrives.
        await new Promise((resolve) => setTimeout(resolve, 400));
      }
    }

    connect();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [chatId, startAfter]);

  return { live, reset };
}

export { IDLE, reduce };

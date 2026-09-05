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
    case "turn.queued":
      // Accepted, but waiting for a free slot. Reusing `statusText` means the
      // existing pending bubble renders this with no new plumbing, and the
      // `run.started` that follows overwrites both fields.
      return {
        ...moved,
        sessionStatus: "queued",
        statusText: "Queued - waiting for a free slot",
      };
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
    case "step.started":
      // A handoff chain is one turn with several engines. The divider goes in
      // the same entry list as the work, so the trace reads in order.
      return { ...moved, entries: [...moved.entries, entryFromEvent(event)].filter(Boolean) };
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
 * Fold one record of the profile-wide tail into per-chat state.
 *
 * Pure, and separated from the hook so the demultiplexing can be tested without
 * a renderer. Returns the same Map when nothing applies, so React can skip the
 * render.
 */
export function applyEvent(streams, event) {
  const chatId = event?.chat_id;
  // `cursor` and `idle` are about the connection, not about any conversation.
  if (!chatId || event.type === "cursor" || event.type === "idle") return streams;
  const next = new Map(streams);
  next.set(chatId, reduce(streams.get(chatId) ?? IDLE, event));
  return next;
}

/**
 * Watch every chat in the profile over one connection.
 *
 * The effect deliberately has no `chatId` in its dependencies, so changing which
 * conversation is on screen does not tear the connection down. That is what lets
 * a chat keep answering after you have walked away from it -- the per-chat tail
 * this replaced re-subscribed on every switch, abandoning the turn you left.
 *
 * One connection rather than one per chat because a tail costs a socket and a
 * server thread for as long as it is held, and browsers allow about six sockets
 * to an origin -- so a handful of background chats would have starved every
 * other request in the app. The log's sequence is profile-wide, so a single
 * cursor orders all of them and each record says which chat it belongs to.
 */
export function useChatStreams({ onTurnEnd } = {}) {
  const [streams, setStreams] = useState(() => new Map());
  // null means "server, you decide" -- and it answers on the stream itself.
  const cursorRef = useRef(null);
  const endRef = useRef(onTurnEnd);
  endRef.current = onTurnEnd;

  const clear = useCallback((chatId) => {
    setStreams((current) => {
      if (!current.has(chatId)) return current;
      const next = new Map(current);
      next.delete(chatId);
      return next;
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    function apply(event) {
      if (event.type === "cursor") {
        cursorRef.current = event.seq ?? 0;
        return;
      }
      cursorRef.current = Math.max(cursorRef.current ?? 0, event.seq || 0);
      if (event.type === "idle") return;
      setStreams((current) => applyEvent(current, event));
      if (TERMINAL_EVENTS.has(event.type) && event.chat_id) {
        // The caller decides when to drop the buffer: a background chat's text
        // has to survive until its transcript has been reloaded, or switching
        // to it would show an empty pane for a turn that just finished.
        endRef.current?.(event.chat_id, event);
      }
    }

    async function connect() {
      while (!cancelled) {
        try {
          await api.streamAllChatEvents(cursorRef.current, apply, controller.signal);
        } catch (error) {
          if (cancelled || error?.name === "AbortError") return;
          await new Promise((resolve) => setTimeout(resolve, 1500));
          continue;
        }
        if (cancelled) return;
        // The server closed on its idle timeout. Reopening is how the next turn
        // arrives -- including one started somewhere else entirely.
        await new Promise((resolve) => setTimeout(resolve, 400));
      }
    }

    connect();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  return { streams, clear };
}

export { IDLE, reduce };

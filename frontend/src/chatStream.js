import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { entryFromEvent } from "./AgentTurn.jsx";

const TERMINAL_EVENTS = new Set(["run.completed", "run.failed", "run.cancelled"]);

//: How long a batch may wait when there are no frames to ride on -- a hidden
//: tab, where `requestAnimationFrame` stops. Short enough that a turn finishing
//: out of sight still reloads its transcript promptly, long enough that a
//: backgrounded reply is not paying a render per quarter second either.
const BACKSTOP_MS = 250;

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
  return applyEvents(streams, [event]);
}

export function applyEvents(streams, events) {
  let next = streams;
  for (const event of events) {
    const chatId = event?.chat_id;
    if (!chatId || event.type === "cursor" || event.type === "idle") continue;
    // Clone once per frame, even when reconnecting replays hundreds of events.
    if (next === streams) next = new Map(streams);
    next.set(chatId, reduce(next.get(chatId) ?? IDLE, event));
  }
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

    /* Events are applied a frame at a time rather than one at a time.
     *
     * A streaming reply arrives as a token per event, and every one of them used
     * to be its own `setStreams`. That is a React render each, and this hook is
     * consumed at the top of the application -- so a fast model was asking for
     * several dozen full renders a second, on top of everything else the turn was
     * already doing. The text cannot be shown faster than the screen is drawn, so
     * the renders past the first in any given frame bought nothing and were
     * competing for the main thread with the animation, the transcript and the
     * composer.
     *
     * So the events are collected and applied together on the next frame. Nothing
     * is dropped and nothing is reordered: the same reducer runs over the same
     * events in the same order, once, and what changes is only how often React is
     * asked to look at the result.
     *
     * The timer beside the frame request is not redundancy for its own sake.
     * `requestAnimationFrame` does not fire in a hidden tab, and a turn does not
     * stop because nobody is watching it -- so without a second way to flush, a
     * background chat would bank every event of a long reply in this array and
     * hold the terminal callbacks that reload its transcript until the tab came
     * back. Whichever fires first flushes and cancels the other.
     */
    let queue = [];
    let frame = 0;
    let backstop = 0;

    function flush() {
      if (frame) cancelAnimationFrame(frame);
      if (backstop) clearTimeout(backstop);
      frame = 0;
      backstop = 0;
      if (cancelled || !queue.length) return;
      const batch = queue;
      queue = [];
      setStreams((current) => applyEvents(current, batch));
      for (const event of batch) {
        if (TERMINAL_EVENTS.has(event.type) && event.chat_id) {
          // The caller decides when to drop the buffer: a background chat's text
          // has to survive until its transcript has been reloaded, or switching
          // to it would show an empty pane for a turn that just finished.
          //
          // After the batch is applied rather than as the event arrives, so the
          // text a turn ended with is in the map before anything is told the turn
          // is over.
          endRef.current?.(event.chat_id, event);
        }
      }
    }

    function apply(event) {
      if (event.type === "cursor") {
        cursorRef.current = event.seq ?? 0;
        return;
      }
      cursorRef.current = Math.max(cursorRef.current ?? 0, event.seq || 0);
      if (event.type === "idle") return;
      queue.push(event);
      if (!frame) frame = requestAnimationFrame(flush);
      if (!backstop) backstop = setTimeout(flush, BACKSTOP_MS);
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
      if (frame) cancelAnimationFrame(frame);
      if (backstop) clearTimeout(backstop);
    };
  }, []);

  return { streams, clear };
}

export { IDLE, reduce };

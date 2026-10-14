/**
 * The status a sidebar row's badge shows, with an identity that changes only
 * when some chat's status does.
 *
 * The inputs -- the live streams, the turns this browser is tracking, the chats
 * that finished while nobody looked -- change constantly while a reply streams:
 * the streams map is new on every flushed frame. The badges, though, only
 * change when a turn starts, a run's status moves or a turn ends. So the
 * function the sidebar is handed is rebuilt from a key made of exactly what it
 * reads, and a streamed token that changes no status hands the sidebar the same
 * function -- which is what lets a memoized sidebar skip that frame entirely.
 */

/** Everything `createStatusFor` reads, as one comparable string. */
export function chatStatusKey(streams, turns, finishedChatIds) {
  const live = [];
  for (const [chatId, state] of streams) {
    const status = state?.sessionStatus || (state?.kind ? "running" : "");
    if (status) live.push(`${chatId}=${status}`);
  }
  const tracked = [...turns.keys()].map(String);
  const finished = [...finishedChatIds].map(String);
  return `${live.sort().join("|")}#${tracked.sort().join(",")}#${finished.sort().join(",")}`;
}

/**
 * The badge status for a chat: a live run's own status first, then "running"
 * for anything streaming or tracked as a turn, then what the server stored,
 * then "done" for a turn that finished while the chat was not open.
 */
export function createStatusFor(streams, turns, finishedChatIds) {
  const live = new Map();
  for (const [chatId, state] of streams) {
    if (state?.sessionStatus) live.set(chatId, state.sessionStatus);
    else if (state?.kind) live.set(chatId, "running");
  }
  const tracked = new Set(turns.keys());
  const finished = new Set(finishedChatIds);
  return (chat) => {
    const liveStatus = live.get(chat.id);
    if (liveStatus) return liveStatus;
    if (tracked.has(chat.id)) return "running";
    const stored = chat.turn_status || chat.agent_status;
    if (stored) return stored;
    return finished.has(chat.id) ? "done" : null;
  };
}

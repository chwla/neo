import { useEffect, useRef } from "react";

export const NOTIFY_STORAGE_KEY = "neo-notify-background-chats";

const OUTCOMES = {
  "run.completed": "Finished replying",
  "run.failed": "Stopped with an error",
  "run.cancelled": "Stopped",
};

/**
 * Whether a finished turn is worth interrupting someone about.
 *
 * Pure, and exported, because the rule is the whole feature and it is easier to
 * be sure of in a test than in a browser: tell them about a chat they are not
 * looking at, and never about the one they are watching finish.
 *
 * `visibleChatId` is already null whenever another view (Tasks, Files, Notes) is
 * on screen, so a turn finishing while the user is somewhere else in the app
 * counts as unseen without this having to know which view is up.
 */
export function shouldNotify({ chatId, visibleChatId, hidden }) {
  if (!chatId) return false;
  return hidden || chatId !== visibleChatId;
}

export function notificationsEnabled(storage = globalThis.localStorage) {
  try {
    return storage?.getItem(NOTIFY_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * Tells the user a background chat is done.
 *
 * Push-driven rather than polled: `ReminderToast` polls because calendar
 * reminders have no live transport, but a finished turn arrives on the chat
 * stream the moment it happens. The two share their styles so the stacks look
 * like one thing.
 */
export default function BackgroundTurnToast({ notices, chatTitles, onOpen, onDismiss }) {
  const announced = useRef(new Set());

  useEffect(() => {
    if (!notices.length || !notificationsEnabled()) return;
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    for (const notice of notices) {
      if (announced.current.has(notice.id)) continue;
      announced.current.add(notice.id);
      try {
        const shown = new Notification(chatTitles.get(notice.chatId) || "Neo", {
          body: OUTCOMES[notice.outcome] || "Finished",
          // Coalesced per chat, so a talkative conversation replaces its own
          // notification instead of stacking five of them.
          tag: `neo-chat-${notice.chatId}`,
        });
        shown.onclick = () => {
          window.focus();
          onOpen(notice.chatId);
          shown.close();
        };
      } catch {
        // Construction can throw in some contexts; the in-app toast still shows.
      }
    }
  }, [notices, chatTitles, onOpen]);

  useEffect(() => {
    if (!notices.length) return undefined;
    const timers = notices.map((notice) =>
      window.setTimeout(() => onDismiss(notice.id), 8000),
    );
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [notices, onDismiss]);

  if (!notices.length) return null;

  return (
    <div className="reminder-toast-stack" role="status" aria-live="polite">
      {notices.map((notice) => (
        <div
          key={notice.id}
          className={`reminder-toast${notice.outcome === "run.failed" ? " is-failed" : ""}`}
        >
          <button
            className="reminder-toast-body reminder-toast-open"
            type="button"
            onClick={() => {
              onOpen(notice.chatId);
              onDismiss(notice.id);
            }}
          >
            <strong>{chatTitles.get(notice.chatId) || "Chat"}</strong>
            <span>{OUTCOMES[notice.outcome] || "Finished"}</span>
          </button>
          <button
            className="reminder-toast-dismiss"
            type="button"
            onClick={() => onDismiss(notice.id)}
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

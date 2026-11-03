import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import { showSystemNotification } from "./systemNotification.js";

const POLL_INTERVAL_MS = 30_000;

/**
 * Surfaces calendar reminders the backend sweep has marked due. No push
 * infrastructure exists (no service worker, no Web Push) -- this only works
 * while Neo is open in a browser tab, polling the same way the sidebar
 * already polls for unfinished agent runs (`App.jsx`, `refreshSidebar`).
 */
export default function ReminderToast() {
  const [reminders, setReminders] = useState([]);
  const notifiedIds = useRef(new Set());

  const poll = useCallback(async () => {
    try {
      const data = await api.calendarPendingReminders();
      const next = data.reminders || [];
      setReminders(next);
      const fresh = next.filter((item) => !notifiedIds.current.has(item.delivery_id));
      // Permission is not asked for from here: a request that does not follow a click
      // is ignored, and on iOS refused. Where it was granted in Settings, a system
      // notification goes out too; the in-app toast below shows every reminder anyway.
      for (const item of fresh) {
        await showSystemNotification(item.event_title, {
          body: "Coming up on your calendar",
          tag: `neo-reminder-${item.delivery_id}`,
        });
      }
      for (const item of next) notifiedIds.current.add(item.delivery_id);
    } catch {
      // Transient failures (offline, backend restarting) just skip a beat.
    }
  }, []);

  useEffect(() => {
    poll();
    const timer = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [poll]);

  async function dismiss(deliveryId) {
    setReminders((current) => current.filter((item) => item.delivery_id !== deliveryId));
    try {
      await api.ackCalendarReminder(deliveryId);
    } catch {
      // If the ack fails the next poll will just show it again.
    }
  }

  if (!reminders.length) return null;

  return (
    <div className="reminder-toast-stack" role="status" aria-live="polite">
      {reminders.map((item) => (
        <div key={item.delivery_id} className="reminder-toast">
          <div className="reminder-toast-body">
            <strong>{item.event_title}</strong>
            <span>Coming up on your calendar</span>
          </div>
          <button
            className="reminder-toast-dismiss"
            type="button"
            onClick={() => dismiss(item.delivery_id)}
            aria-label="Dismiss reminder"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

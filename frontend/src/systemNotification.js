/**
 * Showing a system notification in every browser Neo supports, or reporting that it cannot.
 *
 * Desktop browsers construct `Notification` from the page. iOS has no constructor: an
 * app added to the Home Screen (iOS 16.4 and later) notifies only through a service
 * worker, and a Safari tab has no Notification API at all. So the page's own
 * notification is tried first, then the service worker's; `null` means neither is
 * possible here, and the in-app toast, which every caller shows anyway, is the reminder.
 *
 * Permission is never requested here. Browsers honour a request only when it follows a
 * click, and a refusal cannot be asked again, so it is asked for in Settings alone.
 */

export const NOTIFICATIONS_WORKER = "/neo-notifications-sw.js";

/**
 * Show `title` as a system notification.
 *
 * Resolves to `{ kind: "page", notification }` (attach `onclick` to it),
 * `{ kind: "worker" }` (the worker focuses Neo when it is clicked), or `null`.
 */
export async function showSystemNotification(title, options = {}, env = globalThis) {
  const NotificationApi = env.Notification;
  if (typeof NotificationApi === "undefined" || NotificationApi.permission !== "granted") return null;
  try {
    return { kind: "page", notification: new NotificationApi(title, options) };
  } catch {
    // No constructor in this context (iOS): the service worker can still show it.
  }
  try {
    const registration = await env.navigator?.serviceWorker?.getRegistration?.("/");
    if (registration?.showNotification) {
      await registration.showNotification(title, options);
      return { kind: "worker" };
    }
  } catch {
    // Nothing more to try; the in-app toast stands.
  }
  return null;
}

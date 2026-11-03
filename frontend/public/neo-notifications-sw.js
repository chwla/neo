// Shows Neo's notifications where a page cannot construct one itself: an app added to
// the iOS Home Screen (iOS 16.4 and later) can only notify through a service worker.
// It has no fetch handler, so it never stands between Neo and its server, and it keeps
// no caches; older builds' caching workers are removed by main.jsx and the server.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((client) => "focus" in client);
      return open ? open.focus() : self.clients.openWindow("/");
    }),
  );
});

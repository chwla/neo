import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App.jsx";
import "./index.css";
import { NOTIFICATIONS_WORKER } from "./systemNotification.js";

// Remove service workers and Cache Storage left by older Neo frontend builds, keeping
// only the notifications worker, which caches nothing and handles no requests.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((registrations) => {
    registrations.forEach((registration) => {
      const worker = registration.active || registration.waiting || registration.installing;
      if (!worker?.scriptURL?.endsWith(NOTIFICATIONS_WORKER)) registration.unregister();
    });
  });
  navigator.serviceWorker.register(NOTIFICATIONS_WORKER).catch(() => {
    // Without it, notifications fall back to the page's own or the in-app toast.
  });
}
if ("caches" in window) {
  caches.keys().then((keys) => Promise.all(keys.map((key) => caches.delete(key))));
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

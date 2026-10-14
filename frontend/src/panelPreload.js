import { useEffect } from "react";

/**
 * Fetching a panel's code before it is opened, without getting in the way.
 *
 * Every panel is split out of the first download (see lazyPanel.jsx), which
 * keeps start-up small but makes each first open wait for its chunk. Two kinds
 * of preload close that gap, and neither replaces the fallback a panel shows
 * when it is opened before its code has arrived:
 *
 *  - Intent: focus on or a press of a control marked `data-preload`, or a
 *    pointer resting on it for a moment, starts that panel's download, so it
 *    has usually arrived by the time the click lands. At most two run
 *    together, and a pointer sweeping past does not count.
 *  - Background: the destinations the sidebar actually shows, one at a time,
 *    in idle time, starting a couple of seconds after load. Paused for a
 *    moment after any pointer, key, wheel or touch input and while the page is
 *    hidden; skipped altogether when the device or network asks for economy
 *    (Save-Data, a 2G connection, very little memory). Everything else waits
 *    for intent or for the open itself.
 */

/** How long a pointer rests on a trigger before that counts as intent. */
const HOVER_DWELL_MS = 80;

/** Whether background downloads should be skipped on this device and network. */
export function isConstrained(env = {}) {
  const connection = env.connection;
  if (connection?.saveData) return true;
  if (connection && (connection.effectiveType === "slow-2g" || connection.effectiveType === "2g")) return true;
  if (typeof env.deviceMemory === "number" && env.deviceMemory <= 2) return true;
  return false;
}

/** An idle slot after `delay` ms: requestIdleCallback where it exists (not Safari), else straight after. */
function idleAfter(fn, delay) {
  let idle = null;
  const timer = setTimeout(() => {
    if (typeof requestIdleCallback === "function") idle = requestIdleCallback(fn, { timeout: 2000 });
    else fn();
  }, delay);
  return () => {
    clearTimeout(timer);
    if (idle !== null && typeof cancelIdleCallback === "function") cancelIdleCallback(idle);
  };
}

export function createPanelPreloader({
  env = typeof navigator === "undefined" ? {} : navigator,
  isHidden = () => (typeof document === "undefined" ? false : document.hidden),
  now = () => performance.now(),
  schedule = idleAfter,
  quietMs = 1000,
  startAfterMs = 2000,
  maxIntent = 2,
} = {}) {
  let lastInput = -Infinity;
  let queue = [];
  let intentInFlight = 0;
  let backgroundBusy = false;
  let cancel = null;
  let stopped = false;

  const loaded = (panel) => !panel?.preload || panel.isLoaded?.();

  function arm(delay) {
    cancel?.();
    cancel = schedule(step, delay);
  }

  function step() {
    cancel = null;
    if (stopped) return;
    queue = queue.filter((panel) => !loaded(panel));
    if (!queue.length || backgroundBusy) return;
    if (isHidden() || now() - lastInput < quietMs) {
      arm(quietMs);
      return;
    }
    const panel = queue.shift();
    backgroundBusy = true;
    panel.preload().catch(() => {}).finally(() => {
      backgroundBusy = false;
      if (!stopped) arm(0);
    });
  }

  return {
    /** A control for this panel was pointed at, focused or pressed. */
    intent(panel) {
      if (stopped || loaded(panel) || intentInFlight >= maxIntent) return false;
      intentInFlight += 1;
      panel.preload().catch(() => {}).finally(() => {
        intentInFlight -= 1;
      });
      return true;
    },
    /** Queue these panels, most likely first, for idle-time download. */
    background(panels) {
      if (stopped || isConstrained(env)) return false;
      queue = panels.filter((panel) => !loaded(panel));
      if (queue.length) arm(startAfterMs);
      return true;
    },
    /** The person is doing something: background work waits for a quiet moment. */
    noteInput() {
      lastInput = now();
    },
    stop() {
      stopped = true;
      cancel?.();
      queue = [];
    },
    /** For tests: what is still queued. */
    pending() {
      return queue.slice();
    },
  };
}

/**
 * Wire a preloader to the page: `registry` maps a `data-preload` name to its
 * lazy panel; `backgroundIds` names the panels worth fetching in idle time.
 */
export function usePanelPreloading(registry, backgroundIds) {
  const key = backgroundIds.join(",");
  useEffect(() => {
    const preloader = createPanelPreloader();
    const onInput = () => preloader.noteInput();
    const onKey = (event) => {
      preloader.noteInput();
      // A modifier is how every app-wide shortcut starts, the palette's included.
      if (event.metaKey || event.ctrlKey) preloader.intent(registry.commandPalette);
    };
    const triggerOf = (event) => event.target?.closest?.("[data-preload]") || null;
    // Focus and a press are intent outright. A pointer only counts once it
    // rests on a trigger for a moment, so sweeping across the sidebar on the way
    // somewhere else does not start a download for everything it crosses.
    const onIntent = (event) => {
      const trigger = triggerOf(event);
      if (trigger) preloader.intent(registry[trigger.dataset.preload]);
    };
    let dwell = null;
    const onOver = (event) => {
      const trigger = triggerOf(event);
      if (!trigger || dwell?.trigger === trigger) return;
      clearTimeout(dwell?.timer);
      dwell = {
        trigger,
        timer: setTimeout(() => {
          dwell = null;
          preloader.intent(registry[trigger.dataset.preload]);
        }, HOVER_DWELL_MS),
      };
    };
    const onOut = (event) => {
      if (dwell && !dwell.trigger.contains(event.relatedTarget)) {
        clearTimeout(dwell.timer);
        dwell = null;
      }
    };
    const inputs = ["pointerdown", "wheel", "touchstart"];
    const intents = ["focusin", "pointerdown"];
    for (const type of inputs) window.addEventListener(type, onInput, { capture: true, passive: true });
    window.addEventListener("keydown", onKey, { capture: true, passive: true });
    for (const type of intents) document.addEventListener(type, onIntent, { passive: true });
    document.addEventListener("pointerover", onOver, { passive: true });
    document.addEventListener("pointerout", onOut, { passive: true });
    preloader.background(key ? key.split(",").map((name) => registry[name]).filter(Boolean) : []);
    return () => {
      preloader.stop();
      clearTimeout(dwell?.timer);
      for (const type of inputs) window.removeEventListener(type, onInput, { capture: true });
      window.removeEventListener("keydown", onKey, { capture: true });
      for (const type of intents) document.removeEventListener(type, onIntent);
      document.removeEventListener("pointerover", onOver);
      document.removeEventListener("pointerout", onOut);
    };
  }, [registry, key]);
}

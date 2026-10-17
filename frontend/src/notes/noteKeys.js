/**
 * The keys that open locked notes, held in this page's memory only.
 *
 * Opened with the profile password (the server checks it as sign-in does and
 * returns each key version's wrap; the key is unwrapped here, non-extractable).
 * Unlocking opens every locked note for the session. Nothing about a key is
 * ever written to storage: a reload or a closed tab locks everything.
 *
 * Auto-lock: after 15 minutes without input in Notes, measured on the wall
 * clock (so time asleep or in a hidden tab counts), on Lock Now, and on a
 * profile switch or sign-out (the controller is disposed). Locking runs the
 * registered `beforeLock` steps first, while the keys still exist (the editor
 * goes read-only and unsaved content is encrypted into the journal and the
 * save queue), and only then drops the keys.
 */
import { openWraps } from "./crypto.js";

export const AUTO_LOCK_MS = 15 * 60 * 1000;
const CHECK_EVERY = 30 * 1000;

export function createKeySession({ transport, open = openWraps, now = () => Date.now(), timers = globalThis, idleMs = AUTO_LOCK_MS } = {}) {
  let keys = new Map();
  let currentVersion = null;
  let unavailable = [];
  let lastActivity = 0;
  let interval = null;
  let locking = null;
  // Set before the first step runs (a step may ask for a key at once).
  let inLock = false;
  const beforeLock = new Set();
  const listeners = new Set();

  function emit() {
    for (const listener of listeners) listener();
  }

  function stopClock() {
    if (interval) timers.clearInterval(interval);
    interval = null;
  }

  function startClock() {
    stopClock();
    interval = timers.setInterval(check, CHECK_EVERY);
  }

  function expired() {
    return keys.size > 0 && now() - lastActivity >= idleMs;
  }

  /** On the clock, and on return to the page: the time away counts. */
  function check() {
    if (expired()) lock("idle");
  }

  async function accept(password, result) {
    const opened = await open(password, result.wraps ?? []);
    keys = opened;
    currentVersion = result.current_version ?? null;
    unavailable = [...(result.unavailable ?? [])];
    lastActivity = now();
    if (keys.size) startClock();
    emit();
    return session.state();
  }

  async function lock(reason = "now") {
    if (inLock) return locking ?? Promise.resolve();
    if (!keys.size) return undefined;
    inLock = true;
    locking = (async () => {
      // While the keys still exist: read-only, then unsaved content encrypted away.
      for (const step of beforeLock) {
        try {
          await step(reason);
        } catch {
          /* each step keeps what it could; the keys still go */
        }
      }
      keys = new Map();
      currentVersion = null;
      stopClock();
      emit();
    })().finally(() => {
      locking = null;
      inLock = false;
    });
    return locking;
  }

  const session = {
    /** Open the keys with the profile password. Throws the server's answer for a wrong one. */
    async unlock(password) {
      return accept(password, await transport.unlock(password));
    },
    /** First lock on this profile: make a key (or open the one there is). */
    async setup(password) {
      return accept(password, await transport.setup(password));
    },
    /** Notes locked under an earlier password: re-wrap their key under the current one. */
    async recover(password, earlierPassword) {
      return accept(password, await transport.rewrap(password, earlierPassword));
    },
    state() {
      return { unlocked: keys.size > 0, currentVersion, versions: [...keys.keys()], unavailable: [...unavailable] };
    },
    isUnlocked: () => keys.size > 0,
    keyFor(version) {
      // While locking, the steps before it still need the keys to encrypt what is unsaved.
      if (!inLock && expired()) {
        lock("idle");
        return null;
      }
      return keys.get(version) ?? null;
    },
    currentVersion: () => currentVersion,
    /** Input in Notes. After the deadline, input no longer counts: the keys go first. */
    touch() {
      if (!keys.size || inLock) return;
      if (expired()) {
        lock("idle");
        return;
      }
      lastActivity = now();
    },
    check,
    lock,
    onBeforeLock(step) {
      beforeLock.add(step);
      return () => beforeLock.delete(step);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispose() {
      stopClock();
      return lock("dispose");
    },
  };
  return session;
}

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * The part of signing in to Claude Code or Codex that needs a browser.
 *
 * Opened by Settings > Engines, and only when there is genuinely something for
 * a person to do. Turning the feature on, re-probing the CLI and starting its
 * sign-in have already happened by the time this mounts -- the panel does that
 * behind its button, and an engine that was installed and signed in all along
 * simply goes green without this ever appearing.
 *
 * So there are exactly two reasons to be here. Claude Code's browser flow ends
 * on a page showing a code, and that code has to come back through Neo -- this
 * takes it, and closes the moment the CLI reports itself signed in. Or the CLI
 * is not installed, which is the one thing Neo cannot do for anyone.
 *
 * Codex needs neither: it finishes on its own local callback, so this shows a
 * link and closes itself when the browser comes back.
 *
 * The states are the ones the API returns, so there is no second opinion here
 * about what is wrong with an engine.
 */

const POLL_MS = 1000;

export default function ExternalAgentSetup({ executor, name, initial, onClose, onConnected }) {
  // Seeded from the connect that has already happened. This dialog is only
  // mounted once that call came back needing something, so starting in a
  // "checking..." state would be a spinner for work that is already done.
  const [phase, setPhase] = useState(initial?.state || "connecting");
  const [login, setLogin] = useState(initial?.login || null);
  const [engine, setEngine] = useState(initial?.engine || null);
  const [error, setError] = useState(initial?.error || "");
  const [code, setCode] = useState("");
  const [sending, setSending] = useState(false);
  // Guards the one-shot close: a retry and the poll can both observe "ready",
  // and reporting the same engine connected twice would re-probe for nothing.
  const settled = useRef(false);

  const label = engine?.name || name || "this engine";

  const finish = useCallback(() => {
    if (settled.current) return;
    settled.current = true;
    onConnected?.(executor);
    onClose();
  }, [executor, onConnected, onClose]);

  const connect = useCallback(async () => {
    setPhase("connecting");
    setError("");
    try {
      const result = await api.connectExternalAgent(executor);
      setEngine(result.engine || null);
      setLogin(result.login || null);
      if (result.state === "ready") {
        finish();
        return;
      }
      if (result.state === "error") setError(result.error || "It could not be started.");
      setPhase(result.state);
    } catch (connectError) {
      setError(connectError.message);
      setPhase("error");
    }
  }, [executor, finish]);

  // Watch the sign-in the CLI is running, and close as soon as it has worked.
  useEffect(() => {
    if (phase !== "signing_in") return undefined;
    let stopped = false;
    const timer = setInterval(async () => {
      let next;
      try {
        next = await api.externalAgentLogin(executor);
      } catch {
        return; /* a dropped poll is not a failed sign-in; the next one retries */
      }
      if (stopped) return;
      setLogin(next);
      if (next.running) return;
      if (next.status?.available) {
        finish();
        return;
      }
      setError(next.error || "The sign-in did not complete.");
      setPhase("error");
    }, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [phase, executor, finish]);

  async function sendCode(event) {
    event.preventDefault();
    const value = code.trim();
    if (!value) return;
    setSending(true);
    setCode("");
    try {
      setLogin(await api.submitExternalAgentLoginCode(executor, value));
    } catch (sendError) {
      setError(sendError.message);
    } finally {
      setSending(false);
    }
  }

  async function cancel() {
    try {
      await api.cancelExternalAgentLogin(executor);
    } catch {
      /* Closing is what was asked for; a failed cancel must not block it. */
    }
    onClose();
  }

  return (
    <Modal title={`Use ${label}`} onClose={onClose} className="engine-connect">
      <div className="engine-connect-body">
        {phase === "connecting" ? (
          <p className="engine-connect-line" role="status">
            Checking {label}…
          </p>
        ) : null}

        {phase === "signing_in" ? (
          <>
            <p className="engine-connect-line">
              Sign in to {label} to run agent turns on your own subscription.
            </p>
            {login?.url ? (
              <a className="engine-connect-cta" href={login.url} target="_blank"
                rel="noreferrer noopener">
                Open the sign-in page
              </a>
            ) : (
              <p className="engine-connect-muted" role="status">Starting…</p>
            )}

            {login?.needs_code ? (
              <form className="engine-connect-code" onSubmit={sendCode}>
                <label htmlFor="engine-connect-code-input">
                  Paste the code the page gives you
                </label>
                <div className="engine-connect-code-row">
                  <input
                    id="engine-connect-code-input"
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="code"
                    autoFocus
                  />
                  <button type="submit" className="engine-button" disabled={!code.trim() || sending}>
                    {sending ? "Sending…" : "Done"}
                  </button>
                </div>
              </form>
            ) : (
              <p className="engine-connect-muted" role="status">
                Waiting for you to finish in the browser…
              </p>
            )}

            {login?.url ? <p className="engine-connect-url">{login.url}</p> : null}

            <button type="button" className="engine-button quiet" onClick={cancel}>
              Cancel
            </button>
          </>
        ) : null}

        {phase === "not_installed" ? (
          <>
            <p className="engine-connect-line">
              {label} is not installed on this computer, so there is nothing for Neo
              to sign in to yet.
            </p>
            <p className="engine-connect-muted">
              Install it, then re-check in Settings &gt; Engines. Neo runs the copy on
              this machine and signs in as you.
            </p>
          </>
        ) : null}

        {phase === "error" ? (
          <>
            <p className="engine-connect-error">{error}</p>
            <button type="button" className="engine-button primary" onClick={connect}>
              Try again
            </button>
          </>
        ) : null}

        {engine?.notes?.length ? (
          <p className="engine-connect-note">
            {label} runs under its own CLI permission model.
            {engine.notes.map((note) => (
              <span key={note}>{note}</span>
            ))}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}

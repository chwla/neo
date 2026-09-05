import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";
import ExternalAgentSetup from "./ExternalAgentSetup.jsx";

/**
 * Settings > Engines: signing in to Claude Code and Codex.
 *
 * This is where an external engine is connected, and the only place. The
 * composer's Engine picker offers a CLI once it is genuinely usable and says
 * nothing about one that is not -- an engine you have not signed in to is not a
 * choice you are declining to make, it is a task, and a task does not belong in
 * a dropdown next to three settings that take effect immediately.
 *
 * So the picker got shorter and this got written. Everything that used to
 * happen behind the pick happens here in the open: what is installed, what is
 * signed in, what is merely switched off, and one button per row for whichever
 * of those is standing in the way.
 *
 * The states come from `/external-agents/setup`, which probes regardless of the
 * profile switch. That matters: the listing the composer reads reports a
 * switched-off profile as "unavailable" for everything, which here would hide
 * the one fact worth showing -- that the CLI is ready and only Neo's own
 * opt-in is missing.
 */

/** Which CLI to install for each engine, as documented in the README. */
const INSTALL = {
  claude_code: "npm i -g @anthropic-ai/claude-code",
  codex: "npm i -g @openai/codex",
};

/**
 * What one engine's row is currently saying, as a token the panel branches on.
 *
 * Ordered by what has to be fixed first, because the actions are not
 * interchangeable: nothing can be signed in that is not installed, and nothing
 * runs while the profile has not opted in. `row.available` here is the *machine*
 * fact -- installed and signed in -- because this panel reads the ungated setup
 * endpoint; whether the profile allows it is the separate `enabled` argument.
 *
 * Exported because it is the whole rule of the panel and the suite renders to
 * static markup, with no way to click through the four states.
 */
export function engineState(row, enabled) {
  if (!row?.version) return "not_installed";
  if (!row?.available) return "signed_out";
  if (!enabled) return "off";
  return "connected";
}

const STATE_LABEL = {
  not_installed: "Not installed",
  signed_out: "Not signed in",
  off: "Signed in · switched off",
  connected: "Connected",
};

/** The button that resolves each state, or "" where there is nothing to press. */
const STATE_ACTION = {
  not_installed: "",
  signed_out: "Sign in",
  off: "Turn on",
  connected: "",
};

export default function ExternalAgents({ onClose, onChanged }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  // The sign-in dialog, opened on top of this one when a connect turns out to
  // need a person: a browser round trip, or a code to paste back.
  const [setup, setSetup] = useState(null);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError("");
    try {
      setData(await api.externalAgentSetup(refresh));
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /**
   * Report the change outward as well as re-reading it here.
   *
   * The composer's engine list was loaded before this dialog opened, so without
   * this an engine signed in here would not appear in the picker until the next
   * time Agent mode was entered -- which reads exactly like the sign-in having
   * failed.
   */
  const settled = useCallback(
    async (message) => {
      setNote(message);
      await load(true);
      onChanged?.();
    },
    [load, onChanged],
  );

  async function connect(row) {
    setBusy(row.id);
    setError("");
    setNote("");
    try {
      const result = await api.connectExternalAgent(row.id);
      if (result.state === "ready") {
        await settled(`${row.name} is connected. It is now offered in the engine picker.`);
        return;
      }
      if (result.state === "error") {
        setError(result.error || `${row.name} could not be started.`);
        await load(true);
        return;
      }
      // signing_in, or not_installed with something to say about it.
      setSetup({ executor: row.id, name: row.name, result });
    } catch (connectError) {
      setError(connectError.message);
    } finally {
      setBusy("");
    }
  }

  async function toggle(next) {
    setBusy("__all__");
    setError("");
    setNote("");
    try {
      await api.setExternalAgentsEnabled(next);
      await settled(
        next
          ? "External engines are on for this profile."
          : "External engines are off. They no longer appear in the engine picker.",
      );
    } catch (toggleError) {
      setError(toggleError.message);
    } finally {
      setBusy("");
    }
  }

  const enabled = Boolean(data?.enabled);
  const executors = data?.executors || [];

  // Deliberately not the `settings-dialog` shell: that one dresses the
  // two-column category browser, and to do it zeroes the dialog's padding and
  // hides its overflow -- which this panel has neither the layout for nor the
  // room to spare.
  return (
    <Modal title="Engines" onClose={onClose} className="engines-dialog">
      <p className="dialog-caption">
        Claude Code and Codex run agent turns on your own CLI subscription, in the folder
        attached to the chat. Sign in here and the engine is offered in the composer&apos;s
        Engine picker; until then only Neo&apos;s own engine is.
      </p>

      <div className="chat-tools-row">
        <div className="chat-tools-row-info">
          <div className="chat-tools-row-title">
            <strong>Allow external engines</strong>
          </div>
          <p>
            Off, Neo starts no external CLI and none is offered in any chat. Turning it off
            does not sign you out of the CLIs themselves — turn it back on and whatever was
            signed in still is.
          </p>
        </div>
        <label className="chat-tools-toggle">
          <input
            type="checkbox"
            checked={enabled}
            disabled={loading || Boolean(busy)}
            onChange={(event) => toggle(event.target.checked)}
            aria-label="Allow external engines"
          />
        </label>
      </div>

      <div className="engine-rows">
        {loading && !data ? <p className="engine-connect-muted">Checking…</p> : null}
        {executors.map((row) => {
          const state = engineState(row, enabled);
          const action = STATE_ACTION[state];
          return (
            <div className="engine-row" key={row.id}>
              <div className="engine-row-head">
                <strong>{row.name}</strong>
                <span className={`engine-state engine-state-${state}`}>{STATE_LABEL[state]}</span>
              </div>
              {row.version ? <p className="engine-row-version">{row.version}</p> : null}

              {state === "not_installed" ? (
                <>
                  <p className="engine-connect-muted">
                    {row.reason || "Neo could not find the CLI on this computer."} Neo runs the
                    copy installed on this machine — it cannot install one for you.
                  </p>
                  {INSTALL[row.id] ? <p className="engine-connect-url">{INSTALL[row.id]}</p> : null}
                </>
              ) : null}

              {state === "signed_out" ? (
                <p className="engine-connect-muted">
                  Installed, but not signed in. Neo can start {row.name}&apos;s own sign-in for
                  you{row.command ? <>, or run <code>{row.command}</code> yourself</> : null}.
                </p>
              ) : null}

              {state === "off" ? (
                <p className="engine-connect-muted">
                  Installed and signed in. It is not offered in any chat until external engines
                  are allowed for this profile.
                </p>
              ) : null}

              {state === "connected" ? (
                <p className="engine-connect-muted">
                  Signed in{row.auth === "subscription" ? " on your subscription" : ""} and
                  offered in the engine picker.
                </p>
              ) : null}

              {row.notes?.length ? (
                <details className="agent-executor-note">
                  <summary>Runs under {row.name}&apos;s own permissions</summary>
                  <span className="agent-executor-note-list">
                    {row.notes.map((engineNote) => (
                      <span key={engineNote}>{engineNote}</span>
                    ))}
                  </span>
                </details>
              ) : null}

              {action ? (
                <button
                  type="button"
                  className="engine-button primary"
                  onClick={() => connect(row)}
                  disabled={Boolean(busy) || loading}
                >
                  {busy === row.id ? "Working…" : action}
                </button>
              ) : null}
            </div>
          );
        })}
      </div>

      <div className="engine-rows-footer">
        <button
          type="button"
          className="engine-button quiet"
          onClick={() => load(true)}
          disabled={Boolean(busy) || loading}
        >
          {loading ? "Checking…" : "Re-check"}
        </button>
        {data?.trust_boundary?.summary ? (
          <p className="engine-connect-note">{data.trust_boundary.summary}</p>
        ) : null}
      </div>

      {note && <div className="settings-status">{note}</div>}
      {error && <div className="neo-error">{error}</div>}

      {setup && (
        <ExternalAgentSetup
          executor={setup.executor}
          name={setup.name}
          initial={setup.result}
          onClose={() => setSetup(null)}
          onConnected={() => settled(`${setup.name} is connected.`)}
        />
      )}
    </Modal>
  );
}

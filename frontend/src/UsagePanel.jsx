import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * How much of each connected engine's subscription window is spent.
 *
 * Neither CLI answers this question on demand, so every figure here comes from
 * something the CLI wrote down at some earlier moment -- Claude Code's own usage
 * cache, or the rate-limit notice a run streamed, or the log Codex keeps of its
 * last session. That makes "as of" part of the reading rather than decoration:
 * a percentage shown bare would read as current when it may be from last week,
 * and someone would plan a long run against it. So every engine block states
 * when its numbers were observed, and says plainly when it has none.
 */

/** Rounded to whole minutes: nobody plans around the seconds. */
export function untilPhrase(epochSeconds) {
  if (typeof epochSeconds !== "number") return "";
  const remaining = Math.round(epochSeconds * 1000 - Date.now());
  if (remaining <= 0) return "Resets now";
  const minutes = Math.round(remaining / 60000);
  if (minutes < 60) return `Resets in ${Math.max(1, minutes)}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `Resets in ${hours}h`;
  return `Resets in ${Math.round(hours / 24)}d`;
}

/** The other direction, and the reason the whole panel exists in this shape. */
export function observedPhrase(epochSeconds) {
  if (typeof epochSeconds !== "number") return "";
  const elapsed = Date.now() - epochSeconds * 1000;
  if (elapsed < 90000) return "as of just now";
  const minutes = Math.round(elapsed / 60000);
  if (minutes < 60) return `as of ${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `as of ${hours}h ago`;
  return `as of ${Math.round(hours / 24)}d ago`;
}

// Keyed by the source token, so the prose must hold for whichever engine returned
// it. Naming a specific CLI here was wrong the moment a second engine could report
// the same token -- the label would then confidently credit the wrong tool.
const SOURCE_LABEL = {
  cli_cache: "from the CLI's own usage cache",
  run: "from the last run through Neo",
  session_log: "from the CLI's last recorded session",
};

const ACCOUNT_ROWS = [
  ["auth_method", "Auth method"],
  ["email", "Email"],
  ["organization", "Organization"],
  ["plan", "Plan"],
];

function UsageBar({ window: usageWindow }) {
  const percent = usageWindow.used_percent;
  const resets = untilPhrase(usageWindow.resets_at);
  return (
    <div className="usage-window">
      <div className="usage-window-head">
        <span className="usage-window-title">{usageWindow.title}</span>
        <span className="usage-window-percent">{Math.round(percent)}%</span>
      </div>
      {/* The bar is decoration over a number that is already stated, so it is
          hidden from assistive tech rather than repeated to it. */}
      <div className={`usage-track severity-${usageWindow.severity}`} aria-hidden="true">
        <span className="usage-fill" style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
      {resets ? <span className="usage-window-reset">{resets}</span> : null}
    </div>
  );
}

export function EngineUsage({ engine }) {
  const account = engine.account;
  const observed = observedPhrase(engine.observed_at);
  const source = SOURCE_LABEL[engine.source] || "";
  return (
    <section className="usage-engine">
      <h3>{engine.name}</h3>

      {account ? (
        <dl className="usage-account">
          {ACCOUNT_ROWS.filter(([key]) => account[key]).map(([key, label]) => (
            <div key={key} className="usage-account-row">
              <dt>{label}</dt>
              <dd>{account[key]}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {engine.windows.length ? (
        <>
          <div className="usage-windows">
            {engine.windows.map((usageWindow) => (
              <UsageBar key={usageWindow.key} window={usageWindow} />
            ))}
          </div>
          {/* Not a footnote. These numbers are caches, and the stamp is the only
              thing separating a reading from a guess. */}
          <p className="usage-observed">{[observed, source].filter(Boolean).join(" · ")}</p>
        </>
      ) : (
        <p className="usage-empty">{engine.reason || "No usage recorded for this engine yet."}</p>
      )}
    </section>
  );
}

export default function UsagePanel({ onClose }) {
  const [engines, setEngines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError("");
    try {
      const result = await api.externalAgentUsage(refresh);
      setEngines(result.executors || []);
    } catch (requestError) {
      setError(requestError.message || "Could not read usage.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // An engine that is not connected has no usage to report, and its row would
  // only repeat what the engine picker already says. The composer hides this
  // whole panel when none is connected, so an empty list here means the state
  // changed while it was open.
  const connected = engines.filter((engine) => engine.available);

  return (
    <Modal title="Usage" onClose={onClose} className="usage-panel">
      <p className="dialog-caption">
        Subscription limits for the coding CLIs you have connected. Neo reads what each
        CLI recorded for itself. Neo never reads their credentials, and never asks the
        vendor. That is why each figure is stamped with when it was true.
      </p>

      {error ? <div className="ws-error">{error}</div> : null}

      {loading ? (
        <p className="open-folder-empty">Reading usage…</p>
      ) : connected.length ? (
        connected.map((engine) => <EngineUsage key={engine.id} engine={engine} />)
      ) : (
        <p className="open-folder-empty">No engine is connected right now.</p>
      )}

      <div className="usage-actions">
        <button type="button" onClick={() => load(true)} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
    </Modal>
  );
}

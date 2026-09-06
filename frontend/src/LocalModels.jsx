import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import WorkspaceIcon from "./WorkspaceIcon.jsx";

// How each verdict is worded on screen. The backend decides which tier a model is in;
// this only names it. Never a number, and never a unit.
export const TIER_LABEL = {
  comfortable: "Runs comfortably",
  good: "Runs well",
  tight: "Runs, but only just",
  too_big: "Too big for this computer",
};

// The order the groups appear in, best first.
const TIER_ORDER = ["comfortable", "good", "tight"];

const REMEMBERED_GOAL = "neo.localModels.goal";

/**
 * Split recommendations into the groups the screen shows.
 *
 * Exported as a plain function because the grouping *is* the feature -- "which of
 * these can I actually run" is the whole question -- and the frontend suite renders to
 * static markup with no way to click anything. Same reason engineOptions and
 * engineState are shaped this way.
 */
export function groupByHowTheyRun(items = []) {
  const fits = items.filter((item) => item.fit.tier !== "too_big");
  const groups = TIER_ORDER.map((tier) => ({
    tier,
    label: TIER_LABEL[tier],
    items: fits.filter((item) => item.fit.tier === tier),
  })).filter((group) => group.items.length > 0);

  return {
    top: fits[0] || null,
    // The top pick is shown on its own card above, so it is not repeated in its group.
    groups: groups.map((group) => ({
      ...group,
      items: group.items.filter((item) => item !== fits[0]),
    })).filter((group) => group.items.length > 0),
    tooBig: items.filter((item) => item.fit.tier === "too_big"),
  };
}

/** Whether the wizard should run, i.e. whether a goal has been chosen before. */
export function shouldAskFirst(rememberedGoal) {
  return !rememberedGoal;
}

function readRememberedGoal() {
  try {
    return window.localStorage.getItem(REMEMBERED_GOAL) || "";
  } catch {
    // A private window, or storage turned off. Asking again is a fine fallback.
    return "";
  }
}

function rememberGoal(goal) {
  try {
    window.localStorage.setItem(REMEMBERED_GOAL, goal);
  } catch {
    // Not worth surfacing: the screen still works, it just asks again next time.
  }
}

function GoalPicker({ goals, selected, onPick, heading }) {
  return (
    <section className="lm-goals">
      <h2 className="lm-h2">{heading}</h2>
      <div className="lm-goal-row" role="group" aria-label="What do you want it for?">
        {goals.map((goal) => (
          <button
            key={goal.id}
            type="button"
            className={`lm-goal${selected === goal.id ? " selected" : ""}`}
            aria-pressed={selected === goal.id}
            onClick={() => onPick(goal.id)}
          >
            <span className="lm-goal-label">{goal.label}</span>
            <span className="lm-goal-detail">{goal.detail}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function InstallButton({ item, state, onInstall, onCancel }) {
  const oneClick = item.install_options?.[0]?.kind === "one_click";
  if (item.installed) {
    return <p className="lm-installed">Already on this computer.</p>;
  }
  if (!oneClick) {
    return <p className="lm-note-manual">This one has to be set up by hand.</p>;
  }

  const busy = state?.status === "working";
  return (
    <div className="lm-install">
      <div className="lm-install-actions">
        <button
          type="button"
          className="neo-button lm-install-button"
          onClick={() => onInstall(item)}
          disabled={busy}
        >
          {busy ? "Setting up…" : "Set this up for me"}
        </button>
        {busy ? (
          <button
            type="button"
            className="neo-button secondary lm-cancel-button"
            onClick={() => onCancel(item)}
          >
            Cancel
          </button>
        ) : null}
      </div>
      {state?.message ? (
        <p className={`lm-install-status lm-install-${state.status}`}>{state.message}</p>
      ) : null}
      {busy && typeof state.percent === "number" ? (
        <div className="lm-bar" role="progressbar" aria-valuenow={state.percent}
             aria-valuemin={0} aria-valuemax={100}>
          <div className="lm-bar-fill" style={{ width: `${state.percent}%` }} />
        </div>
      ) : null}
      {state?.downloadUrl ? (
        <a className="lm-install-link" href={state.downloadUrl} target="_blank" rel="noreferrer">
          Get Ollama
        </a>
      ) : null}
    </div>
  );
}

function ModelRow({ item, expanded, onToggle, installState, onInstall, onCancelInstall }) {
  const { model, fit, plain } = item;
  return (
    <li className={`lm-row lm-row-${fit.tier}`}>
      <button
        type="button"
        className="lm-row-main"
        aria-expanded={expanded}
        onClick={() => onToggle(model.id)}
      >
        <span className="lm-row-name">{model.display_name}</span>
        <span className="lm-row-short">{plain.short}</span>
      </button>
      {expanded ? (
        <div className="lm-row-detail">
          <p className="lm-row-fit">{plain.fit}</p>
          <p>{plain.speed}</p>
          <p>{plain.download}</p>
          <p>{plain.compression}</p>
          {fit.data_caveat ? <p className="lm-caveat">{fit.data_caveat}</p> : null}
          <dl className="lm-facts">
            <div>
              <dt>Made by</dt>
              <dd>{String(model.id).split("/")[0]}</dd>
            </div>
            <div>
              <dt>Licence</dt>
              <dd>{model.license || "Not stated"}</dd>
            </div>
          </dl>
          <InstallButton
            item={item}
            state={installState}
            onInstall={onInstall}
            onCancel={onCancelInstall}
          />
        </div>
      ) : null}
    </li>
  );
}

export default function LocalModels({ onBack }) {
  const [goals, setGoals] = useState([]);
  const [goal, setGoal] = useState(() => readRememberedGoal());
  const [asking, setAsking] = useState(() => shouldAskFirst(readRememberedGoal()));
  const [machine, setMachine] = useState(null);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [rescanning, setRescanning] = useState(false);
  const [error, setError] = useState("");
  const [expandedId, setExpandedId] = useState(null);
  const [showTooBig, setShowTooBig] = useState(false);
  const [installs, setInstalls] = useState({});
  const installing = useRef(false);
  const installAbort = useRef(null);

  // The question and the scan start together. The scan is the slow half, so putting it
  // behind the question would leave someone watching a spinner before they had been
  // asked anything -- which is the one thing this screen is meant not to do.
  useEffect(() => {
    let alive = true;
    api
      .localModelGoals()
      .then((body) => alive && setGoals(body.goals || []))
      .catch(() => {});
    api
      .localModelScan()
      .then((body) => alive && setMachine(body))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(
    async (chosen, { fresh = false } = {}) => {
      if (!chosen) return;
      setLoading(true);
      setError("");
      try {
        const body = await api.localModelRecommendations({ goal: chosen, fresh });
        setMachine(body.machine);
        setItems(body.recommendations || []);
      } catch (caught) {
        setError(caught.message || "Neo could not work out what this computer can run.");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!goal || asking) return;
    load(goal);
  }, [goal, asking, load]);

  function pickGoal(chosen) {
    setGoal(chosen);
    rememberGoal(chosen);
    setAsking(false);
  }

  async function rescan() {
    setRescanning(true);
    try {
      await load(goal, { fresh: true });
    } finally {
      setRescanning(false);
    }
  }

  async function install(item) {
    // One at a time. Two concurrent pulls would fight for bandwidth and make both
    // progress bars lie.
    if (installing.current) return;
    installing.current = true;
    const controller = new AbortController();
    installAbort.current = { id: item.model.id, controller };
    const id = item.model.id;
    setInstalls((current) => ({
      ...current,
      [id]: { status: "working", message: "Starting…", percent: 0 },
    }));

    let finished = false;
    try {
      await api.installLocalModel(
        id,
        (event) => {
          finished = event.type === "done";
          setInstalls((current) => ({
            ...current,
            [id]: {
              status:
                event.type === "done"
                  ? "done"
                  : event.type === "error"
                    ? "failed"
                    : event.type === "cancelled"
                      ? "cancelled"
                      : event.type === "needs_engine"
                        ? "needs_engine"
                        : "working",
              message: event.message || "",
              percent: typeof event.percent === "number" ? event.percent : undefined,
              downloadUrl: event.download_url,
            },
          }));
        },
        controller.signal,
      );
      // Only worth another fetch when the model is now actually registered -- a
      // cancelled or failed attempt changed nothing, and reloading the list would
      // just reset everyone's scroll position for no reason.
      if (finished) {
        await load(goal);
      }
    } catch (caught) {
      setInstalls((current) => ({
        ...current,
        [id]: { status: "failed", message: caught.message || "The setup did not finish." },
      }));
    } finally {
      installing.current = false;
      installAbort.current = null;
    }
  }

  async function cancelInstall(item) {
    const id = item.model.id;
    // Tell the server first -- it is the side actually holding the connection to
    // Ollama, so this is what stops the download from continuing in the background.
    // Aborting the fetch only stops the browser from waiting on it.
    api.cancelLocalModelInstall(id).catch(() => {});
    if (installAbort.current?.id === id) {
      installAbort.current.controller.abort();
    }
    // The client stream is torn down by abort() before the server's own "cancelled"
    // event can round-trip back, so this is stated as settled rather than in-progress.
    setInstalls((current) => ({
      ...current,
      [id]: { status: "cancelled", message: "Download cancelled." },
    }));
  }

  const grouped = useMemo(() => groupByHowTheyRun(items), [items]);

  if (asking) {
    return (
      <div className="ws-panel lm">
        <header className="lm-head">
          <button type="button" className="ws-back" onClick={onBack}>
            <WorkspaceIcon name="back" /> Chat
          </button>
          <h1 className="lm-title">Run AI on this computer</h1>
          <p className="lm-sub">
            Neo can run AI models on your own machine. Nothing you type leaves it.
          </p>
        </header>
        <GoalPicker
          goals={goals}
          selected={goal}
          onPick={pickGoal}
          heading="What do you want to use it for?"
        />
        <p className="lm-quiet">
          {machine
            ? "Neo has finished looking at your computer."
            : "Neo is looking at your computer while you decide."}
        </p>
      </div>
    );
  }

  return (
    <div className="ws-panel lm">
      <header className="lm-head">
        <button type="button" className="ws-back" onClick={onBack}>
          <WorkspaceIcon name="back" /> Chat
        </button>
        <h1 className="lm-title">Models on this computer</h1>
        <p className="lm-sub">
          Neo can run AI models on your own machine. Nothing you type leaves it.
        </p>
      </header>

      {error ? <div className="neo-error lm-error">{error}</div> : null}

      <section className="lm-card lm-scan">
        {machine ? (
          <>
            <p className="lm-verdict">{machine.summary}</p>
            <div className="lm-scan-foot">
              <dl className="lm-specs">
                <div>
                  <dt>Processor</dt>
                  <dd>{machine.cpu_name}</dd>
                </div>
                <div>
                  <dt>Memory</dt>
                  <dd>{machine.total_memory_gb} GB</dd>
                </div>
                <div>
                  <dt>Available for AI</dt>
                  <dd>{machine.usable_memory_gb} GB</dd>
                </div>
              </dl>
              <button
                type="button"
                className="lm-rescan"
                onClick={rescan}
                disabled={rescanning}
              >
                {rescanning ? "Checking…" : "Check again"}
              </button>
            </div>
            {(machine.probe_notes || []).map((note) => (
              <p key={note.code} className={`lm-note lm-note-${note.severity}`}>
                {note.message}
              </p>
            ))}
          </>
        ) : (
          <p className="lm-verdict">Looking at what this computer can do…</p>
        )}
      </section>

      <GoalPicker goals={goals} selected={goal} onPick={pickGoal} heading="What do you want it for?" />

      {loading ? (
        <p className="lm-quiet">Working out what runs best here…</p>
      ) : (
        <>
          {grouped.top ? (
            <section className="lm-card lm-top">
              <p className="lm-flag">Best for this</p>
              <h2 className="lm-top-name">{grouped.top.model.display_name}</h2>
              <p className="lm-top-fit">{grouped.top.plain.fit}</p>
              <ul className="lm-top-facts">
                <li>{grouped.top.plain.speed}</li>
                <li>{grouped.top.plain.download}</li>
                <li>{grouped.top.plain.compression}</li>
              </ul>
              <InstallButton
                item={grouped.top}
                state={installs[grouped.top.model.id]}
                onInstall={install}
                onCancel={cancelInstall}
              />
            </section>
          ) : null}

          {grouped.groups.map((group) => (
            <section key={group.tier} className="lm-list-section">
              <h2 className="lm-h2">{group.label}</h2>
              <ul className="lm-list">
                {group.items.map((item) => (
                  <ModelRow
                    key={item.model.id}
                    item={item}
                    expanded={expandedId === item.model.id}
                    onToggle={(id) => setExpandedId(expandedId === id ? null : id)}
                    installState={installs[item.model.id]}
                    onInstall={install}
                    onCancelInstall={cancelInstall}
                  />
                ))}
              </ul>
            </section>
          ))}

          {grouped.tooBig.length ? (
            <section className="lm-list-section">
              <button
                type="button"
                className="lm-disclosure"
                aria-expanded={showTooBig}
                onClick={() => setShowTooBig(!showTooBig)}
              >
                {showTooBig ? "Hide" : "Show"} {grouped.tooBig.length}{" "}
                {grouped.tooBig.length === 1 ? "model" : "models"} this computer cannot run
              </button>
              {showTooBig ? (
                <ul className="lm-list">
                  {grouped.tooBig.map((item) => (
                    <li key={item.model.id} className="lm-row lm-row-too_big">
                      <span className="lm-row-name">{item.model.display_name}</span>
                      <span className="lm-row-reason">{item.fit.reason}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>
          ) : null}
        </>
      )}
    </div>
  );
}

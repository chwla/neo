import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";

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

// How many models the list shows before "Show all". The catalog runs to thirty-odd and
// nearly all of them fit a modern machine, so the full list on arrival was a wall of
// near-identical rows that buried the few worth reading.
export const PREVIEW_COUNT = 6;

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

/**
 * The groups cut down to the first ``count`` models, best first, unless everything was
 * asked for. Groups keep their order and labels; one emptied by the cut is dropped.
 */
export function previewGroups(groups = [], { showAll = false, count = PREVIEW_COUNT } = {}) {
  if (showAll) return groups;
  let left = count;
  return groups
    .map((group) => {
      const items = group.items.slice(0, Math.max(0, left));
      left -= items.length;
      return { ...group, items };
    })
    .filter((group) => group.items.length > 0);
}

/** Whether the wizard should run, i.e. whether any goal has been chosen before. */
export function shouldAskFirst(rememberedGoals) {
  return !rememberedGoals || rememberedGoals.length === 0;
}

/**
 * The goals stored for next time. Written comma-separated, so a value saved when only
 * one goal could be chosen ("coding") still reads back as that one goal.
 */
export function parseRememberedGoals(value) {
  return [...new Set(String(value || "").split(",").map((goal) => goal.trim()).filter(Boolean))];
}

/**
 * Add or remove one goal. The last one cannot be removed: with nothing chosen there is
 * nothing to rank models for, and an empty list would read as a broken screen.
 */
export function toggleGoal(selected, goal) {
  if (selected.includes(goal)) {
    return selected.length > 1 ? selected.filter((each) => each !== goal) : selected;
  }
  return [...selected, goal];
}

function readRememberedGoals() {
  try {
    return parseRememberedGoals(window.localStorage.getItem(REMEMBERED_GOAL));
  } catch {
    // A private window, or storage turned off. Asking again is a fine fallback.
    return [];
  }
}

function rememberGoals(goals) {
  try {
    window.localStorage.setItem(REMEMBERED_GOAL, goals.join(","));
  } catch {
    // Not worth surfacing: the screen still works, it just asks again next time.
  }
}

function GoalPicker({ goals, selected, onToggle, heading, children }) {
  return (
    <section className="lm-goals">
      <h2 className="lm-goals-heading">{heading}</h2>
      <p className="lm-goal-hint">Choose as many as you like.</p>
      <div className="lm-goal-grid" role="group" aria-label={heading}>
        {goals.map((goal) => {
          const on = selected.includes(goal.id);
          return (
            <button
              key={goal.id}
              type="button"
              className={`lm-goal${on ? " selected" : ""}`}
              aria-pressed={on}
              onClick={() => onToggle(goal.id)}
            >
              <span className="lm-goal-check" aria-hidden="true" />
              <span className="lm-goal-label">{goal.label}</span>
              <span className="lm-goal-detail">{goal.detail}</span>
            </button>
          );
        })}
      </div>
      {children}
    </section>
  );
}

// Once there are results the question is a filter, so it shrinks to one row of chips.
// The description each card carried is still there, on hover.
function GoalChips({ goals, selected, onToggle }) {
  return (
    <div className="lm-chipbar" role="group" aria-label="What do you want it for?">
      <span className="lm-chipbar-label">Use it for</span>
      {goals.map((goal) => {
        const on = selected.includes(goal.id);
        return (
          <button
            key={goal.id}
            type="button"
            className={`lm-chip${on ? " on" : ""}`}
            aria-pressed={on}
            title={goal.detail}
            onClick={() => onToggle(goal.id)}
          >
            {goal.label}
          </button>
        );
      })}
    </div>
  );
}

// The three things anyone deciding wants to know, as labelled sentences rather than a
// bulleted list -- the label is what makes each one findable at a glance.
function PlainFacts({ plain }) {
  return (
    <dl className="lm-plainfacts">
      <div>
        <dt>Speed</dt>
        <dd>{plain.speed}</dd>
      </div>
      <div>
        <dt>Download</dt>
        <dd>{plain.download}</dd>
      </div>
      <div>
        <dt>Quality</dt>
        <dd>{plain.compression}</dd>
      </div>
    </dl>
  );
}

function InstallButton({ item, state, onInstall, onCancel, primary = false }) {
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
          className={`neo-button${primary ? " primary" : ""} lm-install-button`}
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
    <li className={`lm-row lm-row-${fit.tier}${expanded ? " open" : ""}`}>
      <button
        type="button"
        className="lm-row-main"
        aria-expanded={expanded}
        onClick={() => onToggle(model.id)}
      >
        <span className="lm-row-name">{model.display_name}</span>
        {item.installed ? <span className="lm-badge">Installed</span> : null}
        <span className="lm-row-short">{plain.short}</span>
        <span className="lm-row-chevron" aria-hidden="true" />
      </button>
      {expanded ? (
        <div className="lm-row-detail">
          <p className="lm-row-fit">{plain.fit}</p>
          <PlainFacts plain={plain} />
          {fit.data_caveat ? <p className="lm-caveat">{fit.data_caveat}</p> : null}
          <div className="lm-row-foot">
            <InstallButton
              item={item}
              state={installState}
              onInstall={onInstall}
              onCancel={onCancelInstall}
            />
            <p className="lm-maker">
              {String(model.id).split("/")[0]} · {model.license || "Licence not stated"}
            </p>
          </div>
        </div>
      ) : null}
    </li>
  );
}

// The specifications worth showing, and only the ones that were actually established.
// A scan that could not see the machine -- Neo in a container with the engine on the
// host it cannot reach -- has no processor name and no memory figure, and printing
// "0 GB" beside an empty label reads as a broken screen rather than as an unanswered
// question. The note underneath is what explains it.
export function specsOf(machine) {
  const specs = [];
  if (machine.cpu_name) specs.push({ label: "Processor", value: machine.cpu_name });
  // Named here because the summary above only mentions it on a shared-memory machine,
  // and someone with a discrete card wants to see which one Neo found.
  if (machine.gpu_name && !machine.unified_memory) {
    specs.push({ label: "Graphics", value: machine.gpu_name });
  }
  if (machine.total_memory_gb > 0) {
    specs.push({ label: "Memory", value: `${machine.total_memory_gb} GB` });
  }
  if (machine.usable_memory_gb > 0) {
    specs.push({ label: "Available for AI", value: `${machine.usable_memory_gb} GB` });
  }
  return specs;
}

// The same specifications as one line under the title, each worded to stand alone.
const SPEC_PHRASE = {
  Memory: (value) => `${value} memory`,
  "Available for AI": (value) => `${value} usable for AI`,
};

export function specLine(machine) {
  return specsOf(machine)
    .map((spec) => (SPEC_PHRASE[spec.label] ? SPEC_PHRASE[spec.label](spec.value) : spec.value))
    .join(" · ");
}

export default function LocalModels() {
  const [goals, setGoals] = useState([]);
  const [chosen, setChosen] = useState(() => readRememberedGoals());
  const [asking, setAsking] = useState(() => shouldAskFirst(readRememberedGoals()));
  const [machine, setMachine] = useState(null);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [rescanning, setRescanning] = useState(false);
  const [error, setError] = useState("");
  const [expandedId, setExpandedId] = useState(null);
  const [showTooBig, setShowTooBig] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [installs, setInstalls] = useState({});
  const installing = useRef(false);
  const installAbort = useRef(null);
  // Goals can now be toggled in quick succession, and each toggle asks again. Only
  // the newest answer may land, or a slow earlier one would overwrite it.
  const latestLoad = useRef(0);

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
    async (picked, { fresh = false } = {}) => {
      if (!picked?.length) return;
      const ticket = ++latestLoad.current;
      setLoading(true);
      setError("");
      try {
        const body = await api.localModelRecommendations({ goals: picked, fresh });
        if (ticket !== latestLoad.current) return;
        setMachine(body.machine);
        setItems(body.recommendations || []);
      } catch (caught) {
        if (ticket !== latestLoad.current) return;
        setError(caught.message || "Neo could not work out what this computer can run.");
      } finally {
        if (ticket === latestLoad.current) setLoading(false);
      }
    },
    [],
  );

  // A goal remembered from an older version of the list is dropped rather than sent,
  // since the server rejects goals it does not know. If nothing is left, ask again.
  useEffect(() => {
    if (!goals.length) return;
    setChosen((current) => {
      const known = current.filter((id) => goals.some((goal) => goal.id === id));
      if (known.length === current.length) return current;
      if (!known.length) setAsking(true);
      return known;
    });
  }, [goals]);

  const chosenKey = chosen.join(",");
  useEffect(() => {
    if (!chosen.length || asking) return;
    load(chosen);
    // chosenKey stands in for chosen: the same goals in a new array are not a change.
  }, [chosenKey, asking, load]);

  // In the first-visit question, choosing only marks a goal; "Show me models" moves
  // on. Afterwards each change applies straight away, like the filter it now is.
  function toggleInQuestion(id) {
    setChosen((current) => (current.includes(id) ? current.filter((each) => each !== id) : [...current, id]));
  }

  function finishQuestion() {
    if (!chosen.length) return;
    rememberGoals(chosen);
    setAsking(false);
  }

  function toggleOnResults(id) {
    setChosen((current) => {
      const next = toggleGoal(current, id);
      if (next !== current) rememberGoals(next);
      return next;
    });
  }

  async function rescan() {
    setRescanning(true);
    try {
      await load(chosen, { fresh: true });
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
        await load(chosen);
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
  const fitCount = grouped.groups.reduce((total, group) => total + group.items.length, 0);
  const shownGroups = previewGroups(grouped.groups, { showAll });

  if (asking) {
    return (
      <div className="ws-panel lm lm-ask">
        <div className="lm-ask-inner">
          <header className="lm-ask-head">
            <h1 className="lm-title">Run AI on this computer</h1>
            <p className="lm-sub">
              Neo can run AI models on your own machine. Nothing you type leaves it.
            </p>
          </header>
          <GoalPicker
            goals={goals}
            selected={chosen}
            onToggle={toggleInQuestion}
            heading="What do you want to use it for?"
          >
            {goals.length ? (
              <button
                type="button"
                className="neo-button primary lm-goal-continue"
                onClick={finishQuestion}
                disabled={!chosen.length}
              >
                {chosen.length > 1 ? `Show models for these ${chosen.length}` : "Show me models"}
              </button>
            ) : null}
          </GoalPicker>
          <p className="lm-quiet">
            {machine
              ? "Neo has finished looking at your computer."
              : "Neo is looking at your computer while you decide."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="ws-panel lm">
      <div className="lm-inner">
        <header className="lm-head">
          <div className="lm-head-text">
            <h1 className="lm-title">Models on this computer</h1>
            <p className="lm-verdict">
              {machine ? machine.summary : "Looking at what this computer can do…"}
            </p>
            {machine ? <p className="lm-specline">{specLine(machine)}</p> : null}
          </div>
          <button type="button" className="lm-rescan" onClick={rescan} disabled={rescanning}>
            {rescanning ? "Checking…" : "Check again"}
          </button>
        </header>

        {(machine?.probe_notes || []).map((note) => (
          <p key={note.code} className={`lm-note lm-note-${note.severity}`}>
            {note.message}
          </p>
        ))}

        {error ? <div className="neo-error lm-error">{error}</div> : null}

        <GoalChips goals={goals} selected={chosen} onToggle={toggleOnResults} />

        {loading ? (
          <p className="lm-quiet">Working out what runs best here…</p>
        ) : (
          <>
            {grouped.top ? (
              <section className="lm-top">
                <div className="lm-top-main">
                  <p className="lm-flag">Best for this</p>
                  <h2 className="lm-top-name">
                    {grouped.top.model.display_name}
                    {grouped.top.installed ? <span className="lm-badge">Installed</span> : null}
                  </h2>
                  <p className="lm-top-fit">{grouped.top.plain.fit}</p>
                  <PlainFacts plain={grouped.top.plain} />
                </div>
                <div className="lm-top-action">
                  <InstallButton
                    item={grouped.top}
                    state={installs[grouped.top.model.id]}
                    onInstall={install}
                    onCancel={cancelInstall}
                    primary
                  />
                </div>
              </section>
            ) : null}

            {shownGroups.length ? (
              <section className="lm-listbox">
                {shownGroups.map((group) => (
                  <div key={group.tier} className={`lm-group lm-group-${group.tier}`}>
                    <h2 className="lm-group-label">{group.label}</h2>
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
                  </div>
                ))}
                {fitCount > PREVIEW_COUNT ? (
                  <button
                    type="button"
                    className="lm-more"
                    aria-expanded={showAll}
                    onClick={() => setShowAll(!showAll)}
                  >
                    {showAll ? "Show fewer" : `Show all ${fitCount} that run here`}
                  </button>
                ) : null}
              </section>
            ) : null}

            {grouped.tooBig.length ? (
              <section className="lm-toobig">
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
                  <ul className="lm-list lm-list-toobig">
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
    </div>
  );
}

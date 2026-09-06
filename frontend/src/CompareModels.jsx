import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import WorkspaceIcon from "./WorkspaceIcon.jsx";

/**
 * Comparing models side by side on a workload.
 *
 * Two things shape this screen.
 *
 * **It never names a winner.** There is no verdict, no ranking and no tie-break. It puts
 * the numbers next to each other and, where they disagree, says so plainly -- "A scored
 * higher, B was faster" -- and leaves the choice where it belongs. Which of those two
 * matters is a question about the reader's work, not about the models.
 *
 * **The evidence is never hidden behind the score.** Every cell opens onto the prompt
 * that was sent, what the model actually said, how each check was decided, what it cost,
 * and the settings it ran under. A score nobody can audit is a number, not a finding.
 *
 * The pure helpers below are exported because they are the parts worth testing: the suite
 * renders to static markup and cannot click anything.
 */

const REMEMBERED = "neo.compareModels.setup";

/** How a check result is shown in a cell. Never a number. */
export const CHECK_MARK = { passed: "✓", failed: "✗", skipped: "–" };

/** The six states a cell moves through, in the words the grid shows them. */
export const CELL_STATE = {
  queued: "Queued",
  generating: "Generating…",
  evaluating: "Evaluating…",
  complete: "Complete",
  error: "Error",
  cancelled: "Cancelled",
};

export function readRemembered() {
  try {
    return JSON.parse(window.localStorage.getItem(REMEMBERED) || "null") || null;
  } catch {
    // A private window, or storage turned off. Starting from the defaults is fine.
    return null;
  }
}

export function remember(setup) {
  try {
    window.localStorage.setItem(REMEMBERED, JSON.stringify(setup));
  } catch {
    // Not worth surfacing: the screen still works, it just forgets between visits.
  }
}

/**
 * Whether the current choices amount to a runnable comparison, and if not, why.
 *
 * Returns the reason rather than only a boolean so the button can say what is missing
 * instead of being mysteriously disabled.
 */
export function whatIsMissing({
  selected = [],
  useCase = "",
  prompts = [],
  deterministic = true,
  judgeOn = false,
  limits = {},
}) {
  const min = limits.min_models || 2;
  const max = limits.max_models || 4;
  if (selected.length < min) {
    const short = min - selected.length;
    return `Pick ${short} more model${short === 1 ? "" : "s"}.`;
  }
  if (selected.length > max) {
    return `Neo compares up to ${max} models at a time.`;
  }
  if (useCase === "custom" && !prompts.some((item) => item.trim())) {
    return "Write at least one question you want them compared on.";
  }
  if (!deterministic && !judgeOn) {
    return "Nothing would be evaluated. Turn on rule-based checks or the judge.";
  }
  return "";
}

/**
 * Assemble the grid the screen draws: one row per task, one cell per model.
 *
 * Built from whatever has arrived so far rather than from a finished run, which is what
 * lets the same function draw a run in progress and a finished one.
 */
export function buildGrid({ tasks = [], contenders = [], outcomes = {}, states = {} }) {
  return tasks.map((task) => ({
    task,
    // One sentence from the judge covers the whole row, so it is lifted out of the
    // cells and shown once rather than repeated under every column.
    judgeNote:
      contenders
        .map((item) => outcomes[`${item.id}::${task.id}`]?.judge_note)
        .find(Boolean) || "",
    cells: contenders.map((contender) => {
      const key = `${contender.id}::${task.id}`;
      return {
        key,
        contender,
        outcome: outcomes[key] || null,
        state: states[key] || "queued",
      };
    }),
  }));
}

/** A duration in the words a person would use. Milliseconds are never shown. */
export function saidAsTime(ms) {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  return seconds < 10 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds)}s`;
}

/** The rule-based score as a percentage, or a dash when no rule applied. */
export function saidAsScore(score) {
  return score === null || score === undefined ? "–" : `${Math.round(score * 100)}%`;
}

/** The judge's rating out of ten, or a dash. Never combined with the score above. */
export function saidAsRating(judge) {
  return judge === null || judge === undefined ? "–" : `${(judge * 10).toFixed(1)}/10`;
}

export function progressLabel(completed, total) {
  return `${completed} / ${total} responses complete`;
}

/**
 * Say what the numbers show, without saying which model is better.
 *
 * This is the one piece of prose that touches two models at once, so it is where a
 * verdict would creep in if one were going to. It states the comparison as arithmetic --
 * who scored higher, who was quicker -- and, whenever those point at different models,
 * hands the decision back rather than resolving it. There is deliberately no tie
 * threshold: a difference of one point is reported as a difference of one point.
 */
export function describeTradeoff(summaries = []) {
  const usable = summaries.filter((item) => item.tasks_answered && !item.error);
  if (usable.length < 2) {
    return usable.length === 1
      ? "Only one model finished, so there is nothing to compare it against."
      : "No model finished enough of this to compare.";
  }

  const scored = usable.filter((item) => item.score !== null && item.score !== undefined);
  const rated = usable.filter(
    (item) => item.judge_score !== null && item.judge_score !== undefined,
  );
  const quickest = [...usable].sort((a, b) => a.median_duration_ms - b.median_duration_ms)[0];
  const name = (item) => item.contender.display_name;

  const parts = [];
  if (scored.length >= 2) {
    const ranked = [...scored].sort((a, b) => b.score - a.score);
    parts.push(
      ranked[0].score === ranked[1].score
        ? `${name(ranked[0])} and ${name(ranked[1])} scored the same on the checks (${saidAsScore(ranked[0].score)})`
        : `${name(ranked[0])} scored higher on the checks (${saidAsScore(ranked[0].score)} against ${saidAsScore(ranked[1].score)})`,
    );
  }
  if (rated.length >= 2) {
    const ranked = [...rated].sort((a, b) => b.judge_score - a.judge_score);
    if (ranked[0].judge_score !== ranked[1].judge_score) {
      parts.push(`the judge rated ${name(ranked[0])} highest (${saidAsRating(ranked[0].judge_score)})`);
    }
  }
  parts.push(
    `${name(quickest)} answered fastest (${saidAsTime(quickest.median_duration_ms)} on a typical task)`,
  );

  const sentence = `${parts.join(", and ")}.`;
  const topScorer = scored.length >= 2 ? [...scored].sort((a, b) => b.score - a.score)[0] : null;
  const disagree =
    topScorer && topScorer.contender.id !== quickest.contender.id && topScorer.score !== null;
  return disagree
    ? `${sentence} Which of those matters more depends on what you are using it for.`
    : sentence;
}

/** Which evaluation a cell actually got, in a phrase, so a dash is never a mystery. */
export function evaluationNote(outcome, { deterministic = true, judgeOn = false } = {}) {
  if (!outcome || outcome.status !== "ok") return "";
  if (outcome.evaluation === "deterministic") return "";
  if (judgeOn && (outcome.judge_score === null || outcome.judge_score === undefined)) {
    return "The judge did not rate this one.";
  }
  if (judgeOn) return "";
  return deterministic
    ? "No deterministic evaluation available. Enable the LLM judge to evaluate this task."
    : "Rule-based checks are switched off for this run.";
}

function ModelPicker({ candidates, selected, onToggle, max }) {
  return (
    <section className="cmp-block">
      <h2 className="cmp-h2">Models</h2>
      <div className="cmp-picker" role="group" aria-label="Models to compare">
        {candidates.map((item) => {
          const chosen = selected.includes(item.id);
          const full = !chosen && selected.length >= max;
          return (
            <button
              key={item.id}
              type="button"
              className={`cmp-candidate${chosen ? " selected" : ""}`}
              aria-pressed={chosen}
              disabled={full}
              onClick={() => onToggle(item.id)}
            >
              <span className="cmp-candidate-name">{item.display_name}</span>
              {item.version ? (
                <span className="cmp-candidate-version">{item.version}</span>
              ) : null}
              <span className="cmp-candidate-where">
                {item.local ? "On this computer" : "Over the internet"}
              </span>
              {item.note ? <span className="cmp-candidate-note">{item.note}</span> : null}
            </button>
          );
        })}
      </div>
      {candidates.length < 2 ? (
        <p className="cmp-quiet">
          Only one model is set up. Add another in Settings, or download one from Local
          Models, and they can be compared here.
        </p>
      ) : null}
    </section>
  );
}

function QuestionList({ prompts, onChange, onAdd, onRemove, max }) {
  return (
    <section className="cmp-block">
      <h2 className="cmp-h2">Questions</h2>
      <ol className="cmp-questions">
        {prompts.map((prompt, index) => (
          // eslint-disable-next-line react/no-array-index-key -- position is the identity
          <li key={index} className="cmp-question">
            <span className="cmp-question-number">{index + 1}</span>
            <textarea
              className="cmp-prompt"
              rows={3}
              value={prompt}
              aria-label={`Question ${index + 1}`}
              placeholder="Ask them all the same thing…"
              onChange={(event) => onChange(index, event.target.value)}
            />
            {prompts.length > 1 ? (
              <button
                type="button"
                className="cmp-question-remove"
                aria-label={`Remove question ${index + 1}`}
                onClick={() => onRemove(index)}
              >
                ×
              </button>
            ) : null}
          </li>
        ))}
      </ol>
      {prompts.length < max ? (
        <button type="button" className="neo-button secondary cmp-add" onClick={onAdd}>
          + Add question
        </button>
      ) : null}
      <p className="cmp-quiet">
        Every model is asked all of these. There is no right answer to check against, so
        the replies are shown side by side for you to judge — or a model can rate them.
      </p>
    </section>
  );
}

function Cell({ cell, onOpen, isOpen }) {
  const { outcome, state } = cell;
  if (!outcome) {
    return (
      <td className={`cmp-cell cmp-cell-${state}`}>
        <span className="cmp-state">{CELL_STATE[state] || CELL_STATE.queued}</span>
        {state === "generating" ? <span className="cmp-pending" aria-hidden="true" /> : null}
      </td>
    );
  }
  const broken = outcome.status !== "ok";
  return (
    <td className={`cmp-cell${broken ? " cmp-cell-error" : ""}${isOpen ? " cmp-cell-open" : ""}`}>
      {broken ? (
        <>
          <span className="cmp-state cmp-state-error">
            {CELL_STATE[outcome.state] || CELL_STATE.error}
          </span>
          <span className="cmp-cell-message">{outcome.message || "No answer."}</span>
        </>
      ) : (
        <>
          <div className="cmp-cell-top">
            <span className="cmp-cell-score">{saidAsScore(outcome.score)}</span>
            {outcome.judge_score !== null && outcome.judge_score !== undefined ? (
              <span className="cmp-cell-rating">{saidAsRating(outcome.judge_score)}</span>
            ) : null}
            <span className="cmp-cell-time">{saidAsTime(outcome.duration_ms)}</span>
          </div>
          {outcome.checks.length ? (
            <ul className="cmp-checks">
              {outcome.checks.map((check) => (
                <li key={check.label} className={`cmp-check cmp-check-${check.status}`}>
                  <span className="cmp-check-mark" aria-hidden="true">
                    {CHECK_MARK[check.status]}
                  </span>
                  <span className="cmp-check-label">{check.label}</span>
                </li>
              ))}
            </ul>
          ) : null}
          <p className="cmp-cell-preview">{(outcome.answer || "").slice(0, 90)}</p>
        </>
      )}
      <button type="button" className="cmp-open" onClick={() => onOpen(cell)}>
        {isOpen ? "Hide details" : "Open"}
      </button>
    </td>
  );
}

function Grid({ rows, contenders, onOpen, openKey }) {
  if (!rows.length) return null;
  return (
    <div className="cmp-grid-scroll">
      <table className="cmp-grid">
        <thead>
          <tr>
            <th className="cmp-grid-corner" scope="col">What they were asked</th>
            {contenders.map((item) => (
              <th key={item.id} className="cmp-grid-head" scope="col">
                <span className="cmp-grid-model">{item.display_name}</span>
                {item.version ? (
                  <span className="cmp-grid-version">{item.version}</span>
                ) : null}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.task.id}>
              <th className="cmp-grid-task" scope="row">
                <span className="cmp-task-label">{row.task.label}</span>
                <p className="cmp-task-text">{row.task.prompt}</p>
                {row.task.rubric ? (
                  <p className="cmp-task-rubric">{row.task.rubric}</p>
                ) : null}
                {row.judgeNote ? <p className="cmp-judge-note">{row.judgeNote}</p> : null}
              </th>
              {row.cells.map((cell) => (
                <Cell
                  key={cell.key}
                  cell={cell}
                  onOpen={onOpen}
                  isOpen={openKey === cell.key}
                />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CellDetail({ cell, task, config, onClose, judgeOn, deterministic }) {
  if (!cell || !task) return null;
  const { outcome } = cell;
  const generation = config?.generation || {};
  const note = evaluationNote(outcome, { deterministic, judgeOn });
  return (
    <section className="cmp-card cmp-detail">
      <header className="cmp-detail-head">
        <h2 className="cmp-detail-title">
          {cell.contender.display_name} · {task.label}
        </h2>
        <button type="button" className="neo-button secondary" onClick={onClose}>
          Close
        </button>
      </header>

      <h3 className="cmp-detail-h3">Prompt</h3>
      <pre className="cmp-answer-body">{task.prompt}</pre>

      <h3 className="cmp-detail-h3">What it said</h3>
      <pre className="cmp-answer-body">{outcome?.answer || "(nothing)"}</pre>

      {outcome?.thinking ? (
        <>
          <h3 className="cmp-detail-h3">What it thought first</h3>
          <pre className="cmp-answer-body">{outcome.thinking}</pre>
        </>
      ) : null}

      <h3 className="cmp-detail-h3">Evaluation</h3>
      {outcome?.checks?.length ? (
        <>
          <p className="cmp-detail-line">
            Rule-based: <strong>{saidAsScore(outcome.score)}</strong>
            {task.rubric ? <span className="cmp-detail-rubric"> {task.rubric}</span> : null}
          </p>
          <ul className="cmp-checks cmp-checks-full">
            {outcome.checks.map((check) => (
              <li key={check.label} className={`cmp-check cmp-check-${check.status}`}>
                <span className="cmp-check-mark" aria-hidden="true">
                  {CHECK_MARK[check.status]}
                </span>
                <span className="cmp-check-label">{check.label}</span>
                {check.detail ? <span className="cmp-check-detail">{check.detail}</span> : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {outcome?.judge_score !== null && outcome?.judge_score !== undefined ? (
        <p className="cmp-detail-line">
          LLM judge: <strong>{saidAsRating(outcome.judge_score)}</strong>
          {config?.judge?.display_name ? ` from ${config.judge.display_name}` : ""}
          {outcome.judge_note ? <span className="cmp-detail-rubric"> {outcome.judge_note}</span> : null}
        </p>
      ) : null}
      {note ? <p className="cmp-detail-note">{note}</p> : null}

      <h3 className="cmp-detail-h3">Timing</h3>
      <dl className="cmp-detail-facts">
        <div>
          <dt>First output</dt>
          <dd>{saidAsTime(outcome?.time_to_first_token_ms) || "not recorded"}</dd>
        </div>
        <div>
          <dt>Whole answer</dt>
          <dd>{saidAsTime(outcome?.duration_ms) || "–"}</dd>
        </div>
        <div>
          <dt>Length</dt>
          <dd>{outcome?.completion_tokens ? `${outcome.completion_tokens} tokens` : "–"}</dd>
        </div>
        {outcome?.thinking_tokens ? (
          <div>
            <dt>Of that, thinking</dt>
            <dd>about {outcome.thinking_tokens} tokens</dd>
          </div>
        ) : null}
      </dl>

      <h3 className="cmp-detail-h3">Settings this ran under</h3>
      <dl className="cmp-detail-facts">
        <div>
          <dt>Model</dt>
          <dd>
            {cell.contender.model}
            {cell.contender.version ? ` (${cell.contender.version})` : ""}
          </dd>
        </div>
        <div>
          <dt>Thinking</dt>
          <dd>{generation.allow_thinking ? "On" : "Off"}</dd>
        </div>
        <div>
          <dt>Output limit</dt>
          <dd>{generation.max_output_tokens || task.max_tokens} tokens</dd>
        </div>
        <div>
          <dt>Temperature</dt>
          <dd>{generation.temperature ?? 0}</dd>
        </div>
        {config?.grader_version ? (
          <div>
            <dt>Checks</dt>
            <dd>version {config.grader_version}</dd>
          </div>
        ) : null}
      </dl>

      {outcome?.message ? (
        <>
          <h3 className="cmp-detail-h3">What went wrong</h3>
          <p className="cmp-detail-error">{outcome.message}</p>
        </>
      ) : null}
    </section>
  );
}

function Scoreboard({ summaries, judgeOn }) {
  if (!summaries.length) return null;
  return (
    <div className="cmp-grid-scroll">
      <table className="cmp-board">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Rule-based</th>
            {judgeOn ? <th scope="col">LLM judge</th> : null}
            <th scope="col">Typical answer</th>
            <th scope="col">First output</th>
            <th scope="col">Answered</th>
          </tr>
        </thead>
        <tbody>
          {summaries.map((item) => (
            <tr key={item.contender.id}>
              <th scope="row">
                <span className="cmp-board-model">{item.contender.display_name}</span>
                {item.contender.version ? (
                  <span className="cmp-board-version">{item.contender.version}</span>
                ) : null}
              </th>
              {item.error ? (
                <td colSpan={judgeOn ? 5 : 4} className="cmp-board-error">
                  {item.error}
                </td>
              ) : (
                <>
                  <td>
                    {saidAsScore(item.score)}
                    {item.checks_applied ? (
                      <span className="cmp-board-sub">
                        {item.checks_passed}/{item.checks_applied} checks
                      </span>
                    ) : null}
                  </td>
                  {judgeOn ? <td>{saidAsRating(item.judge_score)}</td> : null}
                  <td>{saidAsTime(item.median_duration_ms)}</td>
                  <td>{saidAsTime(item.median_time_to_first_token_ms) || "–"}</td>
                  <td>
                    {item.tasks_answered}/{item.tasks_attempted}
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function CompareModels({ onBack }) {
  const [candidates, setCandidates] = useState([]);
  const [useCases, setUseCases] = useState([]);
  const [meta, setMeta] = useState({ min_models: 2, max_models: 4, max_custom_prompts: 10 });
  const [selected, setSelected] = useState([]);
  const [useCase, setUseCase] = useState("coding");
  const [prompts, setPrompts] = useState([""]);
  const [depth, setDepth] = useState(3);
  const [deterministic, setDeterministic] = useState(true);
  const [judgeId, setJudgeId] = useState("");
  const [rubric, setRubric] = useState("");
  const [temperature, setTemperature] = useState(0);
  const [maxTokens, setMaxTokens] = useState("");
  const [allowThinking, setAllowThinking] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [phase, setPhase] = useState("setup");
  const [config, setConfig] = useState(null);
  const [outcomes, setOutcomes] = useState({});
  const [states, setStates] = useState({});
  const [comparison, setComparison] = useState(null);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const [notice, setNotice] = useState("");
  const [estimate, setEstimate] = useState(0);
  const [openKey, setOpenKey] = useState("");
  const runRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await api.compareCandidates();
        if (cancelled) return;
        setCandidates(payload.candidates || []);
        setUseCases(payload.use_cases || []);
        setMeta({
          min_models: payload.min_models,
          max_models: payload.max_models,
          max_custom_prompts: payload.max_custom_prompts,
          limits: payload.limits || {},
        });
        setRubric(payload.default_rubric || "");

        const saved = readRemembered();
        const available = new Set((payload.candidates || []).map((item) => item.id));
        // Only restore models that are still set up; a remembered choice that has since
        // been deleted would make the run fail for a reason the user cannot see.
        const restored = (saved?.selected || []).filter((id) => available.has(id));
        setSelected(
          restored.length >= 2
            ? restored
            : (payload.candidates || []).slice(0, 2).map((item) => item.id),
        );
        if (saved?.useCase) setUseCase(saved.useCase);
        if (saved?.depth) setDepth(saved.depth);
      } catch (caught) {
        if (!cancelled) setError(caught.message || "Could not load the models.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const useCaseRow = useCases.find((item) => item.id === useCase);
  const isCustom = useCase === "custom";
  const maxDepth = useCaseRow?.task_count || 1;
  const effectiveDepth = isCustom ? prompts.filter((p) => p.trim()).length : Math.min(depth, maxDepth || 1);
  const judgeOn = Boolean(judgeId);
  const deterministicAvailable = useCaseRow ? useCaseRow.deterministic_available : true;

  useEffect(() => {
    remember({ selected, useCase, depth });
  }, [selected, useCase, depth]);

  const missing = whatIsMissing({
    selected,
    useCase,
    prompts,
    deterministic: deterministic && deterministicAvailable,
    judgeOn,
    limits: meta,
  });

  const tasks = config?.tasks || [];
  const contenders = config?.contenders || [];
  const rows = useMemo(
    () => buildGrid({ tasks, contenders, outcomes, states }),
    [tasks, contenders, outcomes, states],
  );
  const openCell = useMemo(
    () => rows.flatMap((row) => row.cells).find((cell) => cell.key === openKey) || null,
    [rows, openKey],
  );
  const openTask = openCell
    ? tasks.find((task) => openKey.endsWith(`::${task.id}`)) || null
    : null;

  function toggle(id) {
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  // Priced whenever the configuration changes, so the wait shown is this run's wait
  // rather than a generic figure that is wrong for most configurations.
  useEffect(() => {
    if (missing || phase === "running" || loading) return undefined;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const payload = await api.comparePlan(requestBody());
        if (!cancelled) setEstimate(payload.estimate_seconds || 0);
      } catch {
        if (!cancelled) setEstimate(0);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    missing, phase, loading, selected, useCase, depth, prompts, judgeId, deterministic,
    temperature, maxTokens, allowThinking,
  ]);

  function requestBody() {
    return {
      model_ids: selected,
      use_case: useCase,
      depth: effectiveDepth,
      prompts: isCustom ? prompts.filter((item) => item.trim()) : [],
      judge_id: judgeId || null,
      judge_rubric: judgeOn ? rubric : "",
      deterministic: deterministic && deterministicAvailable,
      temperature: Number(temperature) || 0,
      max_output_tokens: maxTokens ? Number(maxTokens) : null,
      allow_thinking: allowThinking,
    };
  }

  const start = useCallback(async () => {
    if (missing) return;
    const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const controller = new AbortController();
    runRef.current = { runId, controller };

    setPhase("running");
    setError("");
    setNotice("");
    setComparison(null);
    setOutcomes({});
    setStates({});
    setConfig(null);
    setOpenKey("");
    setProgress({ completed: 0, total: 0 });

    try {
      await api.runComparison(
        { ...requestBody(), run_id: runId },
        (event) => {
          if (event.type === "started") {
            setConfig(event.config);
            setProgress({ completed: 0, total: event.total_cells || 0 });
            setEstimate(event.config?.estimate_seconds || 0);
          } else if (event.type === "cell_state") {
            setStates((current) => ({
              ...current,
              [`${event.contender_id}::${event.task_id}`]: event.state,
            }));
          } else if (event.type === "contender_failed") {
            // Every cell this model has not reached is settled now rather than left
            // spinning: the run continues without it, and a column of "Queued" that
            // never changes is the worst thing the grid could show.
            setStates((current) => {
              const next = { ...current };
              (event.tasks || []).forEach((taskId) => {
                next[`${event.contender_id}::${taskId}`] = "error";
              });
              return next;
            });
            setNotice(`${event.contender_id}: ${event.message}`);
          } else if (event.type === "result") {
            setOutcomes((current) => ({
              ...current,
              [`${event.contender_id}::${event.task_id}`]: event,
            }));
            setStates((current) => ({
              ...current,
              [`${event.contender_id}::${event.task_id}`]: event.state,
            }));
          } else if (event.type === "progress") {
            setProgress({ completed: event.completed, total: event.total });
          } else if (event.type === "judging") {
            setNotice(`${event.judge} is rating the answers…`);
          } else if (event.type === "judged") {
            setNotice("");
            setOutcomes((current) => {
              const key = `${event.contender_id}::${event.task_id}`;
              const found = current[key];
              return found
                ? {
                  ...current,
                  [key]: {
                    ...found,
                    judge_score: event.judge_score,
                    judge_note: event.judge_note,
                  },
                }
                : current;
            });
          } else if (event.type === "judge_failed") {
            setNotice(event.message || "");
          } else if (event.type === "done") {
            setComparison(event.comparison);
            setConfig(event.comparison.config);
            // Anything still unsettled ends here. Without this a cancelled run leaves
            // cells reading "Generating…" for ever.
            setStates((current) => {
              const next = { ...current };
              Object.keys(next).forEach((key) => {
                if (next[key] === "generating" || next[key] === "evaluating") {
                  next[key] = event.comparison.cancelled ? "cancelled" : "error";
                }
              });
              return next;
            });
          }
        },
        controller.signal,
      );
    } catch (caught) {
      setError(caught.message || "The comparison did not finish.");
    } finally {
      setPhase("done");
      runRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missing, selected, useCase, effectiveDepth, prompts, judgeId, rubric, deterministic,
    temperature, maxTokens, allowThinking, deterministicAvailable]);

  function stop() {
    const live = runRef.current;
    if (!live) return;
    // The server is told first: it holds the connections to the models, so this is what
    // actually stops them generating. Aborting the fetch only stops the browser waiting.
    api.cancelComparison(live.runId).catch(() => {});
    setNotice("Stopping…");
  }

  const running = phase === "running";
  const summaries = comparison?.summaries || [];

  return (
    <div className="ws-panel cmp">
      <header className="cmp-head">
        <button type="button" className="ws-back" onClick={onBack}>
          <WorkspaceIcon name="back" /> Chat
        </button>
        <h1 className="cmp-title">Compare models</h1>
        <p className="cmp-sub">
          Compare models on tasks you actually care about. Neo asks each of them the same
          questions and shows you what came back, how it was scored and how long it took —
          then you decide which one suits your work. For models running on this computer,
          nothing you type leaves it.
        </p>
      </header>

      {error ? <div className="neo-error cmp-error">{error}</div> : null}

      {loading ? (
        <p className="cmp-quiet">Looking at which models are set up…</p>
      ) : (
        <>
          <ModelPicker
            candidates={candidates}
            selected={selected}
            onToggle={toggle}
            max={meta.max_models || 4}
          />

          <section className="cmp-block">
            <h2 className="cmp-h2">What do you want to compare?</h2>
            <div className="cmp-goal-row" role="group" aria-label="What to compare them on">
              {useCases.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`cmp-goal${useCase === item.id ? " selected" : ""}`}
                  aria-pressed={useCase === item.id}
                  onClick={() => setUseCase(item.id)}
                >
                  <span className="cmp-goal-label">{item.label}</span>
                  <span className="cmp-goal-detail">{item.detail}</span>
                </button>
              ))}
            </div>
          </section>

          {isCustom ? (
            <QuestionList
              prompts={prompts}
              max={meta.max_custom_prompts || 10}
              onChange={(index, value) =>
                setPrompts((current) => current.map((item, i) => (i === index ? value : item)))
              }
              onAdd={() => setPrompts((current) => [...current, ""])}
              onRemove={(index) =>
                setPrompts((current) => current.filter((_, i) => i !== index))
              }
            />
          ) : (
            <section className="cmp-block cmp-depth">
              <label className="cmp-label" htmlFor="cmp-depth">
                Questions — more is steadier, and takes longer
              </label>
              <input
                id="cmp-depth"
                className="cmp-range"
                type="range"
                min={1}
                max={Math.max(1, maxDepth)}
                value={effectiveDepth}
                onChange={(event) => setDepth(Number(event.target.value))}
              />
              <span className="cmp-depth-value">
                {effectiveDepth} of {maxDepth}
              </span>
            </section>
          )}

          <section className="cmp-block cmp-options">
            <h2 className="cmp-h2">Evaluation</h2>
            <label className={`cmp-option${deterministicAvailable ? "" : " disabled"}`}>
              <input
                type="checkbox"
                checked={deterministic && deterministicAvailable}
                disabled={!deterministicAvailable}
                onChange={(event) => setDeterministic(event.target.checked)}
              />
              <span>
                Rule-based evaluation when available
                <span className="cmp-option-detail">
                  {deterministicAvailable
                    ? "Objective checks with a known right answer — valid JSON, code that parses, the correct value. Instant, and you can read every check."
                    : "No deterministic evaluation available for your own questions. Enable the LLM judge to evaluate them."}
                </span>
              </span>
            </label>

            <label className="cmp-option">
              <input
                type="checkbox"
                checked={judgeOn}
                onChange={(event) =>
                  setJudgeId(event.target.checked ? candidates[0]?.id || "" : "")
                }
              />
              <span>
                Use an LLM judge
                <span className="cmp-option-detail">
                  A model reads the answers and rates them out of ten. Reported separately
                  from the rule-based score, never mixed into it.
                </span>
              </span>
            </label>

            {judgeOn ? (
              <div className="cmp-judge-setup">
                <label className="cmp-label" htmlFor="cmp-judge">
                  Which model judges
                </label>
                <select
                  id="cmp-judge"
                  className="cmp-select"
                  value={judgeId}
                  onChange={(event) => setJudgeId(event.target.value)}
                >
                  {candidates.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.display_name}
                      {selected.includes(item.id) ? " (also being compared)" : ""}
                    </option>
                  ))}
                </select>
                {selected.includes(judgeId) ? (
                  <p className="cmp-warn">
                    This model is in the comparison. Models tend to prefer their own
                    answers, so read its ratings with that in mind.
                  </p>
                ) : null}
                <label className="cmp-label" htmlFor="cmp-rubric">
                  Judge rubric
                </label>
                <textarea
                  id="cmp-rubric"
                  className="cmp-prompt"
                  rows={5}
                  value={rubric}
                  onChange={(event) => setRubric(event.target.value)}
                />
              </div>
            ) : null}
          </section>

          <details className="cmp-advanced">
            <summary className="cmp-h2">Advanced</summary>
            <div className="cmp-advanced-body">
              <label className="cmp-option">
                <input
                  type="checkbox"
                  checked={allowThinking}
                  onChange={(event) => setAllowThinking(event.target.checked)}
                />
                <span>
                  Thinking
                  <span className="cmp-option-detail">
                    Let models reason out loud before answering. Slower, and often
                    better — measured here, gemma4 got the bat-and-ball puzzle right only
                    with this on. Off by default because a model given a short budget can
                    spend all of it thinking and never answer. It shows up mostly on your
                    own questions: the built-in ones ask for a terse answer, which most
                    models take as a reason not to reason at all.
                  </span>
                </span>
              </label>
              <div className="cmp-field">
                <label className="cmp-label" htmlFor="cmp-max-tokens">
                  Maximum output
                </label>
                <input
                  id="cmp-max-tokens"
                  className="cmp-number"
                  type="number"
                  min={meta.limits?.min_output_tokens || 32}
                  max={meta.limits?.max_output_tokens || 4096}
                  placeholder="per task"
                  value={maxTokens}
                  onChange={(event) => setMaxTokens(event.target.value)}
                />
                <span className="cmp-field-note">
                  Left empty, each question keeps the limit it was written with.
                </span>
              </div>
              <div className="cmp-field">
                <label className="cmp-label" htmlFor="cmp-temperature">
                  Temperature
                </label>
                <input
                  id="cmp-temperature"
                  className="cmp-number"
                  type="number"
                  min={0}
                  max={meta.limits?.max_temperature || 2}
                  step={0.1}
                  value={temperature}
                  onChange={(event) => setTemperature(event.target.value)}
                />
                <span className="cmp-field-note">
                  Zero keeps answers steady, so two runs are comparable.
                </span>
              </div>
            </div>
          </details>

          <section className="cmp-block cmp-actions">
            {running ? (
              <>
                <button type="button" className="neo-button secondary" onClick={stop}>
                  Stop
                </button>
                <span className="cmp-progress">
                  {progress.total
                    ? progressLabel(progress.completed, progress.total)
                    : "Starting the models up…"}
                </span>
              </>
            ) : (
              <>
                <button
                  type="button"
                  className="neo-button primary cmp-run"
                  onClick={start}
                  disabled={Boolean(missing)}
                >
                  {phase === "done" ? "Run it again" : "Run comparison"}
                </button>
                <span className="cmp-progress">
                  {missing || (estimate ? `Usually under ${estimate} seconds.` : "")}
                </span>
              </>
            )}
          </section>

          {notice ? <p className="cmp-notice">{notice}</p> : null}

          {running && progress.total ? (
            <div
              className="cmp-bar"
              role="progressbar"
              aria-valuenow={progress.completed}
              aria-valuemin={0}
              aria-valuemax={progress.total}
            >
              <div
                className="cmp-bar-fill"
                style={{ width: `${(progress.completed / progress.total) * 100}%` }}
              />
            </div>
          ) : null}

          {summaries.length ? (
            <section className="cmp-card">
              <p className="cmp-tradeoff">{describeTradeoff(summaries)}</p>
              <Scoreboard summaries={summaries} judgeOn={Boolean(comparison?.judged_by)} />
              {comparison?.judged_by ? (
                <p className="cmp-quiet">
                  Ratings out of ten came from {comparison.judged_by}. They are that
                  model's opinion, kept separate from the rule-based checks.
                </p>
              ) : null}
              {comparison?.cancelled ? (
                <p className="cmp-quiet">You stopped this one early.</p>
              ) : null}
              {(comparison?.errors || []).map((item) => (
                <p key={item} className="cmp-warn">{item}</p>
              ))}
            </section>
          ) : null}

          <Grid
            rows={rows}
            contenders={contenders}
            openKey={openKey}
            onOpen={(cell) => setOpenKey(openKey === cell.key ? "" : cell.key)}
          />

          <CellDetail
            cell={openCell}
            task={openTask}
            config={config}
            judgeOn={judgeOn}
            deterministic={deterministic && deterministicAvailable}
            onClose={() => setOpenKey("")}
          />
        </>
      )}
    </div>
  );
}

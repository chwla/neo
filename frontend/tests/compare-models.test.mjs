/**
 * The Compare Models screen.
 *
 * The suite renders to static markup and cannot click, so the parts worth asserting are
 * exported as plain functions.
 *
 * The one that matters most is `describeTradeoff`. It is the only piece of prose that
 * touches two models at once, which makes it the place a verdict would creep back in --
 * so most of the assertions below are about what it refuses to say.
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import CompareModels, {
  CELL_STATE,
  CHECK_MARK,
  buildGrid,
  ADD_PREFIX,
  describeDepth,
  describeLoadFailure,
  describeTradeoff,
  evaluationNote,
  progressLabel,
  saidAsRating,
  saidAsScore,
  saidAsTime,
  whatIsMissing,
} from "../src/CompareModels.jsx";

function model(id) {
  return { id, display_name: id, provider: "ollama", model: id, local: true, version: "" };
}

function task(id, label = id) {
  return {
    id, use_case: "coding", label, prompt: `prompt for ${id}`,
    max_tokens: 200, gradeable: true, rubric: "",
  };
}

function outcome(contenderId, taskId, extra = {}) {
  return {
    contender_id: contenderId,
    task_id: taskId,
    status: "ok",
    state: "complete",
    answer: "42",
    thinking: "",
    checks: [{ label: "Got it", status: "passed", detail: "", weight: 1 }],
    score: 1,
    evaluation: "deterministic",
    duration_ms: 1500,
    time_to_first_token_ms: 300,
    completion_tokens: 8,
    thinking_tokens: null,
    tokens_per_second: 5,
    judge_score: null,
    judge_note: "",
    message: "",
    ...extra,
  };
}

function summary(id, extra = {}) {
  return {
    contender: model(id),
    score: null,
    judge_score: null,
    tasks_answered: 3,
    tasks_attempted: 3,
    tasks_graded: 3,
    checks_passed: 3,
    checks_applied: 3,
    median_duration_ms: 1000,
    median_time_to_first_token_ms: 200,
    median_tokens_per_second: 20,
    total_duration_ms: 3000,
    total_completion_tokens: 60,
    warmup_ms: 0,
    was_warm: null,
    error: "",
    ...extra,
  };
}

describe("never naming a winner", () => {
  const higherButSlower = [
    summary("A", { score: 1, median_duration_ms: 12000 }),
    summary("B", { score: 0.96, median_duration_ms: 4000 }),
  ];

  test("it reports both measurements without picking between them", () => {
    const said = describeTradeoff(higherButSlower);

    assert.match(said, /A scored higher/);
    assert.match(said, /B answered fastest/);
  });

  test("it uses no word that crowns a model", () => {
    const said = describeTradeoff(higherButSlower).toLowerCase();

    for (const word of ["winner", "won", "best", "better model", "recommend", "should use"]) {
      assert.ok(!said.includes(word), `"${word}" appeared in: ${said}`);
    }
  });

  test("when the two measurements disagree it hands the decision back", () => {
    assert.match(describeTradeoff(higherButSlower), /depends on what you are using it for/);
  });

  test("a small difference is reported as a small difference, not as a tie", () => {
    /* There is deliberately no threshold below which a gap stops being reported. */
    const said = describeTradeoff([
      summary("A", { score: 0.78, median_duration_ms: 1000 }),
      summary("B", { score: 0.75, median_duration_ms: 900 }),
    ]);

    assert.match(said, /78%/);
    assert.match(said, /75%/);
  });

  test("an exact tie on quality is stated as a tie rather than broken by speed", () => {
    const said = describeTradeoff([
      summary("A", { score: 0.75, median_duration_ms: 5000 }),
      summary("B", { score: 0.75, median_duration_ms: 1000 }),
    ]);

    assert.match(said, /scored the same/);
    /* Speed is still reported -- as a fact, not as the thing that settles it. */
    assert.match(said, /B answered fastest/);
  });

  test("the judge's rating is reported separately from the checks", () => {
    const said = describeTradeoff([
      summary("A", { score: 1, judge_score: 0.9, median_duration_ms: 1000 }),
      summary("B", { score: 0.5, judge_score: 0.4, median_duration_ms: 900 }),
    ]);

    assert.match(said, /scored higher on the checks/);
    assert.match(said, /the judge rated A highest/);
  });

  test("with nothing scored it still says who was quicker and nothing more", () => {
    const said = describeTradeoff([
      summary("A", { median_duration_ms: 4000 }),
      summary("B", { median_duration_ms: 1000 }),
    ]);

    assert.match(said, /B answered fastest/);
    assert.ok(!said.toLowerCase().includes("scored"));
  });

  test("one survivor is not a comparison", () => {
    const said = describeTradeoff([
      summary("A", { score: 1 }),
      summary("B", { tasks_answered: 0, error: "unreachable" }),
    ]);

    assert.match(said, /nothing to compare/);
  });

  test("nobody finishing is said plainly", () => {
    const said = describeTradeoff([
      summary("A", { tasks_answered: 0, error: "x" }),
      summary("B", { tasks_answered: 0, error: "y" }),
    ]);

    assert.match(said, /No model finished/);
  });
});

describe("keeping the two kinds of score apart", () => {
  test("a rule-based score reads as a percentage", () => {
    assert.equal(saidAsScore(0.889), "89%");
  });

  test("a judge rating reads out of ten, so the two can never be confused", () => {
    assert.equal(saidAsRating(0.87), "8.7/10");
  });

  test("an ungradeable answer shows a dash, not a zero", () => {
    /* "Could not be judged" and "judged and got it all wrong" are different findings. */
    assert.equal(saidAsScore(null), "–");
    assert.equal(saidAsRating(null), "–");
    assert.equal(saidAsScore(0), "0%");
  });

  test("a task with no deterministic grader says so instead of showing a number", () => {
    const said = evaluationNote(outcome("A", "t1", { score: null, evaluation: "none" }), {
      deterministic: true,
      judgeOn: false,
    });

    assert.match(said, /No deterministic evaluation available/);
    assert.match(said, /Enable the LLM judge/);
  });

  test("a graded task needs no explanation", () => {
    assert.equal(evaluationNote(outcome("A", "t1"), { deterministic: true }), "");
  });

  test("a judge that skipped one answer says which", () => {
    const said = evaluationNote(outcome("A", "t1", { score: null, evaluation: "none" }), {
      judgeOn: true,
    });

    assert.match(said, /judge did not rate this one/);
  });

  test("checks switched off is explained as a choice, not as a failure", () => {
    const said = evaluationNote(outcome("A", "t1", { score: null, evaluation: "none" }), {
      deterministic: false,
      judgeOn: false,
    });

    assert.match(said, /switched off/);
  });
});

describe("deciding whether a comparison can be run", () => {
  test("one model is not a comparison, and the button says how many more", () => {
    assert.match(whatIsMissing({ selected: ["a"], useCase: "coding" }), /Pick 1 more model\./);
  });

  test("two models with a built-in use case is runnable", () => {
    assert.equal(whatIsMissing({ selected: ["a", "b"], useCase: "coding" }), "");
  });

  test("too many models is refused with the actual limit", () => {
    const missing = whatIsMissing({
      selected: ["a", "b", "c", "d", "e"],
      useCase: "coding",
      limits: { min_models: 2, max_models: 4 },
    });

    assert.match(missing, /up to 4 models/);
  });

  test("a custom comparison needs at least one question", () => {
    assert.match(
      whatIsMissing({ selected: ["a", "b"], useCase: "custom", prompts: ["  ", ""] }),
      /at least one question/,
    );
  });

  test("one filled question among blanks is enough", () => {
    assert.equal(
      whatIsMissing({ selected: ["a", "b"], useCase: "custom", prompts: ["", "why?", ""] }),
      "",
    );
  });

  test("switching off every evaluation is refused before the run starts", () => {
    /* A grid of dashes is not a result. */
    assert.match(
      whatIsMissing({
        selected: ["a", "b"], useCase: "coding", deterministic: false, judgeOn: false,
      }),
      /Nothing would be evaluated/,
    );
  });

  test("a custom comparison with only the judge on is allowed", () => {
    assert.equal(
      whatIsMissing({
        selected: ["a", "b"], useCase: "custom", prompts: ["why?"],
        deterministic: false, judgeOn: true,
      }),
      "",
    );
  });

  test("an empty slot is named as the thing to fix", () => {
    /* A slot can be added before a model is chosen for it. */
    assert.match(
      whatIsMissing({ selected: ["a", "b", ""], useCase: "coding" }),
      /Choose a model for every slot/,
    );
  });

  test("the same model in two slots is refused with the reason", () => {
    assert.match(
      whatIsMissing({ selected: ["a", "a"], useCase: "coding" }),
      /cannot be compared with itself/,
    );
  });

  test("three and four models are both allowed", () => {
    /* The limit is two to four; three is a comparison, not a mistake. */
    assert.equal(whatIsMissing({ selected: ["a", "b", "c"], useCase: "coding" }), "");
    assert.equal(whatIsMissing({ selected: ["a", "b", "c", "d"], useCase: "coding" }), "");
  });

  test("a dropdown option that sets a model up is marked apart from one that picks it", () => {
    /* Both live in the same list, so the prefix is what keeps them from being confused. */
    assert.ok(`${ADD_PREFIX}gemma4:latest`.startsWith(ADD_PREFIX));
    assert.ok(!"ollama-gemma4".startsWith(ADD_PREFIX));
  });

  test("the reason is a sentence, so the button is never mysteriously disabled", () => {
    assert.ok(whatIsMissing({ selected: [], useCase: "coding" }).endsWith("."));
  });
});

describe("assembling the grid", () => {
  const contenders = [model("fast"), model("slow")];
  const tasks = [task("t1"), task("t2")];

  test("every task gets a row and every model a cell in it", () => {
    const rows = buildGrid({ tasks, contenders, outcomes: {} });

    assert.equal(rows.length, 2);
    assert.deepEqual(rows[0].cells.map((cell) => cell.contender.id), ["fast", "slow"]);
  });

  test("a cell nothing has happened to yet is queued, not blank", () => {
    const rows = buildGrid({ tasks, contenders, outcomes: {} });
    assert.equal(rows[0].cells[0].state, "queued");
  });

  test("a half-finished run draws what has arrived and leaves the rest pending", () => {
    /* Results stream in one at a time and out of order, so this is the normal case. */
    const rows = buildGrid({
      tasks,
      contenders,
      outcomes: { "slow::t2": outcome("slow", "t2") },
      states: { "fast::t1": "generating" },
    });

    assert.equal(rows[0].cells[0].outcome, null);
    assert.equal(rows[0].cells[0].state, "generating");
    assert.equal(rows[1].cells[1].outcome.contender_id, "slow");
  });

  test("a result lands under its own model and not its neighbour", () => {
    const rows = buildGrid({
      tasks,
      contenders,
      outcomes: {
        "fast::t1": outcome("fast", "t1", { score: 1 }),
        "slow::t1": outcome("slow", "t1", { score: 0 }),
      },
    });

    assert.equal(rows[0].cells[0].outcome.score, 1);
    assert.equal(rows[0].cells[1].outcome.score, 0);
  });

  test("a failed cell keeps its error where the result would have been", () => {
    const rows = buildGrid({
      tasks,
      contenders,
      outcomes: {
        "fast::t1": outcome("fast", "t1", {
          status: "failed", state: "error", answer: "", message: "This model did not answer.",
        }),
      },
    });

    assert.equal(rows[0].cells[0].outcome.message, "This model did not answer.");
  });

  test("the judge's one sentence is lifted to the row rather than repeated per column", () => {
    const rows = buildGrid({
      tasks,
      contenders,
      outcomes: {
        "fast::t1": outcome("fast", "t1", { judge_note: "B is clearer." }),
        "slow::t1": outcome("slow", "t1", { judge_note: "B is clearer." }),
      },
    });

    assert.equal(rows[0].judgeNote, "B is clearer.");
    assert.equal(rows[1].judgeNote, "");
  });

  test("every cell carries a key that identifies it for opening", () => {
    const rows = buildGrid({ tasks, contenders, outcomes: {} });
    assert.equal(rows[0].cells[0].key, "fast::t1");
  });

  test("nothing to draw yet is an empty grid rather than a crash", () => {
    assert.deepEqual(buildGrid({}), []);
  });
});

describe("saying what a number of questions actually gets you", () => {
  test("it says how many of the pool are being asked", () => {
    assert.match(describeDepth(3, 100), /3 of 100 questions/);
  });

  test("it says the questions are drawn at random", () => {
    /* A fixed prefix would ask the same handful every run; the sampling is the point. */
    assert.match(describeDepth(3, 100), /picked at random/);
  });

  test("it promises the draw is recorded, so a result can be checked", () => {
    assert.match(describeDepth(3, 100), /records which/);
  });

  test("asking for the whole set is described as the whole set, not a sample", () => {
    const said = describeDepth(100, 100);

    assert.match(said, /All 100 questions/);
    assert.ok(!said.includes("at random"));
  });

  test("a set of unknown size still says something", () => {
    assert.match(describeDepth(4, 0), /4 questions/);
  });
});

describe("no em dashes in anything the user reads", () => {
  test("the screen carries none", () => {
    const markup = renderToStaticMarkup(createElement(CompareModels, {}));
    assert.ok(!markup.includes("\u2014"));
  });

  test("nor do the sentences built for it", () => {
    const sentences = [
      describeDepth(20, 8),
      describeDepth(3, 8),
      describeLoadFailure("Not Found"),
      whatIsMissing({ selected: ["a"], useCase: "coding" }),
      whatIsMissing({ selected: ["a", "a"], useCase: "coding" }),
      describeTradeoff([
        summary("A", { score: 1, median_duration_ms: 12000 }),
        summary("B", { score: 0.96, median_duration_ms: 4000 }),
      ]),
    ];
    for (const said of sentences) {
      assert.ok(!said.includes("\u2014"), `em dash in: ${said}`);
    }
  });
});

describe("saying where the run has got to", () => {
  test("progress is counted in responses, not percentages", () => {
    assert.equal(progressLabel(7, 12), "7 / 12 responses complete");
  });

  test("every state a cell can be in has words for it", () => {
    for (const state of ["queued", "generating", "evaluating", "complete", "error", "cancelled"]) {
      assert.ok(CELL_STATE[state], `no label for ${state}`);
    }
  });

  test("a skipped check is marked apart from a failed one", () => {
    assert.notEqual(CHECK_MARK.skipped, CHECK_MARK.failed);
    assert.notEqual(CHECK_MARK.passed, CHECK_MARK.failed);
  });

  test("a fast answer is reported in a unit that suits it", () => {
    assert.equal(saidAsTime(400), "400ms");
    assert.equal(saidAsTime(1500), "1.5s");
    assert.equal(saidAsTime(23400), "23s");
  });

  test("a latency that was never recorded is empty rather than zero", () => {
    assert.equal(saidAsTime(null), "");
  });
});

describe("the screen on arrival", () => {
  test("it no longer carries a back link, since the sidebar is always there", () => {
    const markup = renderToStaticMarkup(createElement(CompareModels, {}));
    assert.ok(!markup.includes("ws-back"));
  });

  test("a stale backend is explained as a restart, not as a bare Not Found", () => {
    /* The API answers 404 when this screen is newer than the server behind it. */
    assert.match(
      describeLoadFailure("Not Found"),
      /Restart the Neo server/,
    );
  });

  test("any other failure is passed through as it came", () => {
    assert.equal(describeLoadFailure("Backend API is not reachable."),
      "Backend API is not reachable.");
  });

  test("it explains itself before anything has loaded", () => {
    const markup = renderToStaticMarkup(createElement(CompareModels, {}));

    assert.match(markup, /Compare models/);
    /* The first paint happens before the models are known, so it must not look broken. */
    assert.match(markup, /Looking at which models are set up/);
  });

  test("it frames the feature as comparing your own work, not ranking intelligence", () => {
    const markup = renderToStaticMarkup(createElement(CompareModels, {}));

    assert.match(markup, /tasks you actually care about/);
    assert.ok(!/smartest|which model is best|winner/i.test(markup));
  });

  test("it promises that local models keep the prompt on this computer", () => {
    const markup = renderToStaticMarkup(createElement(CompareModels, {}));
    assert.match(markup, /nothing you type leaves it/i);
  });
});

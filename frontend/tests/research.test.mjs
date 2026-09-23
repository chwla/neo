/**
 * The Research screen's judgements about a run.
 *
 * The suite renders to static markup and cannot click, so what matters is exported as plain
 * functions: how a run's ending is named, what it is doing now, and the one line about where
 * the answer stands. That line is counted from the same target states the report is written
 * from, so the two can never disagree -- which is most of what these assertions are for.
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";

import { completionOf, confidenceReasonsOf, minutes, phaseOf, plannedChild, standingOf } from "../src/Research.jsx";

function run(overrides = {}) {
  return {
    status: "completed",
    metadata: { completion_status: "completed", metrics: {} },
    plan: { targets: [{ id: "q1" }, { id: "q1.e1", parent_id: "q1" }] },
    progress_log: [],
    ...overrides,
  };
}

function state(id, sufficiency, basis = "decisive", tier = "core") {
  return { target_id: id, sufficiency, basis, tier };
}

describe("how a run's ending is named", () => {
  test("each ending has one name, and an open question is not a failure", () => {
    assert.equal(completionOf(run())[0], "Complete");
    const gaps = run({ metadata: { completion_status: "completed_with_gaps" } });
    assert.deepEqual(completionOf(gaps), ["Complete, with open questions", "warn"]);
    const ceiling = run({ metadata: { completion_status: "ceiling_reached" } });
    assert.equal(completionOf(ceiling)[0], "Stopped at this depth's limit");
    const blocked = run({ metadata: { completion_status: "blocked" } });
    assert.equal(completionOf(blocked)[0], "Search unavailable");
    assert.equal(completionOf(run({ metadata: { completion_status: "failed" } }))[1], "bad");
  });

  test("a run recorded before completion statuses existed is still named", () => {
    const old = run({ metadata: { stop_reason: "budget_exhausted" } });
    assert.equal(completionOf(old)[0], "Stopped at this depth's limit");
    const cancelled = run({ status: "cancelled", metadata: {} });
    assert.equal(completionOf(cancelled)[0], "Cancelled");
    assert.equal(completionOf(run({ status: "running", metadata: {} })), null);
  });
});

describe("what a live run is doing", () => {
  test("the phase is what a reader would call it, not the engine's stage name", () => {
    const live = run({
      current_step: "Weighing evidence",
      progress_log: [{ phase: "finding_primary_evidence", core_established: 1, core_targets: 3 }],
    });
    const [label, latest] = phaseOf(live);
    assert.equal(label, "Looking for original sources");
    assert.equal(latest.core_targets, 3);
  });

  test("a run with no phase yet falls back to its step, then to Queued", () => {
    assert.equal(phaseOf(run({ current_step: "Planning" }))[0], "Planning");
    assert.equal(phaseOf(run({ current_step: "" }))[0], "Queued");
  });

  test("time is shown as the mode's allowance, not as a percentage", () => {
    assert.equal(minutes(0), "under a minute");
    assert.equal(minutes(29), "under a minute");
    assert.equal(minutes(600), "10 min");
  });
});

describe("where the answer stands", () => {
  test("core questions are counted as the plan asked them, not per subject", () => {
    const states = [
      state("q1", "established"),
      // The per-subject parts of q1 are how it was researched, not questions of their own.
      state("q1.e1", "established"),
      state("q2", "insufficient", "none"),
    ];
    const parts = standingOf(run(), { sources_used: 4, independent_domains: 3 }, states);
    assert.equal(parts[0], "1 of 2 core questions established");
    assert.equal(parts[1], "1 by the original record, 0 by independent sources");
    assert.equal(parts[2], "4 sources cited (3 independent)");
  });

  test("it says what the answer rests on, and what is still open", () => {
    const states = [state("q1", "established", "convergent"), state("q2", "established")];
    const parts = standingOf(run(), { sources_used: 5, independent_domains: 5, open_gaps: 1 }, states);
    assert.equal(parts[1], "1 by the original record, 1 by independent sources");
    assert.equal(parts.at(-1), "1 open question");
  });

  test("a run with nothing established says so rather than nothing at all", () => {
    const parts = standingOf(run(), {}, [state("q1", "insufficient", "none")]);
    assert.deepEqual(parts, ["0 of 1 core question established", "1 open question"]);
  });

  test("a subject's share of a question is recognised from the plan", () => {
    assert.equal(plannedChild(run().plan, "q1.e1"), true);
    assert.equal(plannedChild(run().plan, "q1"), false);
    assert.equal(plannedChild(null, "q1"), false);
  });

  test("a conflict in a subject's evidence leaves the core answer open", () => {
    const states = [state("q1", "established"), {
      ...state("q1.e1", "established"),
      needs: [{ kind: "conflict_material", material: true }],
    }];
    const parts = standingOf(run(), { open_gaps: 0 }, states);
    assert.equal(parts[0], "0 of 1 core question established");
    assert.equal(parts.at(-1), "1 open question");
    assert.deepEqual(confidenceReasonsOf(run({ metadata: { confidence_reasons: [
      "1 of 1 core questions are established.", "Sources conflict on the answer.",
    ] } })), ["Sources conflict on the answer."]);
  });
});

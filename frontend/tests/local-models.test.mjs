/**
 * The Local Models screen.
 *
 * The property that matters most is the order of events on a first visit: the question
 * has to be on screen before any hardware result exists. Putting the scan first would
 * leave someone watching a spinner before they had been asked anything, which is the
 * one thing this screen is meant not to do.
 *
 * The grouping logic is tested as a plain function because the suite renders to static
 * markup and cannot click. That is also why the component exports it.
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import LocalModels, {
  TIER_LABEL,
  groupByHowTheyRun,
  shouldAskFirst,
} from "../src/LocalModels.jsx";

function item(id, tier, overrides = {}) {
  return {
    model: { id, display_name: id.split("/")[1] || id, license: "Apache 2.0" },
    fit: { tier, reason: `${id} verdict`, required_gb: 4, quantization: "Q4_K_M" },
    score: 50,
    plain: {
      fit: `${id} fits`,
      speed: "Replies appear faster than you can read them.",
      download: "4 GB download, about 11 minutes on a typical connection.",
      compression: "Full quality, as the makers released it.",
      short: "Very fast · 4 GB download",
    },
    install_options: [{ kind: "one_click" }, { kind: "manual" }],
    installed: false,
    ...overrides,
  };
}

describe("grouping models by how they run", () => {
  test("the best fitting model is singled out as the top pick", () => {
    const grouped = groupByHowTheyRun([
      item("org/Best", "comfortable"),
      item("org/Second", "comfortable"),
    ]);

    assert.equal(grouped.top.model.id, "org/Best");
  });

  test("the top pick is not repeated in the list below it", () => {
    const grouped = groupByHowTheyRun([
      item("org/Best", "comfortable"),
      item("org/Second", "comfortable"),
    ]);

    const listed = grouped.groups.flatMap((group) => group.items.map((i) => i.model.id));
    assert.deepEqual(listed, ["org/Second"]);
  });

  test("models are grouped by verdict, best first", () => {
    const grouped = groupByHowTheyRun([
      item("org/A", "comfortable"),
      item("org/B", "good"),
      item("org/C", "tight"),
      item("org/D", "good"),
    ]);

    assert.deepEqual(
      grouped.groups.map((group) => group.tier),
      ["good", "tight"],
    );
  });

  test("models that do not fit are separated out rather than dropped", () => {
    const grouped = groupByHowTheyRun([item("org/A", "comfortable"), item("org/Huge", "too_big")]);

    assert.equal(grouped.tooBig.length, 1);
    assert.equal(grouped.tooBig[0].model.id, "org/Huge");
    assert.ok(!grouped.groups.some((group) => group.tier === "too_big"));
  });

  test("an empty group is not shown at all", () => {
    const grouped = groupByHowTheyRun([item("org/A", "comfortable")]);

    assert.deepEqual(grouped.groups, []);
  });

  test("nothing fitting leaves no top pick rather than throwing", () => {
    const grouped = groupByHowTheyRun([item("org/Huge", "too_big")]);

    assert.equal(grouped.top, null);
  });

  test("no results at all is handled", () => {
    const grouped = groupByHowTheyRun([]);

    assert.equal(grouped.top, null);
    assert.deepEqual(grouped.groups, []);
    assert.deepEqual(grouped.tooBig, []);
  });
});

describe("verdict wording", () => {
  test("every verdict has a plain-language label", () => {
    for (const tier of ["comfortable", "good", "tight", "too_big"]) {
      assert.ok(TIER_LABEL[tier], `${tier} has no label`);
    }
  });

  test("no label uses the vocabulary the feature exists to hide", () => {
    const banned = ["vram", "quantiz", "gguf", "tok/s", "parameters"];
    for (const label of Object.values(TIER_LABEL)) {
      for (const term of banned) {
        assert.ok(!label.toLowerCase().includes(term), `${term} leaked into "${label}"`);
      }
    }
  });
});

describe("the first visit asks before it scans", () => {
  test("someone who has never chosen a goal is asked", () => {
    assert.equal(shouldAskFirst(""), true);
    assert.equal(shouldAskFirst(null), true);
  });

  test("someone who has chosen before goes straight to the results", () => {
    assert.equal(shouldAskFirst("coding"), false);
  });

  test("the question renders before any hardware result exists", () => {
    // No scan has returned, and nothing has been fetched. The question must still be
    // the thing on screen.
    const markup = renderToStaticMarkup(createElement(LocalModels, {}));

    assert.ok(
      markup.includes("What do you want to use it for?"),
      "the goal question should be on screen immediately",
    );
    assert.ok(
      markup.includes("looking at your computer"),
      "the scan should be described as running in the background",
    );
  });

  test("the first screen never blocks on a spinner", () => {
    const markup = renderToStaticMarkup(createElement(LocalModels, {}));

    assert.ok(!markup.includes("Working out what runs best"));
  });

  test("it no longer carries a back link, since the sidebar is always there", () => {
    const markup = renderToStaticMarkup(createElement(LocalModels, {}));

    assert.ok(!markup.includes("ws-back"));
  });
});

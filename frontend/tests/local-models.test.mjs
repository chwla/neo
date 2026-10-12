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
  PREVIEW_COUNT,
  parseRememberedGoals,
  previewGroups,
  shouldAskFirst,
  specLine,
  specsOf,
  toggleGoal,
} from "../src/LocalModels.jsx";
import { goalQuery } from "../src/api.js";

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

describe("the specifications beside the verdict", () => {
  const apple = {
    cpu_name: "Apple M5",
    gpu_name: "Apple M5",
    unified_memory: true,
    total_memory_gb: 32,
    usable_memory_gb: 25,
  };

  test("a shared-memory machine is not told its chip twice", () => {
    const labels = specsOf(apple).map((spec) => spec.label);

    assert.deepEqual(labels, ["Processor", "Memory", "Available for AI"]);
  });

  test("a discrete card is named, since the verdict above does not name it", () => {
    const specs = specsOf({
      cpu_name: "AMD Ryzen 9 7950X",
      gpu_name: "NVIDIA GeForce RTX 4090",
      unified_memory: false,
      total_memory_gb: 64,
      usable_memory_gb: 24,
    });

    assert.deepEqual(
      specs.find((spec) => spec.label === "Graphics"),
      { label: "Graphics", value: "NVIDIA GeForce RTX 4090" },
    );
  });

  test("nothing that was never established is printed", () => {
    // Neo in a container with the engine on a host it cannot reach: there is no
    // processor name and no memory figure, and "0 GB" beside an empty label reads as a
    // broken screen rather than as the unanswered question it is.
    const specs = specsOf({
      cpu_name: "",
      gpu_name: "",
      unified_memory: false,
      total_memory_gb: 0,
      usable_memory_gb: 0,
    });

    assert.deepEqual(specs, []);
  });

  test("a figure that is known is still shown when its neighbour is not", () => {
    const specs = specsOf({
      cpu_name: "",
      gpu_name: "NVIDIA GeForce RTX 4090",
      unified_memory: false,
      total_memory_gb: 0,
      usable_memory_gb: 24,
    });

    assert.deepEqual(specs.map((spec) => spec.label), ["Graphics", "Available for AI"]);
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

describe("several goals can be chosen at once", () => {
  test("a goal saved when only one could be chosen still reads back", () => {
    assert.deepEqual(parseRememberedGoals("coding"), ["coding"]);
  });

  test("several saved goals read back once each, in order", () => {
    assert.deepEqual(parseRememberedGoals("chat, coding,,chat"), ["chat", "coding"]);
    assert.deepEqual(parseRememberedGoals(null), []);
  });

  test("an empty choice still asks, a list of goals does not", () => {
    assert.equal(shouldAskFirst([]), true);
    assert.equal(shouldAskFirst(["chat", "images"]), false);
  });

  test("choosing a goal adds it and choosing it again removes it", () => {
    assert.deepEqual(toggleGoal(["chat"], "coding"), ["chat", "coding"]);
    assert.deepEqual(toggleGoal(["chat", "coding"], "chat"), ["coding"]);
  });

  test("the last goal cannot be removed, so there is always something to rank for", () => {
    const only = ["chat"];
    assert.equal(toggleGoal(only, "chat"), only);
  });

  test("every goal goes to the server as its own parameter", () => {
    assert.equal(goalQuery(["chat", "images"]), "goal=chat&goal=images");
    assert.equal(goalQuery("coding"), "goal=coding");
    assert.equal(goalQuery([]), "goal=chat");
  });

  test("the question says more than one may be chosen", () => {
    const markup = renderToStaticMarkup(createElement(LocalModels, {}));

    assert.ok(markup.includes("Choose as many as you like."));
  });
});

describe("the list starts short", () => {
  const groups = [
    { tier: "comfortable", label: "Runs comfortably", items: ["a", "b", "c", "d", "e"] },
    { tier: "good", label: "Runs well", items: ["f", "g", "h"] },
    { tier: "tight", label: "Runs, but only just", items: ["i"] },
  ];

  test("only the best few are shown until everything is asked for", () => {
    const shown = previewGroups(groups);
    const count = shown.reduce((total, group) => total + group.items.length, 0);

    assert.equal(count, PREVIEW_COUNT);
    assert.deepEqual(shown.map((group) => group.tier), ["comfortable", "good"]);
    assert.deepEqual(shown[1].items, ["f"]);
  });

  test("a group the cut empties is dropped, not shown with no rows", () => {
    const shown = previewGroups(groups, { count: 2 });

    assert.deepEqual(shown.map((group) => group.tier), ["comfortable"]);
  });

  test("asking for everything returns every group whole", () => {
    assert.equal(previewGroups(groups, { showAll: true }), groups);
  });
});

describe("the machine in one line", () => {
  test("each figure is worded to stand on its own", () => {
    const line = specLine({
      cpu_name: "Apple M5",
      gpu_name: "Apple M5",
      unified_memory: true,
      total_memory_gb: 32,
      usable_memory_gb: 25,
    });

    assert.equal(line, "Apple M5 · 32 GB memory · 25 GB usable for AI");
  });

  test("a figure that was never established is left out of the line", () => {
    assert.equal(
      specLine({ cpu_name: "", gpu_name: "", unified_memory: false, total_memory_gb: 0, usable_memory_gb: 0 }),
      "",
    );
  });
});


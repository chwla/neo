/**
 * The Voice settings screen: what it says when something is missing.
 *
 * This screen exists because the alternative was a greyed-out menu entry that never
 * explained itself, so the tests are mostly about the explanation being present and
 * being in the user's vocabulary rather than the implementation's.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import VoiceSettings from "../src/VoiceSettings.jsx";

function render(status) {
  return renderToStaticMarkup(
    createElement(VoiceSettings, { status, onStatusChange() {}, onClose() {} }),
  );
}

const READY = {
  available: true,
  reason: "ready",
  model: { id: "small", label: "Standard", installed: true, approx_mb: 484 },
};

describe("voice settings", () => {
  test("names the three things that have to work", () => {
    const html = render(READY);
    for (const label of ["Voice engine", "Transcription model", "Microphone"]) {
      assert.ok(html.includes(label), `missing status row: ${label}`);
    }
  });

  test("explains a missing engine and how to add it", () => {
    const html = render({ available: false, reason: "dependency_missing", model: {} });
    assert.ok(html.includes("Voice support is not installed"));
    assert.ok(html.includes("pip install"), "the one case a button cannot fix");
  });

  test("says the model is missing rather than showing an error code", () => {
    const html = render({
      available: false,
      reason: "model_not_downloaded",
      model: { id: "small", label: "Standard", installed: false, approx_mb: 484 },
    });
    assert.ok(html.includes("Not downloaded."));
    assert.ok(!html.includes("model_not_downloaded"), "no internal state names on screen");
  });

  test("uses the user's vocabulary, not the library's", () => {
    const html = render(READY);
    for (const leak of ["faster-whisper", "faster_whisper", "ctranslate2", "WhisperModel"]) {
      assert.ok(!html.toLowerCase().includes(leak.toLowerCase()), `leaked "${leak}"`);
    }
  });

  test("tells a ready user how to start", () => {
    const html = render(READY);
    assert.ok(html.includes("Dictate"));
  });

  test("a download in progress is announced to assistive technology", () => {
    const html = render({
      available: false,
      reason: "model_downloading",
      model: { id: "small", label: "Standard", installed: false, downloading: true, percent: 40 },
    });
    assert.ok(html.includes("Downloading…"));
  });
});

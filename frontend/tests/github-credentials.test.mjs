import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import GitHub from "../src/GitHub.jsx";
import { api } from "../src/api.js";

test("GitHub collects an explicit token in a password field", () => {
  const html = renderToStaticMarkup(createElement(GitHub, { onClose() {} }));
  assert.match(html, /Access token<input[^>]*type="password"/);
  assert.match(html, /encrypted for this profile/);
  assert.doesNotMatch(html, /Token environment reference|GITHUB_TOKEN/);
});

test("token replacement uses a CSRF-protected body on the selected connection", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const sent = [];
  try {
    globalThis.document = { cookie: "neo_csrf=synthetic-csrf" };
    globalThis.fetch = async (url, options) => {
      sent.push({ url, options });
      return { status: 204 };
    };
    await api.updateGithubConnection("connection/one", { token: "synthetic-token" });
    assert.equal(sent[0].url, "/api/github/connections/connection%2Fone");
    assert.equal(sent[0].options.method, "PATCH");
    assert.equal(sent[0].options.headers["X-Neo-Csrf"], "synthetic-csrf");
    assert.deepEqual(JSON.parse(sent[0].options.body), { token: "synthetic-token" });
    assert.ok(!sent[0].url.includes("synthetic-token"));
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
});

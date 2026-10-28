import assert from "node:assert/strict";
import { after, test } from "node:test";

import { csrfHeaders } from "../src/api.js";

const originalDocument = globalThis.document;
after(() => {
  globalThis.document = originalDocument;
});

test("browser mutations carry the session CSRF cookie as a header", () => {
  globalThis.document = { cookie: "theme=dark; neo_csrf=synthetic-token; other=value" };
  assert.deepEqual(csrfHeaders("POST"), { "X-Neo-Csrf": "synthetic-token" });
  assert.deepEqual(csrfHeaders("DELETE"), { "X-Neo-Csrf": "synthetic-token" });
  assert.deepEqual(csrfHeaders("GET"), {});
});

test("a missing CSRF cookie cannot invent an authorization header", () => {
  globalThis.document = { cookie: "theme=dark" };
  assert.deepEqual(csrfHeaders("PATCH"), {});
});

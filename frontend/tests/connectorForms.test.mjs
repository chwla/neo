import assert from "node:assert/strict";
import test from "node:test";

import {
  buildConnectorRequest,
  buildCredentialRequest,
  parseEnvironmentReferences,
  parseParameterLocations,
} from "../src/connectorForms.js";

test("manual REST reads are automatic and parameters get a bounded schema", () => {
  const request = buildConnectorRequest("manual_rest", {
    name: "Weather",
    baseUrl: "https://api.example.com",
    operationName: "current_weather",
    displayName: "Current weather",
    description: "Read the latest conditions.",
    method: "GET",
    path: "/weather/{city}",
    parametersText: "city: path\nunits: query",
    trustedLocalhost: false,
  });

  assert.equal(request.apiMethod, "createRestConnector");
  assert.equal(request.payload.read_only, true);
  assert.deepEqual(request.payload.parameter_locations, {
    city: "path",
    units: "query",
  });
  assert.deepEqual(Object.keys(request.payload.input_schema.properties), ["city", "units"]);
  assert.equal(request.payload.input_schema.additionalProperties, false);
});

test("manual REST writes cannot be marked read-only", () => {
  const request = buildConnectorRequest("manual_rest", {
    name: "CRM",
    baseUrl: "https://api.example.com",
    operationName: "update_customer",
    method: "PATCH",
    path: "/customers/{id}",
    parametersText: "id: path\nname: body",
  });

  assert.equal(request.payload.read_only, false);
});

test("stdio MCP uses argv and environment references without secret values", () => {
  const request = buildConnectorRequest("mcp_stdio", {
    name: "Local MCP",
    executable: "/opt/tools/company-mcp",
    argumentsText: "--mode\nread-only",
    environmentText: "SERVICE_TOKEN=NEO_SERVICE_TOKEN",
    trustedStdio: true,
  });

  assert.deepEqual(request.payload.command_json, [
    "/opt/tools/company-mcp",
    "--mode",
    "read-only",
  ]);
  assert.deepEqual(request.payload.env_json, {
    SERVICE_TOKEN: "NEO_SERVICE_TOKEN",
  });
  assert.equal(request.payload.metadata.trusted_stdio, true);
});

test("stdio MCP requires an explicit trust confirmation", () => {
  assert.throws(
    () => buildConnectorRequest("mcp_stdio", {
      name: "Untrusted",
      executable: "node",
      trustedStdio: false,
    }),
    /trust this local MCP process/i,
  );
});

test("legacy SSE is represented explicitly and not confused with Streamable HTTP", () => {
  const request = buildConnectorRequest("mcp_sse", {
    name: "Legacy",
    endpointUrl: "https://mcp.example.com/sse",
    trustedLocalhost: false,
  });

  assert.equal(request.payload.server_type, "http");
  assert.equal(request.payload.metadata.transport, "legacy_sse");
});

test("credential payload contains the selected secret but no unrelated secret fields", () => {
  const header = buildCredentialRequest({
    authType: "api_key_header",
    label: "Production",
    secret: "sensitive-value",
    headerName: "X-Service-Key",
  });
  assert.deepEqual(header, {
    auth_type: "api_key_header",
    label: "Production",
    secret: "sensitive-value",
    header_name: "X-Service-Key",
  });

  const none = buildCredentialRequest({
    authType: "none",
    label: "",
    secret: "must-not-leak",
    clientSecret: "must-not-leak",
  });
  assert.deepEqual(none, { auth_type: "none", label: null });
});

test("OAuth scopes normalize from commas, spaces, and lines", () => {
  const payload = buildCredentialRequest({
    authType: "oauth2",
    label: "",
    clientId: "neo-client",
    clientSecret: "",
    authorizationUrl: "https://id.example.com/authorize",
    tokenUrl: "https://id.example.com/token",
    revocationUrl: "",
    redirectUri: "http://127.0.0.1:8000/api/tools/servers/example/oauth/callback",
    scopesText: "profile,read:records\nopenid",
  });

  assert.deepEqual(payload.scopes, ["profile", "read:records", "openid"]);
  assert.equal(payload.client_secret, null);
  assert.equal(payload.revocation_url, null);
});

test("parameter and environment syntax rejects ambiguous input", () => {
  assert.throws(() => parseParameterLocations("city=query"), /must use name/i);
  assert.throws(() => parseEnvironmentReferences("TOKEN=plain-secret-value!"), /invalid variable/i);
});

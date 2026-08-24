import { api } from "./api.js";
import { buildConnectorRequest } from "./connectorForms.js";

export const CONNECTOR_CHOICES = [
  {
    id: "openapi_url",
    title: "OpenAPI URL",
    description: "Import every supported operation from an OpenAPI 3.x document.",
  },
  {
    id: "openapi_file",
    title: "OpenAPI file",
    description: "Upload a local JSON or YAML API description (maximum 2 MiB).",
  },
  {
    id: "manual_rest",
    title: "REST endpoint",
    description: "Add one endpoint without writing an OpenAPI document.",
  },
  {
    id: "mcp_http",
    title: "MCP over HTTP",
    description: "Connect a Streamable HTTP MCP server and discover its tools.",
  },
  {
    id: "mcp_sse",
    title: "Legacy MCP SSE",
    description: "Connect an older GET event-stream MCP server with a same-origin message endpoint.",
  },
  {
    id: "mcp_stdio",
    title: "Local MCP process",
    description: "Run an explicitly trusted stdio MCP server using an argv list.",
  },
];

/**
 * Submits the wizard's current form to the connector API and returns
 * `{server, definitions}`. Throws on failure. Purely the API call -- any
 * local UI state (busy flags, form resets, notices) is the caller's job, so
 * this can be reused from both the Settings admin page and a chat's Tools
 * panel without either owning the other's state shape.
 */
export async function submitConnectorWizard(kind, form, file) {
  if (kind === "openapi_file") {
    if (!file) throw new Error("Choose an OpenAPI JSON or YAML file.");
    return api.importOpenApiFile({
      name: form.name.trim(),
      file,
      allowTrustedLocalhost: form.trustedLocalhost,
    });
  }
  const request = buildConnectorRequest(kind, form);
  let result = await api[request.apiMethod](request.payload);
  if (request.discoverAfterCreate) {
    const discovered = await api.discoverToolServer(result.server.id);
    result = { ...result, definitions: discovered.definitions };
  }
  return result;
}

export function ConnectorWizard({
  busy,
  form,
  kind,
  file,
  onCancel,
  onFile,
  onForm,
  onKind,
  onSubmit,
}) {
  const update = (name, value) => onForm({ ...form, [name]: value });

  return (
    <form className="connector-wizard" onSubmit={onSubmit}>
      <div className="connector-section-heading">
        <div>
          <p className="connector-eyebrow">Guided setup</p>
          <h3>Add a connector</h3>
          <p>Choose a source. Neo validates the configuration and never returns stored secrets.</p>
        </div>
        <button type="button" className="connector-button secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>

      <fieldset className="connector-choice-grid">
        <legend>Connector type</legend>
        {CONNECTOR_CHOICES.map((choice) => (
          <label
            className={`connector-choice ${kind === choice.id ? "selected" : ""}`}
            key={choice.id}
          >
            <input
              type="radio"
              name="connector-kind"
              checked={kind === choice.id}
              onChange={() => onKind(choice.id)}
            />
            <strong>{choice.title}</strong>
            <span>{choice.description}</span>
          </label>
        ))}
      </fieldset>

      <div className="connector-form-grid">
        <label>
          Connector name
          <input
            value={form.name}
            onChange={(event) => update("name", event.target.value)}
            placeholder="e.g. Company knowledge"
            required
          />
        </label>

        {kind === "openapi_url" && (
          <label className="connector-field-wide">
            OpenAPI document URL
            <input
              type="url"
              value={form.openapiUrl}
              onChange={(event) => update("openapiUrl", event.target.value)}
              placeholder="https://api.example.com/openapi.json"
              required
            />
            <span className="connector-help">Public connectors must use HTTPS.</span>
          </label>
        )}

        {kind === "openapi_file" && (
          <label className="connector-field-wide">
            OpenAPI JSON or YAML file
            <input
              type="file"
              accept=".json,.yaml,.yml,application/json,application/yaml,text/yaml"
              onChange={(event) => onFile(event.target.files?.[0] || null)}
              required={!file}
            />
            <span className="connector-help">
              {file ? `${file.name} · ${Math.ceil(file.size / 1024)} KiB` : "No file selected."}
            </span>
          </label>
        )}

        {kind === "manual_rest" && (
          <>
            <label className="connector-field-wide">
              Base URL
              <input
                type="url"
                value={form.baseUrl}
                onChange={(event) => update("baseUrl", event.target.value)}
                placeholder="https://api.example.com"
                required
              />
            </label>
            <label>
              Operation name
              <input
                value={form.operationName}
                onChange={(event) => update("operationName", event.target.value)}
                placeholder="lookup_customer"
                required
              />
            </label>
            <label>
              Display name
              <input
                value={form.displayName}
                onChange={(event) => update("displayName", event.target.value)}
                placeholder="Look up customer"
              />
            </label>
            <label>
              HTTP method
              <select
                value={form.method}
                onChange={(event) => update("method", event.target.value)}
              >
                {["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"].map((method) => (
                  <option key={method} value={method}>{method}</option>
                ))}
              </select>
            </label>
            <label>
              Endpoint path
              <input
                value={form.path}
                onChange={(event) => update("path", event.target.value)}
                placeholder="/customers/{customer_id}"
                required
              />
            </label>
            <label className="connector-field-wide">
              What this endpoint does
              <textarea
                rows={2}
                value={form.description}
                onChange={(event) => update("description", event.target.value)}
                placeholder="Describe when Neo should use this endpoint."
              />
            </label>
            <label className="connector-field-wide">
              Parameters
              <textarea
                rows={4}
                value={form.parametersText}
                onChange={(event) => update("parametersText", event.target.value)}
                placeholder={"customer_id: path\ninclude_orders: query"}
              />
              <span className="connector-help">
                One per line: <code>name: path</code>, <code>query</code>, <code>header</code>,
                or <code>body</code>.
              </span>
            </label>
            {!["GET", "HEAD"].includes(form.method) && (
              <div className="connector-safety-note connector-field-wide">
                This is a write operation. Neo will create a pending approval and will not
                send the request until you approve that exact call.
              </div>
            )}
          </>
        )}

        {["mcp_http", "mcp_sse"].includes(kind) && (
          <label className="connector-field-wide">
            {kind === "mcp_sse" ? "Legacy SSE endpoint" : "Streamable HTTP endpoint"}
            <input
              type="url"
              value={form.endpointUrl}
              onChange={(event) => update("endpointUrl", event.target.value)}
              placeholder="https://mcp.example.com/mcp"
              required
            />
            <span className="connector-help">
              {kind === "mcp_sse"
                ? "Neo reads the server-advertised same-origin message endpoint before initializing."
                : "Neo performs MCP initialize, tools/list, and tools/call using the negotiated session."}
            </span>
          </label>
        )}

        {kind === "mcp_stdio" && (
          <>
            <label className="connector-field-wide">
              Executable
              <input
                value={form.executable}
                onChange={(event) => update("executable", event.target.value)}
                placeholder="/usr/local/bin/company-mcp"
                required
              />
            </label>
            <label>
              Arguments
              <textarea
                rows={4}
                value={form.argumentsText}
                onChange={(event) => update("argumentsText", event.target.value)}
                placeholder={"--mode\nread-only"}
              />
              <span className="connector-help">One argument per line. No shell is used.</span>
            </label>
            <label>
              Environment references
              <textarea
                rows={4}
                value={form.environmentText}
                onChange={(event) => update("environmentText", event.target.value)}
                placeholder="SERVICE_TOKEN=NEO_SERVICE_TOKEN"
              />
              <span className="connector-help">
                Maps a process variable to an existing environment variable. Never paste its value.
              </span>
            </label>
            <label className="connector-confirm connector-field-wide">
              <input
                type="checkbox"
                checked={form.trustedStdio}
                onChange={(event) => update("trustedStdio", event.target.checked)}
              />
              I trust this executable to run locally with the listed arguments and environment
              references.
            </label>
          </>
        )}

        {kind !== "mcp_stdio" && (
          <label className="connector-confirm connector-field-wide warning">
            <input
              type="checkbox"
              checked={form.trustedLocalhost}
              onChange={(event) => update("trustedLocalhost", event.target.checked)}
            />
            Allow this connector to reach a trusted loopback service on this machine.
          </label>
        )}
      </div>

      <div className="connector-form-actions">
        <button className="connector-button primary" type="submit" disabled={busy}>
          {busy ? "Connecting…" : "Connect and validate"}
        </button>
      </div>
    </form>
  );
}

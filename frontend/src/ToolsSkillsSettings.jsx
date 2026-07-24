import { useEffect, useMemo, useState } from "react";

import { api } from "./api.js";
import {
  CONNECTOR_FORM_EMPTY,
  CREDENTIAL_FORM_EMPTY,
  buildConnectorRequest,
  buildCredentialRequest,
  connectorKind,
  nonEmptyLines,
} from "./connectorForms.js";

const TOOL_EMPTY = {
  server_id: "",
  name: "",
  display_name: "",
  description: "",
  category: "external_read",
  input_schema_text: "{}",
  output_schema_text: "{}",
  permissions_text: "{}",
  enabled: true,
  metadata_text: "{}",
};

const SKILL_EMPTY = {
  name: "",
  display_name: "",
  description: "",
  skill_type: "instruction_bundle",
  instructions: "",
  tool_ids: [],
  agent_ids_text: "",
  rules_profile_ids_text: "",
  enabled: true,
  metadata_text: "{}",
};

const CONNECTOR_CHOICES = [
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

const AUTH_OPTIONS = [
  ["none", "No authentication"],
  ["api_key_header", "API key in a header"],
  ["api_key_query", "API key in the query string"],
  ["bearer", "Bearer token"],
  ["oauth2", "OAuth 2.0 with PKCE"],
];

function label(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function parseJson(text, fallback) {
  if (!String(text || "").trim()) return fallback;
  return JSON.parse(text);
}

function fromTool(tool) {
  return {
    ...TOOL_EMPTY,
    ...tool,
    server_id: tool.server_id || "",
    input_schema_text: JSON.stringify(tool.input_schema || {}, null, 2),
    output_schema_text: JSON.stringify(tool.output_schema || {}, null, 2),
    permissions_text: JSON.stringify(tool.permissions || {}, null, 2),
    metadata_text: JSON.stringify(tool.metadata || {}, null, 2),
  };
}

function fromSkill(skill) {
  return {
    ...SKILL_EMPTY,
    ...skill,
    agent_ids_text: (skill.agent_ids || []).join("\n"),
    rules_profile_ids_text: (skill.rules_profile_ids || []).join("\n"),
    metadata_text: JSON.stringify(skill.metadata || {}, null, 2),
  };
}

function StatusBadge({ tone = "neutral", children }) {
  return <span className={`connector-badge ${tone}`}>{children}</span>;
}

function ConnectorWizard({
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

function CredentialPanel({
  busy,
  credential,
  form,
  oauthResult,
  onDelete,
  onForm,
  onOAuthRefresh,
  onOAuthRevoke,
  onOAuthStart,
  onStatusRefresh,
  onSave,
  server,
}) {
  const update = (name, value) => onForm({ ...form, [name]: value });
  const authType = form.authType;

  return (
    <section className="connector-detail-section">
      <div className="connector-section-heading compact">
        <div>
          <h4>Authentication</h4>
          <p>Credentials are encrypted per profile. Secret values are write-only.</p>
        </div>
        {credential?.configured ? (
          <StatusBadge tone="success">{label(credential.auth_type)}</StatusBadge>
        ) : (
          <StatusBadge>No stored credential</StatusBadge>
        )}
      </div>

      {credential?.configured && (
        <div className="connector-credential-summary">
          <div>
            <span>Configuration</span>
            <strong>{credential.label || label(credential.auth_type)}</strong>
          </div>
          {credential.client_id && <div><span>Client ID</span><strong>{credential.client_id}</strong></div>}
          {credential.header_name && <div><span>Header</span><strong>{credential.header_name}</strong></div>}
          {credential.query_name && <div><span>Query parameter</span><strong>{credential.query_name}</strong></div>}
          {credential.expires_at && <div><span>Expires</span><strong>{new Date(credential.expires_at).toLocaleString()}</strong></div>}
        </div>
      )}

      {credential?.auth_type === "oauth2" && credential.configured && (
        <div className="connector-inline-actions">
          <button type="button" className="connector-button primary" onClick={onOAuthStart} disabled={busy}>
            Authorize
          </button>
          <button type="button" className="connector-button secondary" onClick={onStatusRefresh} disabled={busy}>
            Check authorization
          </button>
          <button type="button" className="connector-button secondary" onClick={onOAuthRefresh} disabled={busy}>
            Refresh token
          </button>
          <button type="button" className="connector-button danger" onClick={onOAuthRevoke} disabled={busy}>
            Revoke OAuth
          </button>
        </div>
      )}

      {oauthResult && (
        <div className="connector-safety-note" role="status">
          Authorization opened in a separate tab. Complete the provider flow, then return here
          and test the connector. The PKCE request expires at{" "}
          {new Date(oauthResult.expires_at).toLocaleTimeString()}.
          <a href={oauthResult.authorization_url} target="_blank" rel="noreferrer">
            Open authorization page
          </a>
        </div>
      )}

      <details className="connector-disclosure" open={!credential?.configured}>
        <summary>{credential?.configured ? "Replace authentication" : "Configure authentication"}</summary>
        <form className="connector-auth-form" onSubmit={onSave}>
          <label>
            Method
            <select value={authType} onChange={(event) => update("authType", event.target.value)}>
              {AUTH_OPTIONS.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
            </select>
          </label>
          <label>
            Label
            <input value={form.label} onChange={(event) => update("label", event.target.value)} placeholder="Production credential" />
          </label>

          {["api_key_header", "api_key_query", "bearer"].includes(authType) && (
            <label className="connector-field-wide">
              {authType === "bearer" ? "Bearer token" : "API key"}
              <input
                type="password"
                autoComplete="new-password"
                value={form.secret}
                onChange={(event) => update("secret", event.target.value)}
                required
              />
            </label>
          )}
          {authType === "api_key_header" && (
            <label>
              Header name
              <input value={form.headerName} onChange={(event) => update("headerName", event.target.value)} required />
            </label>
          )}
          {authType === "api_key_query" && (
            <label>
              Query parameter
              <input value={form.queryName} onChange={(event) => update("queryName", event.target.value)} required />
            </label>
          )}
          {authType === "oauth2" && (
            <>
              <label>
                Client ID
                <input value={form.clientId} onChange={(event) => update("clientId", event.target.value)} required />
              </label>
              <label>
                Client secret <span className="connector-help">(optional for public clients)</span>
                <input type="password" autoComplete="new-password" value={form.clientSecret} onChange={(event) => update("clientSecret", event.target.value)} />
              </label>
              <label className="connector-field-wide">
                Authorization URL
                <input type="url" value={form.authorizationUrl} onChange={(event) => update("authorizationUrl", event.target.value)} required />
              </label>
              <label className="connector-field-wide">
                Token URL
                <input type="url" value={form.tokenUrl} onChange={(event) => update("tokenUrl", event.target.value)} required />
              </label>
              <label className="connector-field-wide">
                Revocation URL <span className="connector-help">(optional)</span>
                <input type="url" value={form.revocationUrl} onChange={(event) => update("revocationUrl", event.target.value)} />
              </label>
              <label className="connector-field-wide">
                Exact redirect URI
                <input type="url" value={form.redirectUri} onChange={(event) => update("redirectUri", event.target.value)} required />
                <span className="connector-help">
                  Register this exact URI with the provider. OAuth state is bound to this profile session.
                </span>
              </label>
              <label className="connector-field-wide">
                Scopes
                <input value={form.scopesText} onChange={(event) => update("scopesText", event.target.value)} placeholder="read:records profile" />
              </label>
            </>
          )}
          <div className="connector-form-actions connector-field-wide">
            <button type="submit" className="connector-button primary" disabled={busy}>
              Save encrypted configuration
            </button>
            {credential?.configured && (
              <button type="button" className="connector-button danger" onClick={onDelete} disabled={busy}>
                Remove stored credentials
              </button>
            )}
          </div>
        </form>
      </details>
      {server.server_type === "stdio" && (
        <p className="connector-help">
          Stdio credentials should be supplied through environment references configured on the connector.
        </p>
      )}
    </section>
  );
}

export default function ToolsSkillsSettings({ onClose }) {
  const [activeTab, setActiveTab] = useState("connectors");
  const [servers, setServers] = useState([]);
  const [tools, setTools] = useState([]);
  const [skills, setSkills] = useState([]);
  const [calls, setCalls] = useState([]);
  const [selectedServerId, setSelectedServerId] = useState("");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [connectorKindChoice, setConnectorKindChoice] = useState("openapi_url");
  const [connectorForm, setConnectorForm] = useState({ ...CONNECTOR_FORM_EMPTY });
  const [connectorFile, setConnectorFile] = useState(null);
  const [credential, setCredential] = useState(null);
  const [credentialForm, setCredentialForm] = useState({ ...CREDENTIAL_FORM_EMPTY });
  const [healthByServer, setHealthByServer] = useState({});
  const [oauthResult, setOauthResult] = useState(null);
  const [toolForm, setToolForm] = useState({ ...TOOL_EMPTY });
  const [skillForm, setSkillForm] = useState({ ...SKILL_EMPTY });
  const [editingTool, setEditingTool] = useState(null);
  const [editingSkill, setEditingSkill] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);

  const selectedServer = useMemo(
    () => servers.find((server) => server.id === selectedServerId) || null,
    [servers, selectedServerId],
  );
  const selectedTools = useMemo(
    () => tools.filter((tool) => tool.server_id === selectedServerId),
    [tools, selectedServerId],
  );
  const pendingCalls = calls.filter((call) => call.approval_status === "pending");

  async function load() {
    const [serverData, toolData, skillData, callData] = await Promise.all([
      api.toolServers(true),
      api.toolDefinitions(true),
      api.toolSkills(true),
      api.toolCalls({ limit: 100 }),
    ]);
    const nextServers = serverData.servers || [];
    setServers(nextServers);
    setTools(toolData.definitions || []);
    setSkills(skillData.skills || []);
    setCalls(callData.calls || []);
    setSelectedServerId((current) => (
      nextServers.some((server) => server.id === current)
        ? current
        : (nextServers[0]?.id || "")
    ));
  }

  useEffect(() => {
    load().catch((error) => setNotice({ type: "error", text: error.message }));
  }, []);

  useEffect(() => {
    setOauthResult(null);
    setCredential(null);
    setCredentialForm({ ...CREDENTIAL_FORM_EMPTY });
    if (!selectedServerId) return undefined;
    let active = true;
    api.toolServerCredential(selectedServerId)
      .then((result) => {
        if (active) setCredential(result.credential);
      })
      .catch((error) => {
        if (active) setNotice({ type: "error", text: error.message });
      });
    return () => { active = false; };
  }, [selectedServerId]);

  async function execute(action, success, { reload = true } = {}) {
    setBusy(true);
    setNotice(null);
    try {
      const result = await action();
      if (reload) await load();
      setNotice({ type: "success", text: typeof success === "function" ? success(result) : success });
      return result;
    } catch (error) {
      setNotice({ type: "error", text: error.message });
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function refreshCredential(serverId = selectedServerId) {
    const result = await api.toolServerCredential(serverId);
    setCredential(result.credential);
    return result.credential;
  }

  async function createConnector(event) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      let result;
      if (connectorKindChoice === "openapi_file") {
        if (!connectorFile) throw new Error("Choose an OpenAPI JSON or YAML file.");
        result = await api.importOpenApiFile({
          name: connectorForm.name.trim(),
          file: connectorFile,
          allowTrustedLocalhost: connectorForm.trustedLocalhost,
        });
      } else {
        const request = buildConnectorRequest(connectorKindChoice, connectorForm);
        result = await api[request.apiMethod](request.payload);
        if (request.discoverAfterCreate) {
          const discovered = await api.discoverToolServer(result.server.id);
          result = { ...result, definitions: discovered.definitions };
        }
      }
      await load();
      setSelectedServerId(result.server.id);
      setConnectorForm({ ...CONNECTOR_FORM_EMPTY });
      setConnectorFile(null);
      setWizardOpen(false);
      setNotice({
        type: "success",
        text: `${result.server.name} connected. ${result.definitions?.length ?? 0} tool(s) are ready for review.`,
      });
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setBusy(false);
    }
  }

  async function testServer() {
    if (!selectedServer) return;
    const result = await execute(
      () => api.testToolServer(selectedServer.id),
      (value) => (
        value.health?.ok
          ? `Connection ready${Number.isInteger(value.health.tool_count) ? ` · ${value.health.tool_count} tool(s)` : ""}.`
          : `Connection test failed: ${value.health?.error || value.health?.status || "unknown error"}`
      ),
      { reload: false },
    );
    if (result?.health) {
      setHealthByServer((current) => ({ ...current, [selectedServer.id]: result.health }));
    }
  }

  async function discoverServer() {
    if (!selectedServer) return;
    await execute(
      () => api.discoverToolServer(selectedServer.id),
      (result) => `Discovered and saved ${result.definitions.length} tool(s).`,
    );
  }

  async function toggleServer() {
    if (!selectedServer) return;
    if (selectedServer.enabled) {
      await execute(
        () => api.disableToolServer(selectedServer.id),
        "Connector disabled. Neo will not select its tools.",
      );
    } else {
      await execute(
        () => api.updateToolServer(selectedServer.id, { enabled: true }),
        "Connector enabled.",
      );
    }
  }

  async function saveCredential(event) {
    event.preventDefault();
    if (!selectedServer) return;
    let payload;
    try {
      payload = buildCredentialRequest(credentialForm);
    } catch (error) {
      setNotice({ type: "error", text: error.message });
      return;
    }
    const result = await execute(
      () => api.setToolServerCredential(selectedServer.id, payload),
      "Authentication configuration encrypted and saved.",
      { reload: false },
    );
    if (result) {
      setCredential(result.credential);
      setCredentialForm({ ...CREDENTIAL_FORM_EMPTY });
    }
  }

  async function deleteCredential() {
    if (!selectedServer) return;
    const result = await execute(
      () => api.deleteToolServerCredential(selectedServer.id),
      "Stored credentials removed.",
      { reload: false },
    );
    if (result !== null) {
      await refreshCredential();
      setCredentialForm({ ...CREDENTIAL_FORM_EMPTY });
    }
  }

  async function oauthStart() {
    if (!selectedServer) return;
    const result = await execute(
      () => api.startToolServerOAuth(selectedServer.id),
      "OAuth authorization prepared with PKCE.",
      { reload: false },
    );
    if (result) {
      setOauthResult(result);
      window.open(result.authorization_url, "_blank", "noopener,noreferrer");
    }
  }

  async function oauthRefresh() {
    if (!selectedServer) return;
    const result = await execute(
      () => api.refreshToolServerOAuth(selectedServer.id),
      "OAuth token refreshed.",
      { reload: false },
    );
    if (result) await refreshCredential();
  }

  async function checkCredentialStatus() {
    if (!selectedServer) return;
    await execute(
      () => refreshCredential(selectedServer.id),
      (result) => (
        result.expires_at
          ? `Authorization active until ${new Date(result.expires_at).toLocaleString()}.`
          : "Authentication status refreshed."
      ),
      { reload: false },
    );
  }

  async function oauthRevoke() {
    if (!selectedServer) return;
    const result = await execute(
      () => api.revokeToolServerOAuth(selectedServer.id),
      "OAuth grant revoked and local tokens removed.",
      { reload: false },
    );
    if (result) await refreshCredential();
  }

  async function toggleTool(tool) {
    await execute(
      () => api.updateToolDefinition(tool.id, { enabled: !tool.enabled }),
      `${tool.display_name || tool.name} ${tool.enabled ? "disabled" : "enabled"}.`,
    );
  }

  async function saveTool(event) {
    event.preventDefault();
    let payload;
    try {
      payload = {
        server_id: toolForm.server_id || null,
        name: toolForm.name,
        display_name: toolForm.display_name || null,
        description: toolForm.description || null,
        category: toolForm.category,
        input_schema: parseJson(toolForm.input_schema_text, {}),
        output_schema: parseJson(toolForm.output_schema_text, {}),
        permissions: parseJson(toolForm.permissions_text, {}),
        enabled: toolForm.enabled,
        metadata: parseJson(toolForm.metadata_text, {}),
      };
    } catch (error) {
      setNotice({ type: "error", text: `Tool JSON is invalid: ${error.message}` });
      return;
    }
    const result = await execute(
      () => (
        editingTool
          ? api.updateToolDefinition(editingTool.id, payload)
          : api.createToolDefinition(payload)
      ),
      "Tool definition saved.",
    );
    if (result) {
      setEditingTool(null);
      setToolForm({ ...TOOL_EMPTY });
    }
  }

  async function saveSkill(event) {
    event.preventDefault();
    let payload;
    try {
      payload = {
        name: skillForm.name,
        display_name: skillForm.display_name || null,
        description: skillForm.description || null,
        skill_type: skillForm.skill_type,
        instructions: skillForm.instructions,
        tool_ids: skillForm.tool_ids || [],
        agent_ids: nonEmptyLines(skillForm.agent_ids_text),
        rules_profile_ids: nonEmptyLines(skillForm.rules_profile_ids_text),
        enabled: skillForm.enabled,
        metadata: parseJson(skillForm.metadata_text, {}),
      };
    } catch (error) {
      setNotice({ type: "error", text: `Skill metadata is invalid: ${error.message}` });
      return;
    }
    const result = await execute(
      () => (
        editingSkill
          ? api.updateToolSkill(editingSkill.id, payload)
          : api.createToolSkill(payload)
      ),
      "Skill saved.",
    );
    if (result) {
      setEditingSkill(null);
      setSkillForm({ ...SKILL_EMPTY });
    }
  }

  async function decideCall(call, approved) {
    await execute(
      () => (
        approved
          ? api.approveToolCall(call.id)
          : api.rejectToolCall(call.id, "Rejected in connector settings.")
      ),
      approved ? "Approved call executed." : "Call rejected without execution.",
    );
  }

  const selectedHealth = selectedServer ? healthByServer[selectedServer.id] : null;

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="settings-dialog tools-settings-dialog connector-settings"
        role="dialog"
        aria-modal="true"
        aria-label="Connectors, tools, and skills"
      >
        <header className="modal-header connector-modal-header">
          <div>
            <p className="connector-eyebrow">Neo integrations</p>
            <h2>Connectors, tools &amp; skills</h2>
            <p>Connect trusted services, inspect permissions, and approve every external write.</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close connector settings">×</button>
        </header>

        <nav className="connector-tabs" aria-label="Integration settings">
          {[
            ["connectors", "Connectors", servers.length],
            ["skills", "Skills", skills.length],
            ["approvals", "Approvals", pendingCalls.length],
          ].map(([id, text, count]) => (
            <button
              type="button"
              role="tab"
              aria-selected={activeTab === id}
              className={activeTab === id ? "active" : ""}
              key={id}
              onClick={() => setActiveTab(id)}
            >
              {text}<span>{count}</span>
            </button>
          ))}
        </nav>

        {notice && (
          <div
            className={`connector-notice ${notice.type}`}
            role={notice.type === "error" ? "alert" : "status"}
          >
            {notice.text}
          </div>
        )}

        {activeTab === "connectors" && (
          <div className="connector-tab-content">
            {wizardOpen ? (
              <ConnectorWizard
                busy={busy}
                form={connectorForm}
                kind={connectorKindChoice}
                file={connectorFile}
                onCancel={() => setWizardOpen(false)}
                onFile={setConnectorFile}
                onForm={setConnectorForm}
                onKind={setConnectorKindChoice}
                onSubmit={createConnector}
              />
            ) : (
              <div className="connector-toolbar">
                <div>
                  <strong>{servers.length} connector{servers.length === 1 ? "" : "s"}</strong>
                  <span>Read-only tools may run automatically. Writes always require approval.</span>
                </div>
                <button type="button" className="connector-button primary" onClick={() => setWizardOpen(true)}>
                  + Add connector
                </button>
              </div>
            )}

            <div className="connector-layout">
              <aside className="connector-list" aria-label="Configured connectors">
                {servers.length ? servers.map((server) => {
                  const serverTools = tools.filter((tool) => tool.server_id === server.id);
                  const health = healthByServer[server.id];
                  return (
                    <button
                      type="button"
                      className={selectedServerId === server.id ? "selected" : ""}
                      key={server.id}
                      onClick={() => setSelectedServerId(server.id)}
                    >
                      <span className={`connector-state-dot ${server.enabled ? (health?.ok === false ? "error" : "ready") : "disabled"}`} />
                      <span>
                        <strong>{server.name}</strong>
                        <small>{connectorKind(server)} · {serverTools.length} tool{serverTools.length === 1 ? "" : "s"}</small>
                      </span>
                      <StatusBadge tone={server.enabled ? "success" : "neutral"}>
                        {server.enabled ? "Enabled" : "Off"}
                      </StatusBadge>
                    </button>
                  );
                }) : (
                  <div className="connector-empty">
                    <strong>No connectors configured</strong>
                    <p>Add an API or MCP server to make its capabilities available in chat.</p>
                  </div>
                )}
              </aside>

              <main className="connector-detail">
                {selectedServer ? (
                  <>
                    <section className="connector-detail-hero">
                      <div>
                        <div className="connector-title-row">
                          <h3>{selectedServer.name}</h3>
                          <StatusBadge>{connectorKind(selectedServer)}</StatusBadge>
                        </div>
                        <p>{selectedServer.url || selectedServer.command_json?.join(" ") || "Local built-in integration"}</p>
                      </div>
                      <div className="connector-inline-actions">
                        <button type="button" className="connector-button secondary" onClick={testServer} disabled={busy || !selectedServer.enabled}>
                          Test connection
                        </button>
                        {selectedServer.server_type !== "builtin" && (
                          <button type="button" className="connector-button secondary" onClick={discoverServer} disabled={busy || !selectedServer.enabled}>
                            Discover tools
                          </button>
                        )}
                        {selectedServer.server_type !== "builtin" && (
                          <button type="button" className={`connector-button ${selectedServer.enabled ? "danger" : "primary"}`} onClick={toggleServer} disabled={busy}>
                            {selectedServer.enabled ? "Disable" : "Enable"}
                          </button>
                        )}
                      </div>
                    </section>

                    {selectedHealth && (
                      <section className={`connector-health ${selectedHealth.ok ? "ready" : "error"}`} role="status">
                        <span className="connector-state-dot" />
                        <div>
                          <strong>{selectedHealth.ok ? "Connection ready" : "Connection failed"}</strong>
                          <p>
                            {selectedHealth.error
                              || [
                                selectedHealth.transport && `Transport: ${selectedHealth.transport}`,
                                Number.isInteger(selectedHealth.tool_count) && `Tools: ${selectedHealth.tool_count}`,
                                selectedHealth.protocol_version && `Protocol: ${selectedHealth.protocol_version}`,
                              ].filter(Boolean).join(" · ")
                              || selectedHealth.status}
                          </p>
                        </div>
                      </section>
                    )}

                    <section className="connector-detail-section">
                      <div className="connector-section-heading compact">
                        <div>
                          <h4>Capabilities &amp; permissions</h4>
                          <p>Disable any capability that Neo should not consider.</p>
                        </div>
                        <StatusBadge>{selectedTools.length} tools</StatusBadge>
                      </div>
                      <div className="connector-tool-list">
                        {selectedTools.length ? selectedTools.map((tool) => {
                          const requiresApproval = tool.category?.includes("approval_required");
                          return (
                            <article key={tool.id}>
                              <div>
                                <strong>{tool.display_name || tool.name}</strong>
                                <p>{tool.description || "No description supplied by this connector."}</p>
                                <span className="connector-tool-id">{tool.name}</span>
                              </div>
                              <div className="connector-tool-controls">
                                <StatusBadge tone={requiresApproval ? "warning" : "success"}>
                                  {requiresApproval ? "Approval required" : "Read only"}
                                </StatusBadge>
                                <button type="button" className="connector-button secondary small" onClick={() => toggleTool(tool)} disabled={busy}>
                                  {tool.enabled ? "Disable" : "Enable"}
                                </button>
                              </div>
                            </article>
                          );
                        }) : (
                          <div className="connector-empty compact">
                            <strong>No capabilities discovered</strong>
                            <p>Run discovery after the connector is reachable.</p>
                          </div>
                        )}
                      </div>
                    </section>

                    {selectedServer.server_type === "http" && (
                      <CredentialPanel
                        busy={busy}
                        credential={credential}
                        form={credentialForm}
                        oauthResult={oauthResult}
                        onDelete={deleteCredential}
                        onForm={setCredentialForm}
                        onOAuthRefresh={oauthRefresh}
                        onOAuthRevoke={oauthRevoke}
                        onOAuthStart={oauthStart}
                        onStatusRefresh={checkCredentialStatus}
                        onSave={saveCredential}
                        server={selectedServer}
                      />
                    )}
                    {selectedServer.server_type === "stdio" && (
                      <section className="connector-detail-section">
                        <div className="connector-section-heading compact">
                          <div>
                            <h4>Process environment</h4>
                            <p>
                              Local MCP processes receive only explicitly mapped environment
                              references. Secret values are never shown here or passed through a shell.
                            </p>
                          </div>
                          <StatusBadge tone="warning">Trusted local process</StatusBadge>
                        </div>
                      </section>
                    )}

                    <details className="connector-disclosure connector-detail-section">
                      <summary>Advanced tool definition editor</summary>
                      <p className="connector-help">
                        Most connectors manage definitions through import or discovery. Use this only
                        for custom schemas and existing advanced workflows.
                      </p>
                      <div className="connector-advanced-list">
                        {selectedTools.map((tool) => (
                          <button type="button" key={tool.id} onClick={() => { setEditingTool(tool); setToolForm(fromTool(tool)); }}>
                            {tool.display_name || tool.name}
                          </button>
                        ))}
                        <button type="button" onClick={() => { setEditingTool(null); setToolForm({ ...TOOL_EMPTY, server_id: selectedServer.id }); }}>
                          + New definition
                        </button>
                      </div>
                      <form className="connector-auth-form" onSubmit={saveTool}>
                        <label>Name<input value={toolForm.name} onChange={(event) => setToolForm({ ...toolForm, name: event.target.value })} disabled={Boolean(editingTool)} required /></label>
                        <label>Display name<input value={toolForm.display_name} onChange={(event) => setToolForm({ ...toolForm, display_name: event.target.value })} /></label>
                        <label className="connector-field-wide">Description<textarea rows={2} value={toolForm.description} onChange={(event) => setToolForm({ ...toolForm, description: event.target.value })} /></label>
                        <label>Permission category<select value={toolForm.category} onChange={(event) => setToolForm({ ...toolForm, category: event.target.value })}>{["read_only", "workspace_read", "workspace_write_approval_required", "external_read", "external_write_approval_required", "dangerous_disabled"].map((item) => <option key={item} value={item}>{label(item)}</option>)}</select></label>
                        <label className="connector-field-wide">Input schema JSON<textarea rows={5} value={toolForm.input_schema_text} onChange={(event) => setToolForm({ ...toolForm, input_schema_text: event.target.value })} /></label>
                        <label className="connector-field-wide">Output schema JSON<textarea rows={4} value={toolForm.output_schema_text} onChange={(event) => setToolForm({ ...toolForm, output_schema_text: event.target.value })} /></label>
                        <label className="connector-field-wide">Permissions JSON<textarea rows={3} value={toolForm.permissions_text} onChange={(event) => setToolForm({ ...toolForm, permissions_text: event.target.value })} /></label>
                        <label className="connector-field-wide">Metadata JSON<textarea rows={3} value={toolForm.metadata_text} onChange={(event) => setToolForm({ ...toolForm, metadata_text: event.target.value })} /></label>
                        <label className="connector-confirm"><input type="checkbox" checked={toolForm.enabled} onChange={(event) => setToolForm({ ...toolForm, enabled: event.target.checked })} /> Enabled</label>
                        <div className="connector-form-actions connector-field-wide">
                          <button type="submit" className="connector-button primary" disabled={busy}>Save definition</button>
                        </div>
                      </form>
                    </details>
                  </>
                ) : (
                  <div className="connector-empty large">
                    <strong>Select or add a connector</strong>
                    <p>Its health, authentication status, tools, and permissions will appear here.</p>
                  </div>
                )}
              </main>
            </div>
          </div>
        )}

        {activeTab === "skills" && (
          <div className="connector-tab-content connector-skills-layout">
            <aside className="connector-list" aria-label="Configured skills">
              {skills.map((skill) => (
                <button
                  type="button"
                  className={editingSkill?.id === skill.id ? "selected" : ""}
                  key={skill.id}
                  onClick={() => { setEditingSkill(skill); setSkillForm(fromSkill(skill)); }}
                >
                  <span className={`connector-state-dot ${skill.enabled ? "ready" : "disabled"}`} />
                  <span><strong>{skill.display_name || skill.name}</strong><small>{skill.tool_ids.length} allowed tool(s)</small></span>
                </button>
              ))}
              <button type="button" onClick={() => { setEditingSkill(null); setSkillForm({ ...SKILL_EMPTY }); }}>
                <span className="connector-state-dot ready" /><span><strong>+ New skill</strong><small>Instruction bundle or workflow</small></span>
              </button>
            </aside>
            <main className="connector-detail">
              <section className="connector-detail-section">
                <div className="connector-section-heading compact">
                  <div>
                    <h3>{editingSkill ? "Edit skill" : "Create skill"}</h3>
                    <p>Skills provide instructions and an explicit allowlist of tools.</p>
                  </div>
                </div>
                <form className="connector-auth-form" onSubmit={saveSkill}>
                  <label>Name<input value={skillForm.name} onChange={(event) => setSkillForm({ ...skillForm, name: event.target.value })} disabled={Boolean(editingSkill)} required /></label>
                  <label>Display name<input value={skillForm.display_name} onChange={(event) => setSkillForm({ ...skillForm, display_name: event.target.value })} /></label>
                  <label className="connector-field-wide">Description<textarea rows={2} value={skillForm.description} onChange={(event) => setSkillForm({ ...skillForm, description: event.target.value })} /></label>
                  <label>Type<select value={skillForm.skill_type} onChange={(event) => setSkillForm({ ...skillForm, skill_type: event.target.value })}>{["instruction_bundle", "workflow", "checklist"].map((item) => <option key={item} value={item}>{label(item)}</option>)}</select></label>
                  <label className="connector-field-wide">Instructions<textarea rows={8} value={skillForm.instructions} onChange={(event) => setSkillForm({ ...skillForm, instructions: event.target.value })} required /></label>
                  <fieldset className="connector-tool-picker connector-field-wide">
                    <legend>Allowed tools</legend>
                    {tools.filter((tool) => tool.enabled).map((tool) => (
                      <label key={tool.id}>
                        <input
                          type="checkbox"
                          checked={(skillForm.tool_ids || []).includes(tool.id)}
                          onChange={(event) => setSkillForm({
                            ...skillForm,
                            tool_ids: event.target.checked
                              ? [...(skillForm.tool_ids || []), tool.id]
                              : (skillForm.tool_ids || []).filter((id) => id !== tool.id),
                          })}
                        />
                        <span>{tool.display_name || tool.name}<small>{label(tool.category)}</small></span>
                      </label>
                    ))}
                  </fieldset>
                  <details className="connector-disclosure connector-field-wide">
                    <summary>Advanced routing</summary>
                    <div className="connector-auth-form">
                      <label>Preferred agent IDs<textarea rows={2} value={skillForm.agent_ids_text} onChange={(event) => setSkillForm({ ...skillForm, agent_ids_text: event.target.value })} /></label>
                      <label>Rules profile IDs<textarea rows={2} value={skillForm.rules_profile_ids_text} onChange={(event) => setSkillForm({ ...skillForm, rules_profile_ids_text: event.target.value })} /></label>
                      <label className="connector-field-wide">Metadata JSON<textarea rows={3} value={skillForm.metadata_text} onChange={(event) => setSkillForm({ ...skillForm, metadata_text: event.target.value })} /></label>
                    </div>
                  </details>
                  <label className="connector-confirm"><input type="checkbox" checked={skillForm.enabled} onChange={(event) => setSkillForm({ ...skillForm, enabled: event.target.checked })} /> Enabled</label>
                  <div className="connector-form-actions connector-field-wide">
                    <button type="submit" className="connector-button primary" disabled={busy}>Save skill</button>
                    {editingSkill && <button type="button" className="connector-button danger" onClick={() => execute(() => api.disableToolSkill(editingSkill.id), "Skill disabled.")} disabled={busy || !editingSkill.enabled}>Disable</button>}
                  </div>
                </form>
              </section>
            </main>
          </div>
        )}

        {activeTab === "approvals" && (
          <div className="connector-tab-content connector-approval-page">
            <div className="connector-section-heading">
              <div>
                <p className="connector-eyebrow">Per-call control</p>
                <h3>External action approvals</h3>
                <p>Review the exact tool and arguments. Rejection never contacts the connector.</p>
              </div>
            </div>
            <div className="connector-call-list">
              {calls.length ? calls.map((call) => {
                const tool = tools.find((item) => item.id === call.tool_id);
                const pending = call.approval_status === "pending";
                return (
                  <article className={pending ? "pending" : ""} key={call.id}>
                    <div className="connector-call-heading">
                      <div>
                        <strong>{tool?.display_name || tool?.name || call.tool_id}</strong>
                        <p>{call.created_at ? new Date(call.created_at).toLocaleString() : ""}</p>
                      </div>
                      <StatusBadge tone={pending ? "warning" : call.status === "completed" ? "success" : call.status === "failed" ? "danger" : "neutral"}>
                        {pending ? "Needs approval" : label(call.status)}
                      </StatusBadge>
                    </div>
                    <details className="connector-disclosure">
                      <summary>Review exact arguments</summary>
                      <pre>{JSON.stringify(call.input || {}, null, 2)}</pre>
                    </details>
                    {call.error && <p className="connector-call-error">{call.error}</p>}
                    {call.output && (
                      <details className="connector-disclosure">
                        <summary>Result</summary>
                        <pre>{JSON.stringify(call.output, null, 2)}</pre>
                      </details>
                    )}
                    {pending && (
                      <div className="connector-inline-actions">
                        <button type="button" className="connector-button primary" onClick={() => decideCall(call, true)} disabled={busy}>Approve this call</button>
                        <button type="button" className="connector-button danger" onClick={() => decideCall(call, false)} disabled={busy}>Reject</button>
                      </div>
                    )}
                  </article>
                );
              }) : (
                <div className="connector-empty large">
                  <strong>No connector calls yet</strong>
                  <p>Read calls, approvals, errors, and safe result previews will appear here.</p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

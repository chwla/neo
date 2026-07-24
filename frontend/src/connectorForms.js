const SAFE_METHODS = new Set(["GET", "HEAD"]);
const PARAMETER_LOCATIONS = new Set(["path", "query", "header", "body"]);

export const CONNECTOR_FORM_EMPTY = {
  name: "",
  openapiUrl: "",
  baseUrl: "",
  operationName: "",
  displayName: "",
  description: "",
  method: "GET",
  path: "/",
  parametersText: "",
  executable: "",
  argumentsText: "",
  environmentText: "",
  endpointUrl: "",
  trustedLocalhost: false,
  trustedStdio: false,
};

export const CREDENTIAL_FORM_EMPTY = {
  authType: "none",
  label: "",
  secret: "",
  headerName: "X-API-Key",
  queryName: "api_key",
  clientId: "",
  clientSecret: "",
  authorizationUrl: "",
  tokenUrl: "",
  revocationUrl: "",
  redirectUri: "",
  scopesText: "",
};

export function nonEmptyLines(value) {
  return String(value || "")
    .split("\n")
    .map((item) => item.trim())
    .filter((item) => item && !item.startsWith("#"));
}

export function parseEnvironmentReferences(value) {
  const result = {};
  for (const line of nonEmptyLines(value)) {
    const separator = line.indexOf("=");
    if (separator < 1 || separator === line.length - 1) {
      throw new Error(
        `Environment reference "${line}" must use TARGET=SOURCE_ENV_VARIABLE.`,
      );
    }
    const target = line.slice(0, separator).trim();
    const source = line.slice(separator + 1).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(target)
      || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(source)) {
      throw new Error(`Environment reference "${line}" contains an invalid variable name.`);
    }
    result[target] = source;
  }
  return result;
}

export function parseParameterLocations(value) {
  const result = {};
  for (const line of nonEmptyLines(value)) {
    const match = line.match(/^([A-Za-z_][\w.-]*)\s*:\s*(path|query|header|body)$/i);
    if (!match || !PARAMETER_LOCATIONS.has(match[2].toLowerCase())) {
      throw new Error(
        `Parameter "${line}" must use name: path, query, header, or body.`,
      );
    }
    result[match[1]] = match[2].toLowerCase();
  }
  return result;
}

function simpleInputSchema(parameterLocations) {
  const properties = Object.fromEntries(
    Object.keys(parameterLocations).map((name) => [name, { type: "string" }]),
  );
  return { type: "object", properties, additionalProperties: false };
}

export function buildConnectorRequest(kind, form) {
  const name = String(form.name || "").trim();
  if (!name) {
    throw new Error("Connector name is required.");
  }

  if (kind === "openapi_url") {
    if (!String(form.openapiUrl || "").trim()) {
      throw new Error("OpenAPI document URL is required.");
    }
    return {
      apiMethod: "importOpenApiConnector",
      payload: {
        name,
        document_url: form.openapiUrl.trim(),
        enabled: true,
        allow_trusted_localhost: Boolean(form.trustedLocalhost),
        default_write_approval: true,
      },
    };
  }

  if (kind === "manual_rest") {
    const baseUrl = String(form.baseUrl || "").trim();
    const operationName = String(form.operationName || "").trim();
    const path = String(form.path || "").trim();
    if (!baseUrl || !operationName || !path) {
      throw new Error("Base URL, operation name, and path are required.");
    }
    const method = String(form.method || "GET").toUpperCase();
    const parameterLocations = parseParameterLocations(form.parametersText);
    return {
      apiMethod: "createRestConnector",
      payload: {
        server_name: name,
        base_url: baseUrl,
        name: operationName,
        display_name: String(form.displayName || "").trim() || null,
        description: String(form.description || "").trim() || null,
        method,
        path,
        input_schema: simpleInputSchema(parameterLocations),
        output_schema: {},
        parameter_locations: parameterLocations,
        read_only: SAFE_METHODS.has(method),
        allow_trusted_localhost: Boolean(form.trustedLocalhost),
      },
    };
  }

  if (kind === "mcp_http") {
    const url = String(form.endpointUrl || "").trim();
    if (!url) {
      throw new Error("MCP endpoint URL is required.");
    }
    return {
      apiMethod: "createToolServer",
      discoverAfterCreate: true,
      payload: {
        name,
        server_type: "http",
        command_json: null,
        url,
        env_json: {},
        enabled: true,
        approval_required: true,
        metadata: {
          connector_type: "mcp",
          transport: "streamable_http",
          trusted_localhost: Boolean(form.trustedLocalhost),
        },
      },
    };
  }

  if (kind === "mcp_sse") {
    const url = String(form.endpointUrl || "").trim();
    if (!url) {
      throw new Error("Legacy MCP SSE endpoint URL is required.");
    }
    return {
      apiMethod: "createToolServer",
      discoverAfterCreate: true,
      payload: {
        name,
        server_type: "http",
        command_json: null,
        url,
        env_json: {},
        enabled: true,
        approval_required: true,
        metadata: {
          connector_type: "mcp",
          transport: "legacy_sse",
          trusted_localhost: Boolean(form.trustedLocalhost),
        },
      },
    };
  }

  if (kind === "mcp_stdio") {
    const executable = String(form.executable || "").trim();
    if (!executable) {
      throw new Error("Executable is required.");
    }
    if (!form.trustedStdio) {
      throw new Error("Confirm that you trust this local MCP process before connecting.");
    }
    return {
      apiMethod: "createToolServer",
      discoverAfterCreate: true,
      payload: {
        name,
        server_type: "stdio",
        command_json: [executable, ...nonEmptyLines(form.argumentsText)],
        url: null,
        env_json: parseEnvironmentReferences(form.environmentText),
        enabled: true,
        approval_required: true,
        metadata: {
          connector_type: "mcp",
          transport: "stdio",
          trusted_stdio: true,
        },
      },
    };
  }

  throw new Error("Choose a supported connector type.");
}

export function buildCredentialRequest(form) {
  const authType = String(form.authType || "none");
  const common = {
    auth_type: authType,
    label: String(form.label || "").trim() || null,
  };
  if (authType === "none") return common;
  if (authType === "api_key_header") {
    return {
      ...common,
      secret: String(form.secret || ""),
      header_name: String(form.headerName || "").trim(),
    };
  }
  if (authType === "api_key_query") {
    return {
      ...common,
      secret: String(form.secret || ""),
      query_name: String(form.queryName || "").trim(),
    };
  }
  if (authType === "bearer") {
    return { ...common, secret: String(form.secret || "") };
  }
  if (authType === "oauth2") {
    const scopes = String(form.scopesText || "")
      .split(/[\n, ]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    return {
      ...common,
      client_id: String(form.clientId || "").trim(),
      client_secret: String(form.clientSecret || "") || null,
      authorization_url: String(form.authorizationUrl || "").trim(),
      token_url: String(form.tokenUrl || "").trim(),
      revocation_url: String(form.revocationUrl || "").trim() || null,
      redirect_uri: String(form.redirectUri || "").trim(),
      scopes,
      extra_token_params: {},
    };
  }
  throw new Error("Choose a supported authentication method.");
}

export function connectorKind(server) {
  const metadata = server?.metadata || {};
  if (metadata.connector_type === "openapi") return "OpenAPI";
  if (metadata.connector_type === "rest") return "REST API";
  if (server?.server_type === "stdio") return "MCP · stdio";
  if (metadata.transport === "legacy_sse") return "MCP · legacy SSE";
  if (metadata.connector_type === "mcp" || server?.server_type === "http") {
    return "MCP · Streamable HTTP";
  }
  return "Built-in";
}

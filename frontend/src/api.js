const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";

async function request(path, options = {}) {
  let response;
  try {
    const isForm = options.body instanceof FormData;
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...(options.headers ?? {}),
      },
    });
  } catch (error) {
    throw new Error(
      `Backend API is not reachable. Start FastAPI on http://127.0.0.1:8000. Details: ${
        error.message || error
      }`,
    );
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail = typeof body === "object" && body !== null ? body.detail : body;
    if (response.status === 500 && !detail) {
      throw new Error("Backend API is not running on http://127.0.0.1:8000.");
    }
    throw new Error(detail || `Request failed with ${response.status}`);
  }

  return body;
}

async function streamRequest(path, payload, onEvent) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    throw new Error(
      `Backend API is not reachable. Start FastAPI on http://127.0.0.1:8000. Details: ${
        error.message || error
      }`,
    );
  }

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed with ${response.status}`);
  }
  if (!response.body) {
    throw new Error("Streaming response is not available in this browser.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  while (true) {
    const { done, value } = await reader.read();
    buffered += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const lines = buffered.split("\n");
    buffered = lines.pop() ?? "";
    for (const line of lines) {
      const cleaned = line.trim();
      if (!cleaned) {
        continue;
      }
      const event = JSON.parse(cleaned);
      if (event.type === "error") {
        throw new Error(event.detail || "Streaming request failed.");
      }
      onEvent(event);
    }
    if (done) {
      break;
    }
  }
}

export const api = {
  webSearchPlan: (payload) => request("/web-search/plan", { method: "POST", body: JSON.stringify(payload) }),
  webSearchRun: (payload) => request("/web-search/run", { method: "POST", body: JSON.stringify(payload) }),
  webSearchRuns: () => request("/web-search/runs"),
  webSearchRunDetail: (id) => request(`/web-search/runs/${id}`),
  webSearchCache: () => request("/web-search/cache"),
  memoryItems: () => request("/memory/items"),
  memoryItem: (id) => request(`/memory/items/${id}`),
  memoryScope: (scopeType, scopeId) => request(`/memory/scopes/${encodeURIComponent(scopeType)}/${encodeURIComponent(scopeId)}`),
  indexMemory: (payload = {}) => request("/memory/index", { method: "POST", body: JSON.stringify(payload) }),
  retrieveMemory: (payload) => request("/memory/retrieve", { method: "POST", body: JSON.stringify(payload) }),
  memoryRetrievals: () => request("/memory/retrievals"),
  memoryPrunePreview: (payload = {}) => request("/memory/prune/preview", { method: "POST", body: JSON.stringify(payload) }),
  agenticRuns: () => request("/agentic/runs"),
  agenticRun: (id) => request(`/agentic/runs/${id}`),
  startAgenticRun: (payload) => request("/agentic/runs", { method: "POST", body: JSON.stringify(payload) }),
  planAgenticRun: (id, payload) => request(`/agentic/runs/${id}/plan`, { method: "POST", body: JSON.stringify(payload) }),
  stepAgenticRun: (id, payload = {}) => request(`/agentic/runs/${id}/step`, { method: "POST", body: JSON.stringify(payload) }),
  continueAgenticRun: (id, payload = {}) => request(`/agentic/runs/${id}/continue`, { method: "POST", body: JSON.stringify(payload) }),
  reflectAgenticRun: (id) => request(`/agentic/runs/${id}/reflect`, { method: "POST" }),
  stopAgenticRun: (id) => request(`/agentic/runs/${id}/stop`, { method: "POST" }),
  agenticContext: (id) => request(`/agentic/runs/${id}/context`),
  commandRuns: (workspaceId = "") => request(`/command-sandbox/runs${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ""}`),
  commandRun: (id) => request(`/command-sandbox/runs/${id}`),
  validateCommand: (payload) => request("/command-sandbox/validate", { method: "POST", body: JSON.stringify(payload) }),
  proposeCommand: (payload) => request("/command-sandbox/propose", { method: "POST", body: JSON.stringify(payload) }),
  approveCommand: (id) => request(`/command-sandbox/runs/${id}/approve`, { method: "POST", body: JSON.stringify({ confirm: true }) }),
  executeCommand: (id) => request(`/command-sandbox/runs/${id}/execute`, { method: "POST" }),
  cancelCommand: (id) => request(`/command-sandbox/runs/${id}/cancel`, { method: "POST" }),
  contextSummaries: (params = {}) => {
    const search = new URLSearchParams();
    if (params.scopeType) search.set("scope_type", params.scopeType);
    if (params.scopeId) search.set("scope_id", params.scopeId);
    return request(`/context-memory/summaries?${search.toString()}`);
  },
  contextSummary: (id) => request(`/context-memory/summaries/${id}`),
  contextPreview: (payload) => request("/context-memory/preview", { method: "POST", body: JSON.stringify(payload) }),
  compactContext: (payload) => request("/context-memory/compact", { method: "POST", body: JSON.stringify(payload) }),
  contextEvents: (scopeType, scopeId) => request(`/context-memory/scopes/${scopeType}/${scopeId}/events`),
  createContextEvent: (scopeType, scopeId, payload) => request(`/context-memory/scopes/${scopeType}/${scopeId}/events`, { method: "POST", body: JSON.stringify(payload) }),
  ruleProfiles: () => request("/rules/profiles"),
  agentDefinitions: (includeDisabled = true) =>
    request(`/agents/definitions?include_disabled=${includeDisabled ? "true" : "false"}`),
  createAgentDefinition: (payload) => request("/agents/definitions", {
    method: "POST", body: JSON.stringify(payload),
  }),
  updateAgentDefinition: (id, payload) => request(`/agents/definitions/${id}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  disableAgentDefinition: (id) => request(`/agents/definitions/${id}`, { method: "DELETE" }),
  resetBuiltinAgents: () => request("/agents/definitions/reset-builtins", { method: "POST" }),
  toolServers: (includeDisabled = true) =>
    request(`/tools/servers?include_disabled=${includeDisabled ? "true" : "false"}`),
  createToolServer: (payload) => request("/tools/servers", {
    method: "POST", body: JSON.stringify(payload),
  }),
  updateToolServer: (id, payload) => request(`/tools/servers/${id}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  disableToolServer: (id) => request(`/tools/servers/${id}`, { method: "DELETE" }),
  toolServerHealth: (id) => request(`/tools/servers/${id}/health`, { method: "POST" }),
  discoverToolServer: (id) => request(`/tools/servers/${id}/discover`, { method: "POST" }),
  toolDefinitions: (includeDisabled = true) =>
    request(`/tools/definitions?include_disabled=${includeDisabled ? "true" : "false"}`),
  createToolDefinition: (payload) => request("/tools/definitions", {
    method: "POST", body: JSON.stringify(payload),
  }),
  updateToolDefinition: (id, payload) => request(`/tools/definitions/${id}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  disableToolDefinition: (id) => request(`/tools/definitions/${id}`, { method: "DELETE" }),
  toolSkills: (includeDisabled = true) =>
    request(`/tools/skills?include_disabled=${includeDisabled ? "true" : "false"}`),
  createToolSkill: (payload) => request("/tools/skills", {
    method: "POST", body: JSON.stringify(payload),
  }),
  updateToolSkill: (id, payload) => request(`/tools/skills/${id}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  disableToolSkill: (id) => request(`/tools/skills/${id}`, { method: "DELETE" }),
  toolCalls: (params = {}) => {
    const search = new URLSearchParams();
    if (params.runId) search.set("run_id", params.runId);
    if (params.codingRunId) search.set("coding_run_id", params.codingRunId);
    if (params.status) search.set("status", params.status);
    search.set("limit", String(params.limit ?? 100));
    return request(`/tools/calls?${search.toString()}`);
  },
  createToolCall: (payload) => request("/tools/calls", {
    method: "POST", body: JSON.stringify(payload),
  }),
  approveToolCall: (id) => request(`/tools/calls/${id}/approve`, { method: "POST" }),
  rejectToolCall: (id, reason = null) => request(`/tools/calls/${id}/reject`, {
    method: "POST", body: JSON.stringify({ reason }),
  }),
  agentDelegations: (params = {}) => {
    const search = new URLSearchParams();
    if (params.parentRunId) search.set("parent_run_id", params.parentRunId);
    if (params.childRunId) search.set("child_run_id", params.childRunId);
    if (params.status) search.set("status", params.status);
    search.set("limit", String(params.limit ?? 100));
    return request(`/agents/delegations?${search.toString()}`);
  },
  createRuleProfile: (payload) => request("/rules/profiles", { method: "POST", body: JSON.stringify(payload) }),
  updateRuleProfile: (id, payload) => request(`/rules/profiles/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  disableRuleProfile: (id) => request(`/rules/profiles/${id}`, { method: "DELETE" }),
  resolveRules: (payload) => request("/rules/resolve", { method: "POST", body: JSON.stringify(payload) }),
  importRepoRules: (repoId) => request(`/rules/repos/${repoId}/import`, { method: "POST" }),
  ruleLogs: () => request("/rules/resolution-logs?limit=50"),
  recoveryRuns: (params = {}) => {
    const search = new URLSearchParams();
    if (params.runType) search.set("run_type", params.runType);
    if (params.scan) search.set("scan", "true");
    search.set("limit", String(params.limit ?? 100));
    return request(`/recovery/runs?${search.toString()}`);
  },
  recoveryRun: (runType, runId) => request(`/recovery/runs/${runType}/${runId}`),
  resumeRecoveryRun: (runType, runId) => request(`/recovery/runs/${runType}/${runId}/resume`, {
    method: "POST", body: JSON.stringify({ confirm: true }),
  }),
  retryRecoveryRun: (runType, runId, payload = {}) => request(`/recovery/runs/${runType}/${runId}/retry`, {
    method: "POST", body: JSON.stringify({ confirm: true, ...payload }),
  }),
  forkRecoveryRun: (runType, runId, payload = {}) => request(`/recovery/runs/${runType}/${runId}/fork`, {
    method: "POST", body: JSON.stringify({ confirm: true, ...payload }),
  }),
  repairRecoveryRun: (runType, runId, targetStatus) => request(`/recovery/runs/${runType}/${runId}/repair-state`, {
    method: "POST", body: JSON.stringify({ confirm: true, target_status: targetStatus }),
  }),
  recoveryEvents: (params = {}) => {
    const search = new URLSearchParams();
    if (params.runType) search.set("run_type", params.runType);
    if (params.runId) search.set("run_id", params.runId);
    search.set("limit", String(params.limit ?? 100));
    return request(`/recovery/events?${search.toString()}`);
  },
  startCodingRun: (payload) => request("/coding-agent/runs", {
    method: "POST", body: JSON.stringify(payload),
  }),
  codingRuns: (params = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries({
      task_id: params.taskId, project_id: params.projectId, repo_id: params.repoId,
      status: params.status,
    })) if (value) search.set(key, value);
    search.set("limit", String(params.limit ?? 50));
    return request(`/coding-agent/runs?${search.toString()}`);
  },
  codingRun: (runId) => request(`/coding-agent/runs/${runId}`),
  approveCodingAction: (actionId, options = {}) => request(
    `/coding-agent/actions/${actionId}/approve`, {
      method: "POST", body: JSON.stringify({ confirm: true, options }),
    },
  ),
  rejectCodingAction: (actionId, reason = null) => request(
    `/coding-agent/actions/${actionId}/reject`, {
      method: "POST", body: JSON.stringify({ reason }),
    },
  ),
  reviseCodingPatch: (runId, instructions) => request(
    `/coding-agent/runs/${runId}/revise-patch`, {
      method: "POST", body: JSON.stringify({ instructions }),
    },
  ),
  cancelCodingRun: (runId) => request(`/coding-agent/runs/${runId}/cancel`, {
    method: "POST",
  }),
  lspStatus: () => request("/lsp/status"),
  lspServers: () => request("/lsp/servers"),
  lspStart: (workspaceId, language = "python") => request(`/lsp/workspaces/${workspaceId}/start`, {
    method: "POST", body: JSON.stringify({ language }),
  }),
  lspStop: (workspaceId) => request(`/lsp/workspaces/${workspaceId}/stop`, {
    method: "POST", body: JSON.stringify({}),
  }),
  lspDiagnostics: (workspaceId) => request(`/lsp/workspaces/${workspaceId}/diagnostics`),
  lspQuery: (workspaceId, action, payload = {}) => request(`/lsp/workspaces/${workspaceId}/${action}`, {
    method: "POST", body: JSON.stringify(payload),
  }),
  exportBundle: (payload) => request("/bundles/export", { method: "POST", body: JSON.stringify(payload) }),
  bundleExports: () => request("/bundles/exports"),
  bundleExport: (bundleId) => request(`/bundles/exports/${bundleId}`),
  bundleImports: () => request("/bundles/imports"),
  bundleImport: (bundleId) => request(`/bundles/imports/${bundleId}`),
  validateBundle: (file) => { const form = new FormData(); form.append("file", file); return request("/bundles/import/validate", { method: "POST", body: form }); },
  importBundle: (file) => { const form = new FormData(); form.append("file", file); form.append("confirm", "true"); form.append("mode", "archive_only"); return request("/bundles/import", { method: "POST", body: form }); },
  githubConnections: () => request("/github/connections"),
  createGithubConnection: (payload) => request("/github/connections", { method: "POST", body: JSON.stringify(payload) }),
  githubHealth: (id) => request(`/github/connections/${id}/health`, { method: "POST" }),
  importGithubIssue: (id, number) => request(`/github/connections/${id}/issues/${number}/import`, { method: "POST" }),
  importGithubPr: (id, number) => request(`/github/connections/${id}/pulls/${number}/import`, { method: "POST" }),
  githubItems: () => request("/github/items"),
  githubOperations: () => request("/github/operations"),
  githubCreateTask: (id) => request(`/github/items/${id}/create-task`, { method: "POST" }),
  codeIndex: (repoId) => request(`/code-index/repos/${repoId}`),
  buildCodeIndex: (repoId, force = false) => request(`/code-index/repos/${repoId}/build`, {
    method: "POST", body: JSON.stringify({ force, summarize: true }),
  }),
  codeSymbols: (repoId, params = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.symbolType) search.set("symbol_type", params.symbolType);
    search.set("limit", String(params.limit ?? 100));
    return request(`/code-index/repos/${repoId}/symbols?${search.toString()}`);
  },
  codeRoutes: (repoId) => request(`/code-index/repos/${repoId}/routes`),
  codeDependencies: (repoId, relativePath = "") => {
    const search = new URLSearchParams();
    if (relativePath) search.set("relative_path", relativePath);
    return request(`/code-index/repos/${repoId}/dependencies?${search.toString()}`);
  },
  codeSearch: (repoId, q) => request(
    `/code-index/repos/${repoId}/search?${new URLSearchParams({ q, limit: "50" })}`,
  ),
  symbolAwareness: (repoId) => request(`/symbols/repos/${repoId}`),
  buildSymbolAwareness: (repoId, force = false) => request(
    `/symbols/repos/${repoId}/build`, {
      method: "POST", body: JSON.stringify({ force }),
    },
  ),
  symbolDefinitions: (repoId, name) => request(
    `/symbols/repos/${repoId}/definition?${new URLSearchParams({ name })}`,
  ),
  symbolReferencesByName: (repoId, name) => request(
    `/symbols/repos/${repoId}/references?${new URLSearchParams({ name, limit: "100" })}`,
  ),
  documentSymbols: (repoId, repoFileId) => request(
    `/symbols/repos/${repoId}/files/${repoFileId}/document-symbols`,
  ),
  relatedCodeFiles: (repoId, repoFileId) => request(
    `/symbols/repos/${repoId}/files/${repoFileId}/related-files`,
  ),
  reposList: (params = {}) => {
    const search = new URLSearchParams();
    if (params.projectId) search.set("project_id", params.projectId);
    search.set("limit", String(params.limit ?? 100));
    return request(`/repos?${search.toString()}`);
  },
  registerRepo: (payload) => request("/repos/register", {
    method: "POST", body: JSON.stringify(payload),
  }),
  repo: (repoId) => request(`/repos/${repoId}`),
  repoFiles: (repoId, params = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries({
      q: params.q, extension: params.extension, language: params.language,
    })) if (value) search.set(key, value);
    search.set("limit", String(params.limit ?? 500));
    return request(`/repos/${repoId}/files?${search.toString()}`);
  },
  repoFile: (repoId, repoFileId) => request(`/repos/${repoId}/files/${repoFileId}`),
  deleteRepo: (repoId) => request(`/repos/${repoId}`, { method: "DELETE" }),
  testCommands: (repoId) => request(`/test-runner/repos/${repoId}/commands`),
  detectTestCommands: (repoId) => request(`/test-runner/repos/${repoId}/detect`, { method: "POST" }),
  createTestCommand: (repoId, payload) => request(`/test-runner/repos/${repoId}/commands`, {
    method: "POST", body: JSON.stringify(payload),
  }),
  updateTestCommand: (commandId, payload) => request(`/test-runner/commands/${commandId}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  disableTestCommand: (commandId) => request(`/test-runner/commands/${commandId}`, { method: "DELETE" }),
  runTestCommand: (commandId, payload) => request(`/test-runner/commands/${commandId}/run`, {
    method: "POST", body: JSON.stringify(payload),
  }),
  testRuns: (params = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries({
      repo_id: params.repoId, project_id: params.projectId, task_id: params.taskId,
      agent_run_id: params.agentRunId, patch_application_id: params.patchApplicationId,
      status: params.status,
    })) if (value) search.set(key, value);
    search.set("limit", String(params.limit ?? 50));
    return request(`/test-runner/runs?${search.toString()}`);
  },
  testRun: (runId) => request(`/test-runner/runs/${runId}`),
  gitStatus: (repoId) => request(`/git/repos/${repoId}/status`),
  initGit: (repoId) => request(`/git/repos/${repoId}/init`, {
    method: "POST", body: JSON.stringify({ confirm: true }),
  }),
  gitDiff: (repoId, path = "") => {
    const search = new URLSearchParams();
    if (path) search.set("path", path);
    return request(`/git/repos/${repoId}/diff?${search.toString()}`);
  },
  gitCheckpoints: (repoId, params = {}) => {
    const search = new URLSearchParams();
    if (params.taskId) search.set("task_id", params.taskId);
    if (params.patchApplicationId) search.set("patch_application_id", params.patchApplicationId);
    search.set("limit", String(params.limit ?? 50));
    return request(`/git/repos/${repoId}/checkpoints?${search.toString()}`);
  },
  createGitCheckpoint: (repoId, payload) => request(`/git/repos/${repoId}/checkpoints`, {
    method: "POST", body: JSON.stringify({ ...payload, confirm: true }),
  }),
  gitCheckpoint: (checkpointId) => request(`/git/checkpoints/${checkpointId}`),
  restoreGitCheckpoint: (checkpointId) => request(`/git/checkpoints/${checkpointId}/restore`, {
    method: "POST", body: JSON.stringify({ confirm: true }),
  }),
  gitOperations: (repoId) => request(`/git/repos/${repoId}/operations`),
  filesList: (params = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries({
      q: params.q, extension: params.extension, project_id: params.projectId,
      task_id: params.taskId, note_id: params.noteId,
    })) if (value) search.set(key, value);
    search.set("limit", String(params.limit ?? 100));
    return request(`/files?${search.toString()}`);
  },
  file: (fileId) => request(`/files/${fileId}`),
  uploadFile: (file, links = {}) => {
    const form = new FormData();
    form.append("file", file);
    if (links.projectId) form.append("project_id", links.projectId);
    if (links.taskId) form.append("task_id", links.taskId);
    if (links.noteId) form.append("note_id", links.noteId);
    return request("/files/upload", { method: "POST", body: form });
  },
  deleteFile: (fileId) => request(`/files/${fileId}`, { method: "DELETE" }),
  summarizeFile: (fileId) => request(`/files/${fileId}/summarize`, { method: "POST" }),
  attachFile: (fileId, linkType, targetId) => request(`/files/${fileId}/links`, {
    method: "POST", body: JSON.stringify({ link_type: linkType, target_id: targetId }),
  }),
  detachFile: (fileId, linkId) => request(`/files/${fileId}/links/${linkId}`, { method: "DELETE" }),
  fileDownloadUrl: (fileId) => `${API_BASE}/files/${fileId}/download`,
  artifactsList: (params = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries({ project_id: params.projectId, task_id: params.taskId,
      agent_run_id: params.agentRunId, artifact_type: params.artifactType })) if (value) search.set(key, value);
    return request(`/artifacts?${search.toString()}`);
  },
  createArtifact: (payload) => request("/artifacts", { method: "POST", body: JSON.stringify(payload) }),
  artifact: (artifactId) => request(`/artifacts/${artifactId}`),
  artifactDownloadUrl: (artifactId) => `${API_BASE}/artifacts/${artifactId}/download`,
  proposePatch: (payload) => request("/patches/propose", {
    method: "POST", body: JSON.stringify(payload),
  }),
  validatePatchApply: (artifactId, fileId = null) => request(
    `/patches/${artifactId}/validate-apply`, {
      method: "POST", body: JSON.stringify({ file_id: fileId }),
    },
  ),
  applyPatch: (artifactId, fileId = null) => request(`/patches/${artifactId}/apply`, {
    method: "POST", body: JSON.stringify({ file_id: fileId, confirm: true }),
  }),
  patchApplications: (params = {}) => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries({
      artifact_id: params.artifactId, file_id: params.fileId, task_id: params.taskId,
      project_id: params.projectId, agent_run_id: params.agentRunId,
    })) if (value) search.set(key, value);
    return request(`/patches/applications?${search.toString()}`);
  },
  patchApplication: (applicationId) => request(`/patches/applications/${applicationId}`),
  patchApplicationDownloadUrl: (applicationId, version = null) =>
    `${API_BASE}/patches/applications/${applicationId}/download${version ? `?version=${version}` : ""}`,
  sidebar: () => request("/sidebar"),
  createChat: (projectId = null) =>
    request("/chats", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId }),
    }),
  getChat: (chatId) => request(`/chats/${chatId}`),
  sendMessage: (chatId, prompt, llmId = null) =>
    request(`/chats/${chatId}/messages`, {
      method: "POST",
      body: JSON.stringify({ prompt, llm_id: llmId }),
    }),
  streamMessage: (chatId, prompt, onEvent, llmId = null) =>
    streamRequest(`/chats/${chatId}/messages/stream`, { prompt, llm_id: llmId }, onEvent),
  llms: () => request("/llms"),
  selectLlm: (id) =>
    request("/llms/active/select", { method: "PUT", body: JSON.stringify({ id }) }),
  saveLlm: (config) =>
    request(`/llms/${encodeURIComponent(config.id)}`, {
      method: "PUT",
      body: JSON.stringify(config),
    }),
  deleteLlm: (id) => request(`/llms/${encodeURIComponent(id)}`, { method: "DELETE" }),
  testLlm: (id) => request(`/llms/${encodeURIComponent(id)}/test`, { method: "POST" }),
  llmProviders: () => request("/llm/providers"),
  createLlmProvider: (payload) => request("/llm/providers", {
    method: "POST", body: JSON.stringify(payload),
  }),
  updateLlmProvider: (id, payload) => request(`/llm/providers/${encodeURIComponent(id)}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  deleteLlmProvider: (id) => request(`/llm/providers/${encodeURIComponent(id)}`, {
    method: "DELETE",
  }),
  llmModels: (providerId = "") => request(
    `/llm/models${providerId ? `?provider_id=${encodeURIComponent(providerId)}` : ""}`,
  ),
  createLlmModel: (payload) => request("/llm/models", {
    method: "POST", body: JSON.stringify(payload),
  }),
  updateLlmModel: (id, payload) => request(`/llm/models/${encodeURIComponent(id)}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  deleteLlmModel: (id) => request(`/llm/models/${encodeURIComponent(id)}`, {
    method: "DELETE",
  }),
  llmRoutes: () => request("/llm/routes"),
  updateLlmRoute: (name, payload) => request(`/llm/routes/${encodeURIComponent(name)}`, {
    method: "PATCH", body: JSON.stringify(payload),
  }),
  testLlmRoute: (routeName) => request("/llm/health", {
    method: "POST", body: JSON.stringify({ route_name: routeName }),
  }),
  testLlmProvider: (providerId, modelId) => request("/llm/health", {
    method: "POST", body: JSON.stringify({ provider_id: providerId, model_id: modelId }),
  }),
  llmUsage: () => request("/llm/usage?limit=50"),
  updateChatMessage: (chatId, messageId, content) =>
    request(`/chats/${chatId}/messages/${messageId}`, {
      method: "PATCH",
      body: JSON.stringify({ content }),
    }),
  deleteChat: (chatId) => request(`/chats/${chatId}`, { method: "DELETE" }),
  createProject: (name) =>
    request("/chat-projects", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  deleteProject: (projectId) => request(`/chat-projects/${projectId}`, { method: "DELETE" }),
  memory: () =>
    Promise.all([
      request("/profile"),
      request("/preferences"),
      request("/goals"),
      request("/chat-projects"),
      request("/events"),
      request("/memories"),
    ]).then(([profile, preferences, goals, projects, events, memories]) => ({
      profile,
      preferences,
      goals,
      projects,
      events,
      memories,
    })),
  updateProfile: (id, payload) =>
    request(`/profile/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteProfile: (id) => request(`/profile/${id}`, { method: "DELETE" }),
  updatePreference: (id, payload) =>
    request(`/preferences/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deletePreference: (id) => request(`/preferences/${id}`, { method: "DELETE" }),
  updateGoal: (id, payload) =>
    request(`/goals/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteGoal: (id) => request(`/goals/${id}`, { method: "DELETE" }),
  updateProjectMemory: (id, payload) =>
    request(`/chat-projects/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteProjectMemory: (id) => request(`/chat-projects/${id}/memory`, { method: "DELETE" }),
  updateEvent: (id, payload) =>
    request(`/events/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteEvent: (id) => request(`/events/${id}`, { method: "DELETE" }),
  updateMemory: (id, payload) =>
    request(`/memories/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteMemory: (id) => request(`/memories/${id}`, { method: "DELETE" }),
  searchConfig: () => request("/search/config"),
  updateSearchConfig: (payload) =>
    request("/search/config", { method: "POST", body: JSON.stringify(payload) }),
  testSearchProvider: (payload = {}) =>
    request("/search/test", { method: "POST", body: JSON.stringify(payload) }),

  researchClear: () => request("/research/clear", { method: "DELETE" }),
  researchStart: (payload) =>
    request("/research/start", { method: "POST", body: JSON.stringify(payload) }),
  researchList: (limit = 20) => request(`/research/list?limit=${limit}`),
  researchJob: (jobId) => request(`/research/${jobId}`),
  researchStatus: (jobId) => request(`/research/${jobId}/status`),
  researchReport: (jobId) => request(`/research/${jobId}/report`),
  researchCancel: (jobId) =>
    request(`/research/${jobId}/cancel`, { method: "POST" }),
  researchSaveToNote: (jobId, payload = {}) =>
    request(`/research/${jobId}/save-to-note`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  researchEvents: (jobId) => `${API_BASE}/research/${jobId}/events`,
  researchModePlan: (payload) => request("/research/plan", { method: "POST", body: JSON.stringify(payload) }),
  researchModeRun: (payload) => request("/research/run", { method: "POST", body: JSON.stringify(payload) }),
  researchModeRuns: () => request("/research/runs"),
  researchModeDetail: (runId) => request(`/research/runs/${runId}`),
  researchModeEvidence: (runId) => request(`/research/runs/${runId}/evidence`),
  researchModeClaims: (runId) => request(`/research/runs/${runId}/claims`),
  researchModeConflicts: (runId) => request(`/research/runs/${runId}/conflicts`),
  researchModeReport: (runId) => request(`/research/runs/${runId}/report`),
  researchModeContinue: (runId) => request(`/research/runs/${runId}/continue`, { method: "POST" }),
  researchModeRefresh: (runId) => request(`/research/runs/${runId}/refresh`, { method: "POST" }),
  researchModeValidate: (runId) => request(`/research/runs/${runId}/validate-citations`, { method: "POST" }),

  notesList: (params = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.tag) search.set("tag", params.tag);
    if (params.includeArchived) search.set("include_archived", "true");
    if (params.pinnedFirst === false) search.set("pinned_first", "false");
    search.set("limit", String(params.limit ?? 50));
    search.set("offset", String(params.offset ?? 0));
    return request(`/notes?${search.toString()}`);
  },
  notesTags: () => request("/notes/tags"),
  note: (noteId) => request(`/notes/${noteId}`),
  createNote: (payload) =>
    request("/notes", { method: "POST", body: JSON.stringify(payload) }),
  updateNote: (noteId, payload) =>
    request(`/notes/${noteId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  pinNote: (noteId, pinned) =>
    request(`/notes/${noteId}/pin`, {
      method: "POST",
      body: JSON.stringify({ pinned }),
    }),
  archiveNote: (noteId, archived) =>
    request(`/notes/${noteId}/archive`, {
      method: "POST",
      body: JSON.stringify({ archived }),
    }),
  deleteNote: (noteId) => request(`/notes/${noteId}`, { method: "DELETE" }),

  projectsList: (params = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.tag) search.set("tag", params.tag);
    if (params.status) search.set("status", params.status);
    if (params.includeArchived) search.set("include_archived", "true");
    if (params.pinnedFirst === false) search.set("pinned_first", "false");
    search.set("limit", String(params.limit ?? 50));
    search.set("offset", String(params.offset ?? 0));
    return request(`/projects?${search.toString()}`);
  },
  projectsTags: () => request("/projects/tags"),
  project: (projectId) => request(`/projects/${projectId}`),
  createWorkspaceProject: (payload) =>
    request("/projects", { method: "POST", body: JSON.stringify(payload) }),
  updateWorkspaceProject: (projectId, payload) =>
    request(`/projects/${projectId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  pinProject: (projectId, pinned) =>
    request(`/projects/${projectId}/pin`, {
      method: "POST",
      body: JSON.stringify({ pinned }),
    }),
  archiveProject: (projectId, archived) =>
    request(`/projects/${projectId}/archive`, {
      method: "POST",
      body: JSON.stringify({ archived }),
    }),
  deleteWorkspaceProject: (projectId) => request(`/projects/${projectId}`, { method: "DELETE" }),
  attachNoteToProject: (projectId, noteId) =>
    request(`/projects/${projectId}/notes`, {
      method: "POST",
      body: JSON.stringify({ note_id: noteId }),
    }),
  detachNoteFromProject: (projectId, noteId) =>
    request(`/projects/${projectId}/notes/${noteId}`, { method: "DELETE" }),
  projectNotes: (projectId) => request(`/projects/${projectId}/notes`),
  noteProjects: (noteId) => request(`/projects/notes/${noteId}/projects`),

  tasksList: (params = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.status) search.set("status", params.status);
    if (params.priority) search.set("priority", params.priority);
    if (params.projectId) search.set("project_id", params.projectId);
    if (params.parentTaskId) search.set("parent_task_id", params.parentTaskId);
    if (params.tag) search.set("tag", params.tag);
    if (params.dueBefore) search.set("due_before", params.dueBefore);
    if (params.dueAfter) search.set("due_after", params.dueAfter);
    if (params.includeArchived) search.set("include_archived", "true");
    if (params.includeDone === false) search.set("include_done", "false");
    if (params.pinnedFirst === false) search.set("pinned_first", "false");
    search.set("limit", String(params.limit ?? 50));
    search.set("offset", String(params.offset ?? 0));
    return request(`/tasks?${search.toString()}`);
  },
  tasksTags: () => request("/tasks/tags"),
  task: (taskId) => request(`/tasks/${taskId}`),
  createTask: (payload) => request("/tasks", { method: "POST", body: JSON.stringify(payload) }),
  updateTask: (taskId, payload) =>
    request(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  setTaskStatus: (taskId, status) =>
    request(`/tasks/${taskId}/status`, { method: "POST", body: JSON.stringify({ status }) }),
  pinTask: (taskId, pinned) =>
    request(`/tasks/${taskId}/pin`, { method: "POST", body: JSON.stringify({ pinned }) }),
  archiveTask: (taskId, archived) =>
    request(`/tasks/${taskId}/archive`, { method: "POST", body: JSON.stringify({ archived }) }),
  deleteTask: (taskId) => request(`/tasks/${taskId}`, { method: "DELETE" }),
  attachNoteToTask: (taskId, noteId) =>
    request(`/tasks/${taskId}/notes`, { method: "POST", body: JSON.stringify({ note_id: noteId }) }),
  detachNoteFromTask: (taskId, noteId) =>
    request(`/tasks/${taskId}/notes/${noteId}`, { method: "DELETE" }),
  taskNotes: (taskId) => request(`/tasks/${taskId}/notes`),
  noteTasks: (noteId) => request(`/tasks/notes/${noteId}/tasks`),
  projectTasks: (projectId, params = {}) => {
    const search = new URLSearchParams();
    if (params.status) search.set("status", params.status);
    if (params.includeDone === false) search.set("include_done", "false");
    if (params.includeArchived) search.set("include_archived", "true");
    return request(`/projects/${projectId}/tasks?${search.toString()}`);
  },
  createProjectTask: (projectId, payload) =>
    request(`/projects/${projectId}/tasks`, { method: "POST", body: JSON.stringify(payload) }),

  agentRuns: (params = {}) => {
    const search = new URLSearchParams();
    if (params.taskId) search.set("task_id", params.taskId);
    if (params.projectId) search.set("project_id", params.projectId);
    if (params.status) search.set("status", params.status);
    search.set("limit", String(params.limit ?? 50));
    search.set("offset", String(params.offset ?? 0));
    return request(`/agents/runs?${search.toString()}`);
  },
  taskAgentRuns: (taskId) => request(`/tasks/${taskId}/agent-runs`),
  agentRun: (runId) => request(`/agents/runs/${runId}`),
  startAgentRun: (payload) =>
    request("/agents/runs", { method: "POST", body: JSON.stringify(payload) }),
  planAgentTasks: (payload) =>
    request("/agents/plan-tasks", { method: "POST", body: JSON.stringify(payload) }),
  startAgentRunFromObjective: (payload) =>
    request("/agents/runs/from-objective", { method: "POST", body: JSON.stringify(payload) }),
  cancelAgentRun: (runId) =>
    request(`/agents/runs/${runId}/cancel`, { method: "POST" }),
  approveAgentStep: (runId, stepId, approved) =>
    request(`/agents/runs/${runId}/steps/${stepId}/approve`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),
  saveAgentRunToNote: (runId, payload = {}) =>
    request(`/agents/runs/${runId}/save-to-note`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};

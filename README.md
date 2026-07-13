# Neo

Neo is a local-first AI workbench for research, planning, coding, validation, and continuity.
It combines a FastAPI backend, a React/Vite frontend, a SQLite workspace database, a CLI/TUI,
and controlled service layers for LLM providers, web search, code analysis, patches, tests,
Git checkpoints, artifacts, and recovery.

The core design principle is visibility before automation: Neo can help inspect, plan, propose,
run, and preserve work, but risky operations stay behind explicit approval gates and are recorded
for later review.

## What is included

### Application surfaces

- Web app: React panels for projects, tasks, notes, research, coding runs, repos, providers,
  bundles, recovery, evaluations, workspaces, continuity, and settings.
- API: FastAPI route groups under `/api/...` for every major platform feature.
- CLI: `neo` command groups for headless operator workflows.
- TUI: terminal dashboard components under `app/cli/tui/`.
- Docker runtime: single-container build that serves the backend and built frontend.

### Core platform areas

- Projects, tasks, notes, files, and artifacts for organizing local work.
- Chat and memory services for structured memory, reflection, retrieval, pruning, and audits.
- Context Memory and Memory Retrieval for scoped, redacted workspace context.
- Reliable Web Search and Enterprise Research Mode for bounded, citation-backed research.
- LLM registry and Provider Runtime for provider/model routing, fallbacks, health, usage,
  budgets, retries, streaming, and secret redaction.
- Agent framework, agent task runs, Coding Agent, and Agentic Core for persisted multi-step
  workflows with plan, inspect, act, verify, reflect, and continue phases.
- Repository registration, Codebase Index, Symbol Awareness, and LSP support for code-aware
  context.
- Controlled Patch Apply, Command Sandbox, Test Runner, and local Git checkpoints for guarded
  code operations inside managed repository copies.
- GitHub integration, tools and skills registry, rules profiles, evaluation harness, recovery
  scanner, session bundles, workspace orchestration, continuity bundles, and integration checks.

## Architecture

Neo is organized into five layers:

| Layer | Location | Responsibility |
| --- | --- | --- |
| Frontend | `frontend/src/` | React screens and settings panels. |
| API | `app/api/routes/` | FastAPI route groups mounted by `app/main.py`. |
| Services | `app/services/` | Business logic, safety checks, orchestration, provider calls, and persistence workflows. |
| Storage | `app/models/`, `app/db/`, service stores | SQLite-backed workspace state and filesystem artifacts. |
| CLI/TUI | `app/cli/` | Terminal commands and API-backed operator views. |

The backend app is created in `app/main.py`. On startup it mounts all route groups, initializes
workspace tables, seeds built-in tools/agents/evaluations, scans recovery state, and serves the
built frontend when `frontend/dist/index.html` is available.

## Repository layout

```text
app/
  api/routes/                  FastAPI routes
  cli/                         CLI and terminal UI
  core/                        Settings and configuration
  db/                          Database session setup
  models/                      SQLAlchemy models
  repositories/                Repository abstractions
  schemas/                     Shared Pydantic schemas
  services/                    Platform service modules
frontend/
  src/                         React application panels
  package.json                 Vite/Tailwind frontend scripts
tests/                         Backend integration tests
Dockerfile                     Single-container runtime build
pyproject.toml                 Python package, CLI entry point, lint/test config
```

## Requirements

- Python 3.12+
- Node.js and npm for frontend development
- Docker, optional, for the single-container runtime
- Ollama or an OpenAI-compatible provider, optional, for model-backed features
- External SearXNG, optional, for live web search

Neo can run with search disabled. In that mode, research/search features record a clear degraded
state instead of inventing unsupported evidence.

## Local development

Create and install the Python environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Run the backend:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run the frontend in another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The Vite dev server proxies `/api` requests to
`http://127.0.0.1:8000`.

## Docker

Build and run the single-container app:

```bash
docker build -t neo:local .
docker run --rm \
  --name neo \
  -p 8000:8000 \
  -v neo_data:/app/data \
  -e NEO_SEARCH_PROVIDER=disabled \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  neo:local
```

Open `http://127.0.0.1:8000`.

The container stores SQLite data, uploaded files, managed repository copies, run state, bundles,
and workspace artifacts under `/app/data`. Ollama, model weights, and SearXNG are not bundled.

## Configuration

Common runtime variables:

| Variable | Purpose |
| --- | --- |
| `NEO_DATA_DIR` | Storage root for SQLite and workspace files. |
| `NEO_SEARCH_PROVIDER` | `disabled` or `external_searxng`. |
| `NEO_SEARXNG_URL` | External SearXNG URL when search is enabled. |
| `NEO_LLM_PROVIDER` | Initial provider, usually `ollama` or `openai_compatible`. |
| `OLLAMA_BASE_URL` | Ollama endpoint. |
| `NEO_DEFAULT_MODEL` | Initial default model route target. |
| `NEO_OPENAI_COMPAT_BASE_URL` | OpenAI-compatible `/v1` base URL. |
| `NEO_OPENAI_COMPAT_API_KEY_REF` | Name of the environment variable containing the provider key. |
| `NEO_OPENAI_COMPAT_MODEL` | Initial OpenAI-compatible model name. |

Provider API key values are not stored as provider records. Neo stores a variable name such as
`OPENAI_API_KEY`, resolves it at call time, and redacts secret values from stored errors.

## CLI

Installing the Python package exposes the `neo` command:

```bash
neo status --api-url http://127.0.0.1:8000
neo health
neo research plan "Compare local coding-agent architectures" --mode technical --fresh
neo research run "Compare local coding-agent architectures" --mode technical --fresh --depth deep
neo providers list
neo eval list
neo workspace list
neo continuity list
neo coding start "Investigate failing tests" --repo <repo-id> --agent coder
neo agentic start --type coding --objective "Investigate failing tests"
neo recovery list
neo rules resolve --repo <repo-id> --context coding_agent
neo tools list
neo skills list
neo tests list --repo <repo-id>
neo git status <repo-id>
neo export run <run-id> --out run.neo.zip
neo bundles import validate run.neo.zip
neo tui
```

Set `NEO_API_URL` for the default server URL. Use `--json` or `NEO_CLI_OUTPUT=json` for
machine-readable output. Approval-sensitive commands still use Neo's backend confirmation gates;
`--yes` only skips the local CLI prompt.

## Safety model

Neo's guarded workflows are intentionally constrained:

- Original repositories are not edited directly. Repo operations use managed copies.
- Patch apply validates paths, hashes, hunks, and metadata before explicit approval.
- Patch application is atomic and does not automatically run tests or create checkpoints.
- Test Runner executes only saved, allowlisted argv commands after confirmation.
- Command Sandbox, tools, external writes, patches, tests, and checkpoints keep approval records.
- Git support is limited to local checkpointing inside managed repository copies.
- Remote Git operations, arbitrary shell access, package installation, and automatic commits are
  outside the controlled workflow.
- Bundles and continuity exports redact credentials, environment values, provider secrets, and
  sensitive host paths.
- Missing providers create persisted degraded states instead of fabricated success.

## Feature map

| Feature area | UI | API | CLI | Main code |
| --- | --- | --- | --- | --- |
| Chat and memory | main app memory surfaces | `/conversation`, `/api/memory` | indirect | `app/services/chat.py`, `app/services/extraction.py`, `app/services/retrieval.py` |
| Projects | `Projects.jsx` | `/api/projects` | none | `app/services/projects/` |
| Tasks | `Tasks.jsx` | `/api/tasks` | indirect | `app/services/tasks/` |
| Notes | `Notes.jsx` | `/api/notes` | none | `app/services/notes/` |
| Files and artifacts | `Files.jsx`, `ArtifactsPanel.jsx` | `/api/files` | none | `app/services/files/` |
| Reliable Web Search | `WebSearch.jsx` | `/api/search`, `/api/web`, `/api/web-search` | `neo web ...` | `app/services/search/`, `app/services/web_search/` |
| Research Mode | `Research.jsx` | `/api/research` | `neo research ...` | `app/services/research_mode/` |
| LLM registry | settings | `/api/llm`, `/api/llms` | indirect | `app/services/llm_registry/` |
| Provider Runtime | `ProviderRuntime.jsx` | `/api/providers/runtime` | `neo providers ...` | `app/services/provider_runtime/` |
| Evaluation Harness | `EvaluationHarness.jsx` | `/api/evals` | `neo eval ...` | `app/services/evaluation/` |
| Workspace Orchestration | `WorkspaceOrchestration.jsx` | `/api/workspaces` | `neo workspace ...` | `app/services/workspace_orchestration/` |
| Continuity | `Continuity.jsx` | `/api/continuity` | `neo continuity ...` | `app/services/continuity/` |
| Bundles | `Bundles.jsx` | `/api/bundles` | `neo bundles ...`, `neo export ...` | `app/services/bundles/` |
| Repos and code index | `Repos.jsx`, `CodebaseIndex.jsx` | `/api/repos`, `/api/code-index` | repo-backed flows | `app/services/repos/`, `app/services/code_index/` |
| Symbol and LSP support | `SymbolAwareness.jsx`, `LspPanel.jsx` | `/api/symbols`, `/api/lsp` | `neo lsp ...` | `app/services/symbol_awareness/`, `app/services/lsp/` |
| Command Sandbox | `CommandSandbox.jsx` | `/api/command-sandbox` | `neo commands ...` | `app/services/command_sandbox/` |
| Test Runner | `TestRunner.jsx` | `/api/test-runner` | `neo tests ...` | `app/services/test_runner/` |
| Patch apply | `PatchApplications.jsx` | `/api/patches` | indirect | `app/services/patches/`, `app/services/patch_apply/` |
| Git checkpoints | `GitCheckpoints.jsx` | `/api/git` | `neo git ...` | `app/services/git/` |
| Coding Agent | `CodingAgent.jsx` | `/api/coding-agent` | `neo coding ...` | `app/services/coding_agent/` |
| Agentic Core | `AgenticRuns.jsx` | `/api/agentic` | `neo agentic ...` | `app/services/agentic_core/` |
| Agents | `AgentSettings.jsx` | `/api/agents` | `neo agents ...` | `app/services/agent_framework/`, `app/services/agents/` |
| Rules, tools, skills | settings panels | `/api/rules`, `/api/tools` | `neo rules ...`, `neo tools ...`, `neo skills ...` | `app/services/rules/`, `app/services/tools/` |
| GitHub | `GitHub.jsx` | `/api/github` | `neo github ...` | `app/services/github/` |
| Context and memory retrieval | `ContextMemory.jsx`, `MemoryRetrieval.jsx` | `/api/context-memory`, `/api/memory` | `neo context ...`, `neo memory ...` | `app/services/context_memory/`, `app/services/memory_retrieval/` |
| Recovery | `RecoveryPanel.jsx` | `/api/recovery` | `neo recovery ...` | `app/services/recovery/` |
| Integration and health | no dedicated full screen | `/api/integration`, `/api/health` | `neo integration ...`, `neo health` | `app/services/integration.py`, `app/api/routes/health.py` |

## Validation

Recommended checks before marking a change ready:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall app tests
cd frontend
npm run build
cd ..
git diff --check
```

If a local integrity script exists in your checkout, run it before the regression suite:

```bash
.venv/bin/python scripts/check_repo_integrity.py
```

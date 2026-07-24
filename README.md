# Neo

Neo is a local-first personal AI and software-work platform. It combines persistent
conversation, typed long-term memory, grounded web and live-data answers, project and
knowledge management, guarded coding workflows, agents, and user-configured connectors in
one profile-scoped application.

The production image serves the current React interface and the FastAPI API from the same
origin. Data stays in local SQLite databases and local workspace directories unless a user
explicitly invokes a configured model, search provider, GitHub connection, REST API, or MCP
server.

## What the platform includes

- Persistent chat with resumable background generations, progressive output, editable and
  rerunnable user messages, model-provided thinking traces, and provider/model/token/timing
  metadata. Idempotent submissions, expiring worker leases, fenced writes, and a
  generation-linked assistant row prevent duplicate transcript entries across retries,
  refreshes, and process restarts.
- Typed personal memory for profile facts, education, preferences, goals, projects, events,
  current activities, general memories, provenance, supersession, archiving, and deletion.
  Source replacement is reversible during an edit; deleting the final supporting source or
  explicitly deleting a memory leaves a durable tombstone and is not silently undone by
  later extraction.
- Conservative routing that distinguishes conversation, personal declarations, explicit
  internal commands, live-data requests, web research, and connector calls.
- Structured current weather and daily forecasts through Open-Meteo, currency conversion
  through Frankfurter, and local date/time answers that do not require web search.
- Multi-provider web search with page retrieval, relevance ranking, evidence extraction,
  release-date safeguards, citation validation, fallback providers, and persisted provider
  attempt/evidence audits. Search-result snippets help discovery only; claims and citations
  require successfully fetched page content.
- Notes, tasks, projects, files, artifacts, repositories, code indexes, symbol awareness,
  LSP, patches, sandboxed commands, tests, Git checkpoints, research, agents, recovery,
  evaluation, continuity, and export/import bundles.
- OpenAPI, manual REST, MCP Streamable HTTP, legacy MCP SSE, and trusted stdio connectors;
  profile-bound AES-GCM credential storage; OAuth 2.0 Authorization Code with PKCE and atomic
  token rotation; conservative auto-use of uniquely matched read operations; and explicit
  approval for external or workspace writes. Connector administration requires an active
  profile session.

## Architecture

```text
Browser (React + Vite)              Neo CLI
             \                        /
              \                      /
                 FastAPI application
             account/profile middleware
                         |
       routing, memory, search, agent, and safety services
                         |
       profile SQLite DB + local files + managed repo copies
                         |
       optional LLM, search, live-data, GitHub, REST, and MCP services
```

Every non-guest account has a registry record plus its own storage directory and SQLite
database. Guest profiles are temporary and removed when their session or the application
ends. The original repository registered by a user is not edited directly: code operations
work against a managed copy.

## Quick start

### Local development

Requirements:

- Python 3.12 or newer
- Node.js 22 or a compatible current LTS release
- npm
- optionally Ollama or an OpenAI-compatible model endpoint

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to
`http://127.0.0.1:8000` by default. Set `VITE_API_PROXY_TARGET` only when the
development backend is elsewhere.

### Docker

The multi-stage image compiles the frontend, installs the backend, removes any stale static
bundle, and copies only the newly built interface into the runtime image.

Set `NEO_CONNECTOR_MASTER_KEY` to a stable URL-safe base64 value that decodes to 32 bytes for
this local Docker example. Production deployments should mount the permission-restricted key
file described below instead of exposing the key through process environment.

```bash
docker build -t neo:local .
docker run --name neo \
  -p 8000:8000 \
  -v neo_data:/app/data \
  -e NEO_CONNECTOR_MASTER_KEY="$NEO_CONNECTOR_MASTER_KEY" \
  --add-host=host.docker.internal:host-gateway \
  neo:local
```

Open `http://127.0.0.1:8000`. The default container expects Ollama at
`http://host.docker.internal:11434`, uses `qwen3-coder:30b`, and configures
DuckDuckGo with a Bing HTML fallback. Override any `NEO_*` setting with `-e`.
On Docker Desktop, `host.docker.internal` is normally available without the explicit
`--add-host` option.

Persistent state is under `/app/data`; do not run the container without a volume if the
profile must survive replacement. The image runs as the unprivileged `neo` user.

## Health and readiness

- `GET /api/health/live` is a process-only liveness probe.
- `GET /api/health` reports basic storage, search, and Ollama availability.
- `GET /api/health/ready` performs dependency-aware checks for writable storage and SQLite,
  the selected chat model, the configured search provider, the connector vault, and every
  enabled connector marked required.

The Docker health check uses `/api/health/live`, so a temporarily unavailable model or search
provider does not create a container restart loop. Treat `/api/health/ready` as the
pre-traffic and pre-release gate. A readiness `503` means Neo is alive but a required
dependency is not ready; inspect the returned `checks` object instead of restarting blindly.

## Configuration and secrets

Backend settings use the `NEO_` prefix and may be placed in `.env` for local development.
Provider API keys are read from environment references. Connector credentials and OAuth
tokens are encrypted per profile with AES-GCM and are never returned by read APIs.

For production, provide a stable 32-byte URL-safe base64 connector key through
`NEO_CONNECTOR_MASTER_KEY` or an existing `0600` file referenced by
`NEO_CONNECTOR_MASTER_KEY_FILE`. The production image points that file at
`/app/data/secrets/connector-master-key` and deliberately fails the vault readiness check if
the file does not already exist. Development mode may create a permission-restricted local
key. Preserve the production key with the data backup: ciphertext is bound to both the
profile and connector record and cannot be decrypted with a replacement key.

Do not expose port 8000 directly to an untrusted network. The current profile cookie is
HTTP-only and same-site but is configured for local HTTP, so internet-facing deployments
must add an authenticated TLS reverse proxy and review cookie policy before exposure.

## Validation

Run the project checks before publishing an image:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
cd frontend
npm test
npm run build
```

Then build the Docker image, wait for readiness, exercise chat/memory/search/connectors, and
restart the container to confirm persistence:

```bash
docker build -t neo:local .
curl -fsS http://127.0.0.1:8000/api/health/ready
docker restart neo
curl -fsS http://127.0.0.1:8000/api/health/ready
```

Detailed automated and manual acceptance procedures are included in the backend and frontend
guides.

## Repository map

```text
app/                 FastAPI routes, services, persistence, models, CLI, and TUI
frontend/            React/Vite application and browser API client
docs/backend.md      Backend architecture, database, API, operations, and validation
docs/frontend.md     Frontend architecture, every screen, behavior, and manual testing
tests/               Backend and contract regression tests
Dockerfile           Production frontend/backend image
pyproject.toml       Python package and tooling configuration
```

## Detailed documentation

- [Backend guide](docs/backend.md)
- [Frontend guide](docs/frontend.md)

The `/docs` directory intentionally contains only these two Markdown files.

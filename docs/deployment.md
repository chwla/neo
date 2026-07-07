# Neo deployment

## Local development

Run the API and Vite development server separately:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```

Vite proxies `/api` to the backend. Local development does not require Docker.

## Single-container run

Build and run Neo with one exposed port and one persistent data volume:

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

Open <http://127.0.0.1:8000>. The backend serves both `/api/...` and the built frontend.
SQLite data is stored at `/app/data/neo.db`; uploaded files and managed repository copies are
stored under `/app/data/workspace_files` and `/app/data/workspace_repos`.

## With host Ollama

Ollama and model weights are not bundled in the Neo image. On macOS and Windows with Docker
Desktop:

```bash
docker run --rm \
  -p 8000:8000 \
  -v neo_data:/app/data \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  neo:local
```

On Linux, add the host gateway mapping:

```bash
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -p 8000:8000 \
  -v neo_data:/app/data \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  neo:local
```

## With external SearXNG

SearXNG is optional and is not bundled or started by Neo. Point Neo at an existing instance:

```bash
docker run --rm \
  -p 8000:8000 \
  -v neo_data:/app/data \
  -e NEO_SEARCH_PROVIDER=external_searxng \
  -e NEO_SEARXNG_URL=http://your-searxng:8080 \
  neo:local
```

With `NEO_SEARCH_PROVIDER=disabled` (the default), Neo makes no web-search provider calls and
Chat/Research return clean limited-availability responses when current web evidence is needed.

## Runtime variables

| Variable | Container default | Purpose |
|---|---|---|
| `NEO_HOST` | `0.0.0.0` | API bind address |
| `NEO_PORT` | `8000` | Single exposed HTTP port |
| `NEO_DATA_DIR` | `/app/data` | SQLite and workspace storage root |
| `NEO_SEARCH_PROVIDER` | `disabled` | `disabled` or `external_searxng` |
| `NEO_SEARXNG_URL` | `http://127.0.0.1:8080` | Optional external SearXNG URL |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | External Ollama endpoint |

SearXNG is optional and not required for the default Docker setup.
Neo does not start a SearXNG sidecar.
Ollama/model weights are not bundled.
No Docker Compose file is required for normal operation.

## Controlled Test Runner

The Test Runner executes only saved, explicitly confirmed allowlisted commands inside a
Neo-managed repository copy. Commands are stored as argv arrays and started without a shell;
shell chaining, Git, destructive commands, network fetch tools, and dependency installation are
rejected. Runs are foreground-only, have a 1–600 second timeout, use a minimal environment, and
capture bounded stdout/stderr plus the exit code.

In the default slim Docker image, Test Runner can only execute tools installed in the Neo
container.

Available:

- Python
- Python standard library

Not guaranteed:

- pytest
- Node
- npm
- project-specific dependencies

Neo does not install dependencies at runtime or run package managers. Node and npm build the
frontend in an intermediate image but are not copied into the final Python slim image. For npm or
pytest workflows, use local development mode or build a separate custom image with the required
tools preinstalled.

Test results may be attached to tasks, agent runs, and patch applications. Agent and Chat surfaces
can read stored results but cannot start commands. Applying a patch never starts tests
automatically.

## Controlled Git checkpoints

The standard Neo image includes the Git binary for local checkpointing inside Neo-managed
repository copies. Git is installed at image build time only. Neo exposes fixed operations for
initialization, status, diff, local checkpoint commits, history, and explicitly confirmed restore.
There is no command box or general terminal.

Remote operations are not supported: Neo has no clone, fetch, pull, push, remote, submodule, or
credential endpoints. Host Git configuration and credentials are not mounted or passed into the
container. Each managed repository uses a local `Neo <neo@local>` identity, a minimal environment,
and a private local `.git` directory. Patch application, tests, Agent, and Chat never commit or
restore automatically.

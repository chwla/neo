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
  -p 8000:8000 \
  -v neo_data:/app/data \
  -e NEO_SEARCH_PROVIDER=disabled \
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

No Docker Compose file or SearXNG sidecar is required for normal operation.

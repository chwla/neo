# Setting up Neo

This guide takes you from a fresh checkout to a running Neo. Pick one of two paths:

- **[Docker](#option-a-docker)**: the production shape. One container serves the API and the
  built interface on port 8000. Best for just using Neo.
- **[Local development](#option-b-local-development)**: a Python virtualenv plus the Vite dev
  server. Best for changing Neo's code.

Both need a language model. Neo does not ship one; see [Choose a model](#choose-a-model).

---

## Prerequisites

| Need | For | Notes |
|---|---|---|
| Docker + Docker Compose | Option A | Docker Desktop on macOS and Windows. |
| Python 3.12+ | Option B | `python3 --version` |
| Node.js 22 (or current LTS) + npm | Option B | `node --version` |
| A model endpoint | Both | [Ollama](https://ollama.com) (default) or any OpenAI-compatible endpoint. |

Platform support: macOS, Linux and Windows all run the same code. The one platform-specific
feature is the sandbox for external coding CLIs (Claude Code, Codex, ...), which only runs on a
validated macOS install; everywhere else those CLIs refuse to run. Everything else works the
same.

---

## Choose a model

Neo talks to a model over HTTP. Two options:

**Ollama (default).** Install Ollama, start it, and pull a chat model:

```bash
ollama pull llama3.2:3b          # small default for local runs
# or, for the Docker defaults:
ollama pull qwen3-coder:30b
```

Optional extras Neo will use when present:

| Model | Used for | Setting |
|---|---|---|
| `nomic-embed-text:latest` | Semantic memory recall and gallery search | `NEO_MEMORY_EMBEDDING_MODEL`, `NEO_EMBEDDING_MODEL` |
| `qwen2.5vl:7b` | Describing gallery images | `NEO_VISION_MODEL` |

Without an embedding model, recall falls back to keyword search; chat still works.

**OpenAI-compatible endpoint.** Set `NEO_OPENAI_COMPAT_BASE_URL`, `NEO_OPENAI_COMPAT_MODEL`,
and put the key in the environment variable named by `NEO_OPENAI_COMPAT_API_KEY_REF`
(default `OPENAI_API_KEY`). Providers and models can also be added in the app under
the model registry settings; those are stored per profile in `neo_llms.json`.

---

## Option A: Docker

```bash
git clone <your-repo-url> neo
cd neo
docker compose up -d --build
```

Open <http://127.0.0.1:8000>. On first launch you create a profile (see
[First run](#first-run)).

What this does:

- Builds the React interface, installs the backend, bakes in a pinned SearXNG, and serves
  everything from one origin on port 8000.
- Mounts the named volume `neo-data` at `/app/data`. **All profiles, chats, memories and keys live
  there**; do not run without a volume if you want data to survive replacing the container.
- Mounts your home directory at `/workspace` so Agent Mode can open projects where they
  already live.

> **Note:** `docker-compose.yml` declares `neo-data` as an `external` volume with a fixed name
> from the original author's machine. On a new machine, either create a volume with that exact
> name or edit the `volumes:` block at the bottom of the file to a plain `neo-data:` entry
> (remove `external` and `name`) so Compose creates it for you.

### Configure with `.env`

Copy the template next to `docker-compose.yml`; Compose reads it automatically.

```bash
cp .env.example .env
```

| Variable | When you need it |
|---|---|
| `NEO_WORKSPACE_HOST_ROOT` | To narrow what Agent Mode can reach. Defaults to your home directory. macOS `/Users/you`, Linux/WSL2 `/home/you`, Windows `C:\Users\you`. |
| `NEO_UID` | **Linux only.** Set to `$(id -u)` so the container can write your files. Not needed on macOS/Windows. |
| `NEO_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | To connect a Google account. See [Google](#connecting-a-google-account). |
| `NEO_WITH_VOICE=1` | Set in the shell when building, to include speech-to-text (adds a few hundred MB). |

Folders are attached by their path **inside** the container: a project at `~/code/my-app` is
`/workspace/code/my-app`. The folder picker in the app browses for you.

### Reaching Ollama from the container

The image expects Ollama at `http://host.docker.internal:11434`. Docker Desktop provides that
name. On Linux, add `--add-host=host.docker.internal:host-gateway` (with `docker run`) or an
`extra_hosts` entry (with Compose), or set `OLLAMA_BASE_URL` to an address the container can
reach.

### Plain `docker run`

```bash
docker build -t neo:local .
docker run --name neo -p 8000:8000 -v neo_data:/app/data \
  --add-host=host.docker.internal:host-gateway neo:local
```

### Production secrets

Connector credentials and OAuth tokens are encrypted with a master key. In the image the key
is read from `/app/data/secrets/connector-master-key` (via `NEO_CONNECTOR_MASTER_KEY_FILE`),
and the readiness check **fails if that file does not exist**. Create it once:

```bash
docker exec neo sh -c 'mkdir -p /app/data/secrets && \
  python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
  > /app/data/secrets/connector-master-key && chmod 600 /app/data/secrets/connector-master-key'
```

For a throwaway local container you can instead pass `-e NEO_CONNECTOR_MASTER_KEY=<32-byte
urlsafe base64>`. **Back this key up with your data**: ciphertext is bound to the profile and
cannot be decrypted with a replacement key.

---

## Option B: Local development

### 1. Backend

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"      # Windows: .venv\Scripts\python
# optional speech-to-text:
.venv/bin/python -m pip install -e ".[voice]"
```

Create a `.env` in the repo root (or export variables) with at least a search provider if you
want web search; local runs default to search **disabled**:

```bash
NEO_SEARCH_PROVIDER=duckduckgo
NEO_DEFAULT_MODEL=llama3.2:3b
```

Start the API:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

There is **no auto-reload**; restart after changing backend code. Confirm it is up:

```bash
curl http://127.0.0.1:8000/api/health/live
```

By default, data is created next to the checkout (`neo_memory.db`, `profiles/`, `data/`, all
git-ignored). Set `NEO_DATA_DIR=/some/path` to keep everything in one folder instead.

### 2. Frontend

In a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open <http://127.0.0.1:5173>. Vite proxies `/api` to `http://127.0.0.1:8000`; if your backend is
elsewhere, set `VITE_API_PROXY_TARGET`.

### 3. Serving the built UI from the backend (optional)

```bash
cd frontend && npm run build
```

Point `NEO_FRONTEND_DIR` at `frontend/dist` (default is `app/static`) and the backend serves the
interface itself on port 8000, exactly as the Docker image does.

### 4. Checks

```bash
.venv/bin/ruff check app
cd frontend && npm test && npm run build
```

---

## First run

1. Open Neo in a browser. You land on the **profile picker**.
2. **Create a profile** (username + password) or **Continue as guest**. A guest profile is
   temporary and deleted when its session or the app ends.
3. Each profile has its own database, files and memory. Nothing is shared between profiles.
4. Open the chat and send a message. If you see a model error, check
   [Troubleshooting](#troubleshooting).

---

## Optional features

### Web search

Set `NEO_SEARCH_PROVIDER` to one of:

| Value | Needs |
|---|---|
| `searxng` | Built in on Docker. A local install needs a SearXNG source tree at `NEO_SEARXNG_SOURCE_DIR` (default `data/searxng`); without it the provider reports unavailable. |
| `external_searxng` | `NEO_SEARXNG_URL` pointing at your own instance. |
| `duckduckgo`, `bing_html` | Nothing (keyless; can be rate-limited or blocked on some networks). |
| `tavily`, `brave`, `serper` | An API key: `NEO_TAVILY_API_KEY`, `NEO_BRAVE_API_KEY`, `NEO_SERPER_API_KEY`. |
| `disabled` | Default on local runs. |

`NEO_WEB_SEARCH_FALLBACK_PROVIDERS` is a comma-separated list tried after the primary fails.
A keyed provider is the most reliable choice on networks that block the keyless ones.

### Connecting a Google account

Neo is installed, not hosted, so the OAuth client is yours:

1. In Google Cloud Console, create a project and enable the APIs you want (Calendar, Docs,
   Drive, Gmail).
2. Create an OAuth client of type **Desktop app** and register this redirect URI exactly:
   `http://127.0.0.1:8000/api/integrations/oauth/callback`
3. Put the values in `.env` as `NEO_GOOGLE_OAUTH_CLIENT_ID` and `NEO_GOOGLE_OAUTH_CLIENT_SECRET`.
4. In Neo: Settings > Capabilities > Connected accounts.

### Voice input

Install the `voice` extra (local) or build with `NEO_WITH_VOICE=1` (Docker), then download a
speech model in Settings > Voice input. Without the extra, the microphone reports itself
unavailable and nothing else changes.

### External coding CLIs

Claude Code, Codex and similar CLIs can run as Agent Mode executors, **only on a validated macOS
install**. Set `NEO_EXTERNAL_AGENTS_ENABLED=true` and, if needed, `NEO_CLAUDE_CODE_BIN` /
`NEO_CODEX_BIN`. Set them up from the External agents panel.

### Command line

Installing the package adds a `neo` command that talks to a running server:

```bash
.venv/bin/neo status
.venv/bin/neo health
.venv/bin/neo tui
```

Use `--api-url` or `NEO_API_URL` to point it elsewhere (default `http://127.0.0.1:8000`).

---

## Health and readiness

| Endpoint | Meaning |
|---|---|
| `GET /api/health/live` | Process is up. Used by the Docker healthcheck. |
| `GET /api/health` | Basic storage, search and Ollama availability. |
| `GET /api/health/ready` | Dependency-aware: storage/SQLite, chat model, search provider, connector vault, required connectors. A `503` means alive but not ready; read the `checks` object. |

---

## Backups and upgrades

- **What to back up:** the data directory (Docker: the `neo-data` volume at `/app/data`; local:
  `NEO_DATA_DIR`, or `profiles/` + `neo_memory.db` + `data/`) **and the connector master key**.
- **Upgrading:** rebuild and restart (`docker compose up -d --build`, or `git pull` + restart the
  backend + `npm ci`). Schema changes for new features are applied automatically at startup per
  profile.

## Security notes

- Port 8000 has no TLS. Do not expose it to an untrusted network. If you must, put an
  authenticated TLS reverse proxy in front, add its hostname to `NEO_TRUSTED_HOSTS`, and review
  the cookie settings.
- Requests with an unrecognised `Host` header are refused (DNS-rebinding defence); loopback
  names are always accepted.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "Backend API is not reachable" in the UI | The backend is not running on :8000, or `VITE_API_PROXY_TARGET` is wrong. |
| Chat returns a model error | Ollama is not running, or the model is not pulled. `ollama list`; check `NEO_DEFAULT_MODEL` and `OLLAMA_BASE_URL`. |
| `/api/health/ready` returns 503 for the vault | The master key file does not exist; see [Production secrets](#production-secrets). |
| Docker: `external volume not found` | See the note under [Option A](#option-a-docker). |
| Linux: "Permission denied" writing to a project | Set `NEO_UID=$(id -u)` in `.env` and recreate the container. |
| Web search returns nothing | Provider disabled, blocked, or rate-limited. Try a keyed provider or fallbacks; see `/api/health`. |
| `untrusted_host` (400) | Request used a hostname not in loopback or `NEO_TRUSTED_HOSTS`. |
| Changes to Python code do nothing | No auto-reload; restart uvicorn. Also check you are not hitting a Docker Neo on the same port 8000. |

Next: [Backend guide](backend.md) and [Frontend guide](frontend.md).

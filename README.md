# Neo

Neo is a local-first personal AI and software-work platform. It combines persistent
conversation, typed long-term memory, grounded web and live-data answers, project and
knowledge management, guarded coding workflows, agents, and user-configured connectors in
one profile-scoped application.

The production image serves the current React interface and the FastAPI API from the same
origin. Data stays in local SQLite databases and local workspace directories unless a user
explicitly invokes a configured model, search provider, GitHub connection, or connected
Google account.

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
  LSP, patches, guarded commands, tests, Git checkpoints, research, agents, recovery,
  evaluation, continuity, and export/import bundles.
- Connected Google accounts (Calendar, Docs, Drive, Gmail) through OAuth 2.0 Authorization
  Code with PKCE and atomic token rotation, with tokens sealed per profile with AES-GCM and
  never returned by read APIs. External or workspace writes need explicit approval, and
  connecting an account requires a signed-in profile; guest sessions cannot.

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
       optional LLM, search, live-data, GitHub, and Google services
```

Every non-guest account has a registry record plus its own storage directory and SQLite
database. Guest profiles are temporary and removed when their session or the application
ends.

A folder can be attached in either of two ways. **Live** is the default and is what Agent
Mode is built around: Neo indexes the folder in place and the agent reads and edits those
files directly, the way a coding CLI does, with no copy and no delivery step. Every file a
run touches is journalled first, so the whole run can be undone. **Managed** is the
opt-in alternative, and keeps the older guarantee that the user's own files are never
written: Neo copies the supported text files, the agent edits the copy, and the user
applies the result themselves.

External coding CLIs have a separate boundary. On a validated macOS installation,
Neo launches them with a per-run Seatbelt policy and gives them the current task,
their scoped workspace, and any earlier message or note the user explicitly
previews and approves. A private disclosure runs in a separate full copy and its
changes require exact-diff review before delivery. External CLIs refuse to run on
Linux, Windows and Docker, or when macOS isolation and CLI validation are missing.

Neo's own commands and test runs are sandboxed only where a sandbox exists (macOS).
Everywhere else -- Windows, Linux, Docker, or a Mac whose sandbox check fails -- a
command or test suite would run with your whole user account, so Neo never runs one
unattended there: every launch needs your approval of that exact command, after Neo
has said what running it unsandboxed exposes. AUTO mode asks too. The rule is enforced
at the moment each process starts (`app/services/isolation/launch.py`), not only in
the interface. On Windows, Neo never starts a `.cmd` or `.bat` file (which `cmd.exe`
would re-parse): `npm` and `npx` run as `node` with their own script, and any other
batch shim is refused.

File edits are the same on every platform. A write goes to a temporary file beside the
target and is renamed over it, so a write that is refused -- the file open in another
program, read-only, the disk full -- leaves the original bytes in place. A file that is
not UTF-8 is edited in its own encoding, or the edit is refused; it is never rewritten
with replacement characters. Undo, patch apply, delivery and checkpoints use anchored
writes that cannot be redirected out of the folder by a symlink or junction, on macOS,
Linux and Windows alike.

### Reaching your folder

Neo runs on macOS, Linux and Windows from one code path. What differs is only how the
server process comes to see your files.

Run directly on the host and it already does: any absolute path works, and
`NEO_WORKSPACE_LIVE_ROOTS` can stay unset.

In a container it can only reach what is mounted. `docker-compose.yml` bind-mounts one
host directory at `/workspace` and sets `NEO_WORKSPACE_LIVE_ROOTS=/workspace`; folders are
then attached by their path **inside** the container (`/workspace/code/my-app`), not the
host path it came from. **Your home directory is the default**, so projects open where
they already live. Copy `.env.example` to `.env` only if you want to narrow it:

| Host | `NEO_WORKSPACE_HOST_ROOT` | Also needed |
|---|---|---|
| macOS | `/Users/you` | - |
| Linux | `/home/you` | `NEO_UID=$(id -u)`: see below |
| Windows (Docker Desktop) | `C:\Users\you` | - |
| WSL2 | `/home/you` | - |

Open a folder is a browser, not a text field: it lists what is under the root and you
navigate to the project you want. Everything on disk is listed, hidden folders included,
because `.dotfiles`, `.vscode` and a repository's `.git` are ordinary folders to a coding
agent.

The selected workspace scopes Neo's internal file tools. Their permission resolver in
`app/services/agent_core/permissions.py` governs reads, writes and commands; the
external CLI boundary also needs its operating-system policy and validation. The picker's rules
(`app/services/repos/safety.py`) decide which folders may be opened at all, for live and
managed attachment alike:

- `/workspace` itself is **browse-only**: a root holds projects, it is not one. When roots
  are configured, every attached folder must live under one of them.
- Folders that hold credentials or other apps' private data are refused, whether you pick
  the folder itself, something inside it, or a folder that contains it: `.ssh`, `.aws`,
  `.config/gcloud`, the coding CLIs' own homes (`.claude`, `.codex`, `.cursor`, `.gemini`),
  shell histories, the macOS keychains and browser profiles (which also rules out
  `~/Library` as a whole). The list is checked under your home directory and under each
  configured root, so it holds inside a container too.
- Neo's own data directory is refused, including any folder that contains it: it holds
  every profile's database and the connector key.
- On Windows, `%APPDATA%`, `%LOCALAPPDATA%` and `%PROGRAMDATA%` are refused whole (browser
  profiles, saved credentials and the DPAPI keys all live there), as is anything under
  `AppData`; on Linux, `~/.config`, `~/.gnupg`, `~/.docker`, `~/.kube` and the keyrings.
  These are decided from the path alone, before Neo looks inside the folder.
- Symlinks and Windows junctions are refused, `..` cannot escape the root, and the system
  trees (`/etc`, `/usr`, `/System`, …), the account containers (`/Users`, `/home`) and
  `$HOME` itself stay refused as they always were. Neo's own file tools (`glob`, `grep`,
  `list_dir`) never follow a link or junction out of the folder either.

Other broad folders such as `Desktop` and `Documents` are not refused by name. Attaching
one indexes everything in it, so pick the project folder inside instead.

**Linux uid.** A bind mount keeps the host's ownership, so a container running as another
user can read every file and write none of them. Set `NEO_UID` to your own `id -u`; the
image keeps `/app/data` group-owned by 0 and group-writable so that any uid still works.
macOS and Windows need nothing: Docker Desktop maps ownership for you. A write that fails
this way is reported with this cause named, not as a bare "Permission denied".

**Windows.** Attach folders by their container path. Keep projects on the Linux side of
WSL2 where you can: a `C:\` or `\\wsl$` mount crosses a filesystem bridge and is
markedly slower. Native Windows paths, drive letters, UNC shares and junctions are all
handled; separate multiple roots with `;` when configuring a native Windows Neo, and with
`:` inside the Linux container.

Line endings are preserved: editing one line of a CRLF file leaves the rest of the file
untouched, on every platform.

## Quick start

### Local development

Requirements:

- Python 3.12 or newer
- Node.js 20 or newer (22 is what CI builds with)
- npm
- optionally Ollama or an OpenAI-compatible model endpoint

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Windows (PowerShell) the same steps are:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install --no-deps -e ".[dev]"
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`requirements.txt` holds the exact versions Neo is tested with; `pyproject.toml` carries only
lower bounds, and installing from it alone resolves to a different, untested set. Stop the
server with Ctrl+C: it exits within a few seconds even while a browser tab holds a stream open.

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

The connector master key is generated on first run inside the data volume, beside the data
it protects (see [Connector master key](#connector-master-key)); nothing has to be supplied.

```bash
docker build -t neo:local .
docker run --name neo \
  -p 127.0.0.1:8000:8000 \
  -v neo_data:/app/data \
  --add-host=host.docker.internal:host-gateway \
  neo:local
```

Open `http://127.0.0.1:8000`. The default container expects Ollama at
`http://host.docker.internal:11434`, uses `qwen3-coder:30b`, and searches with
the built-in SearXNG backed by DuckDuckGo and Bing HTML fallbacks. Override any
`NEO_*` setting with `-e`.

SearXNG runs inside Neo's own process rather than as a second container:
`searx.webapp` is a Flask WSGI app, so the image bakes in its source tree at a
pinned commit and `app/services/search/searxng_embedded.py` imports and calls it
directly. Nothing binds an extra port, and `docker-compose.yml` maps
`host.docker.internal` to the host the same way `--add-host` does above, so `docker run`
and `docker compose up` are equivalent. To use a SearXNG you run yourself, set
`NEO_SEARCH_PROVIDER=external_searxng` and `NEO_SEARXNG_URL` to its address.
On Docker Desktop, `host.docker.internal` is normally available without the explicit
`--add-host` option.

Persistent state is under `/app/data`; do not run the container without a volume if the
profile must survive replacement. The image runs as the unprivileged `neo` user.

## Health and readiness

- `GET /api/health/live` is a process-only liveness probe.
- `GET /api/health` reports basic storage, search, and Ollama availability.
- `GET /api/health/ready` answers anonymous callers with only `{"status": ...}`, from a
  cheap check of writable storage and SQLite: these routes need no session, and any web page
  can make a browser request them, so nothing an anonymous caller triggers may start a web
  search or a model call or reveal a path. A signed-in caller gets the full report -- storage
  (with its path), the selected chat model, the configured search provider, voice and the
  connector key -- computed at most once every 30 seconds.

The Docker health check uses `/api/health/live`, so a temporarily unavailable model or search
provider does not create a container restart loop. Treat `/api/health/ready` as the
pre-traffic and pre-release gate. A readiness `503` means Neo is alive but a required
dependency is not ready; inspect the returned `checks` object instead of restarting blindly.

## Configuration and secrets

Backend settings use the `NEO_` prefix and may be placed in `.env` for local development.
Provider API keys are read from environment references. Connector credentials and OAuth
tokens are encrypted per profile with AES-GCM and are never returned by read APIs.

Model configurations may name an environment variable for their key (`api_key_env`). A key
from the server's environment is only ever sent over `https` to its own provider's host
(`OPENAI_API_KEY` to `api.openai.com`, and so on), never to an IP address, never after a
redirect, and never for a guest session. Neo's own search keys cannot be named at all.
`NEO_PROVIDER_KEY_HOSTS=MY_API_KEY=llm.example.com` allows a self-hosted endpoint. This
stops a profile from sending an operator's key to a server of its choosing; it does not
stop any non-guest profile from *spending* that key at its provider, and any process
running as the same OS user can read the server's environment directly.

### Connector master key

Connected Google accounts, bring-your-own OAuth client secrets and GitHub tokens are sealed
under one machine-level key. It is generated on first run at
`NEO_CONNECTOR_MASTER_KEY_FILE` (in the image, `/app/data/secrets/connector-master-key`;
otherwise `.neo-connector-master-key` beside the profiles), readable only by your account
(mode `0600`; on Windows an access list restricted to you). `NEO_CONNECTOR_MASTER_KEY`
supplies one inline instead. **Back it up with your data**: without it those records cannot
be read.

What Neo does in each case:

- **First run**: generates the key, but only if no profile holds sealed records.
- **Key file missing while sealed records exist**: Neo refuses to mint a new key and says so,
  naming the missing key's fingerprint. Restore the file from a backup and restart.
- **Unreadable or damaged**: an error naming the fix; the file is never replaced.
- **Replaced by an operator**: accepted; accounts sealed under the old key show
  "reconnect required" instead of failing.
- **Rotation** (re-sealing everything under a new key) is not supported in this release.

**Resetting the key is destructive.** If no backup exists, start once with
`NEO_CONNECTOR_KEY_RESET=<fingerprint>`, copied from the refusal message (the value `1`, or
any other value, does nothing). That generates a new key and permanently destroys every
sealed record on every profile: each connected account must be reconnected and each OAuth
client secret and GitHub token entered again. Passwords, chats, notes (locked notes included),
memories and files are not affected. The reset is recorded, and because the fingerprint it
names is then gone, leaving the variable set can never trigger a second one.

Do not expose port 8000 directly to an untrusted network. The `docker run` example above and
`docker-compose.yml` publish it on `127.0.0.1` only, and that is deliberate: anyone who can reach the port can start a guest
session or register a profile, and a session can open folders under the workspace root and
run allowlisted commands there (unsandboxed outside macOS, each only after explicit
approval). Profile cookies are HTTP-only and same-site; behind an HTTPS reverse proxy set
`NEO_COOKIE_SECURE=true` so they are also marked `Secure`. Internet-facing deployments must
add an authenticated TLS reverse proxy before exposure.

## Supported platforms and browsers

One code path runs on macOS, Linux and Windows. The table says exactly what differs; a
row that says "supported" has that capability working the same way on every platform.

| Capability | macOS | Linux | Windows | Docker |
|---|---|---|---|---|
| Install, start, serve, chat, memory, notes, search, agent file tools | supported | supported | supported | supported |
| Agent undo, patch apply, delivery, Git init and checkpoints (anchored writes) | supported | supported | supported | supported |
| AUTO mode edits files unattended (managed copy and live folder) | supported | supported | supported | supported |
| Commands and test runs sandboxed | Seatbelt | **no**: each run needs your approval | **no**: each run needs your approval | **no**: each run needs your approval |
| External coding CLIs (Claude Code, Codex, …) | supported | **refused** (no validated sandbox) | **refused** (no validated sandbox) | **refused** |
| External CLI sign-in through a terminal | supported | supported | **no** (no pseudo-terminal) | n/a |
| Browsers | Chrome, Edge 111+ / Firefox 121+ / Safari 16.2+ (iOS 16.2+): the floor in `browserslist` (`frontend/package.json`) |

A Linux or Windows sandbox for commands, tests and external CLIs is not in this release; the
per-run approval, with the exposure spelled out on the card, is the rule until it is.

Every push runs `.github/workflows/ci.yml`: lint, the workspace-containment and data-safety
checks, the agent-loop and CLI checks, and a startup smoke on macOS, Linux and Windows
(Python 3.12, 3.13 and 3.14), the frontend production build on all three, and the Docker
image smoke test. The scripts are in `.github/scripts/`.

## Validation

Run the project checks before publishing an image:

```bash
.venv/bin/ruff check app
cd frontend
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

## Repository map

```text
app/                 FastAPI routes, services, persistence, models, CLI, and TUI
frontend/            React/Vite application and browser API client
.github/workflows/   CI: lint, safety checks and startup on macOS/Linux/Windows, frontend build, Docker smoke
.github/scripts/     The checks CI runs: agent tools, workspace safety, CLI and platform behaviour, smoke
docs/                Setup, backend and frontend guides
docker/searxng/      Settings for the SearXNG that runs inside Neo's process
Dockerfile           Production frontend/backend image
pyproject.toml       Python package and tooling configuration
```

## Documentation

- [Setup guide](docs/setup.md): install, configure and run Neo
- [Backend guide](docs/backend.md): architecture, storage, security, every subsystem
- [Frontend guide](docs/frontend.md): structure, state conventions, testing, benchmarks

## License

Neo is licensed under the [Apache License 2.0](LICENSE). Copyright 2026 Soham Chawla.

This license applies to the entire codebase, including every version and commit of Neo
published in this repository before this notice was added.

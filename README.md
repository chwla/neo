Neo 
==========

LLM provider and model registry
-------------------------------

Neo stores providers, models, role routes, fallbacks, and usage history in the workspace database.
Native Ollama and OpenAI-compatible chat-completions APIs are supported. Chat, Research, Agent,
Coding Agent, Patch Proposal, and embedding entry points resolve through named routes. A retryable
primary failure uses only an explicitly configured fallback, and both the failure and fallback are
visible in `/api/llm/usage` and Settings → LLM Providers.

API keys are never accepted or returned as provider data. Store only an environment-variable name
such as `OPENAI_API_KEY` in `api_key_ref`; Neo resolves the value at call time and redacts it from
stored errors. Existing `neo_llms.json` entries migrate without copying plaintext keys and remain
available through the compatibility API.

Useful environment defaults:

```env
NEO_LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
NEO_DEFAULT_MODEL=llama3.2:3b
NEO_OPENAI_COMPAT_BASE_URL=
NEO_OPENAI_COMPAT_API_KEY_REF=OPENAI_API_KEY
NEO_OPENAI_COMPAT_MODEL=
```

Run everything:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-dev.ps1
```

Keep that terminal open; it runs the backend server.

Run the API:

```powershell
& "C:\Program Files\Python313\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run the React/Tailwind frontend:

```powershell
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` requests to `http://127.0.0.1:8000`.

For the single-container deployment, see [docs/deployment.md](docs/deployment.md).

SearXNG is optional and not required for the default Docker setup. Neo does not start a
SearXNG sidecar, and Ollama/model weights are not bundled.

## Controlled Test Runner

Registered repositories expose a Test Runner for saved, explicitly confirmed test commands. It
runs strict argv allowlists only in Neo's managed repository copy, without a shell, Git, package
installation, background jobs, or automatic execution after patch apply. Output, exit code,
duration, and associations are stored for later read-only Agent/Chat context. See
[docs/deployment.md](docs/deployment.md#controlled-test-runner) for runtime tool limitations.

## Controlled multi-file patches

Patch proposals can modify multiple registered text/code files and create new safe text/code files
inside one managed repository. Neo validates every path, hash, hunk, and metadata entry before an
explicit approval, then applies the whole patch atomically. A failure restores modified files and
removes created files. Delete, rename, binary, symlink, permission, hidden/secret, dependency,
build/cache, and `.git` patches remain unsupported. Applying a patch never runs tests or creates a
checkpoint automatically, and the original repository is never written.

## Controlled Git checkpoints

Neo can initialize local Git tracking inside a registered managed repository copy, show status and
diffs, create explicitly confirmed checkpoints, and restore managed files from a checkpoint. The
original repository is never modified. Remote Git operations, arbitrary Git commands, automatic
commits, and automatic restores are not available. See
[docs/deployment.md](docs/deployment.md#controlled-git-checkpoints).

## Multi-Step Coding Agent Loop

Agent Mode and Task detail can orchestrate an objective through bounded Codebase Index and Symbol
Awareness context, a review-only patch proposal, Controlled Patch Apply, a saved Controlled Test
Runner command, and a local Git checkpoint. Patch application, test execution, and checkpoint
creation are separate persisted approval requests. Neo never auto-approves them, never edits the
original repository, and never writes coding-run state to Memory automatically.

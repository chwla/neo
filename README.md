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

## Validation guardrail

Before marking a change ready, run the repo/test integrity guard and then the normal regression
suite:

```bash
.venv/bin/python scripts/check_repo_integrity.py
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall app tests scripts
cd frontend && npm run build
cd ..
git diff --check
git diff --cached --check
```

The guard fails loudly if the real `tests/` suite disappears, pytest collects suspiciously few
tests, test sources are deleted or unexpectedly untracked, cache/bytecode is staged or tracked,
placeholder-like tests replace real coverage, or critical source directories are missing.

## Reliable Web Search

Settings → Reliable Web Search provides bounded research planning, persistent source/evidence
history, citations, conflict flags, and a safe source cache. It never bypasses paywalls or CAPTCHAs,
stores credentials, runs code, or performs unbounded crawling. With a disabled provider it records
a clear degraded audit run instead of generating unsupported claims.

## CLI / headless runner

Neo includes a small headless CLI for scripted checks and operator workflows against a running Neo
API:

```bash
python -m app.cli status --api-url http://127.0.0.1:8000
python -m app.cli agents list
python -m app.cli coding start "Investigate failing tests" --repo repo-id --agent coder
python -m app.cli coding actions run-id
python -m app.cli agentic start --type coding --objective "Investigate failing tests"
python -m app.cli agentic show run-id
python -m app.cli agentic steps run-id
python -m app.cli agentic context run-id
python -m app.cli recovery list
python -m app.cli rules resolve --repo repo-id --context coding_agent
python -m app.cli tools list
python -m app.cli skills list
python -m app.cli tests list --repo repo-id
python -m app.cli git status repo-id
python -m app.cli export run run-id --out run.neo.zip
python -m app.cli bundles list
python -m app.cli bundles import validate run.neo.zip
python -m app.cli bundles import run.neo.zip --yes
```

Configuration is intentionally boring: set `NEO_API_URL` for the default server URL and
`NEO_CLI_OUTPUT=json` or pass `--json` for machine-readable output. `--timeout` adjusts API request
timeouts, and `--no-color` is accepted for non-interactive environments.

Commands that approve agent actions, run saved tests, resume/retry/fork recovery runs, or create
checkpoints ask for confirmation unless `--yes` is passed. `--yes` only skips the local prompt; the
CLI still sends the existing backend `confirm=true` field and does not bypass Neo's approval gates.

Exit codes are:

- `0`: success.
- `1`: invalid input, failed confirmation setup, or local export write failure.
- `2`: Neo API unavailable.
- `3`: Neo API returned an error response.
- `4`: operator confirmation denied.

## Session bundles

Neo can package a coding run, agent run, task, or project into a portable `.neo.zip` evidence
bundle. Each archive contains `neo_bundle.json`, optional patch/test-report artifacts, and a
`checksums.json` manifest. Credential values, environment values, provider credentials, and
original absolute host paths are replaced with `[REDACTED]` before an archive is written.

Bundles can be exported from a Coding Run detail or Settings → Bundles, downloaded, validated,
and imported elsewhere. Import currently supports `archive_only` exclusively: it preserves the
archive and its read-only metadata for inspection without merging active runs, applying patches,
running tests, creating checkpoints, or writing any original repository.

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

## Agentic Core

Agentic Core persists a shared `PLAN → INSPECT → ACT → VERIFY → REFLECT → CONTINUE`
state machine for coding, research, and task workflows. Each run records an editable plan,
completion criteria, bounded context budget, explicit tool decisions, verification evidence,
reflection, failures, recovery attempts, blockers, and a grounded final report. Settings →
Agentic Runs exposes the run list, plan, timeline, context, controls, and step detail; Coding Agent
detail embeds its linked agentic state.

The core orchestrates existing Neo services instead of bypassing them. Patch apply, Command
Sandbox execution, saved tests, local checkpoints, tool mutations, and external writes retain
their existing explicit approval gates. A blocked or unavailable action is persisted as a blocker;
it is never silently treated as success.

## Memory Retrieval

Memory Retrieval extends Context Memory with redacted SQLite metadata, FTS keyword matching,
structured filters, scope/importance/recency scoring, retrieval audits, and safe pruning previews.
Use `neo memory retrieve "query" --scope project:<id>` or open **Settings → Memory Retrieval**.
Retrieved memories supplement current instructions and never execute, edit, test, checkpoint, or
apply Git actions. `neo memory prune-apply --yes` is the only deletion command and protects user
instructions and safety notes.

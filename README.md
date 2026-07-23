# Neo

Neo is a local-first AI workbench for research, planning, software delivery, validation, and long-running work continuity. It combines a React web application, a FastAPI service, a command-line interface, and local storage to help people organize work while keeping consequential actions visible and approval-controlled.

## What Neo provides

- Project, task, note, chat, file, artifact, and durable-memory management.
- Research and web-search workflows that preserve sources, evidence, claims, and conflicts.
- Model and provider management with health checks, routing, usage, budgets, retries, streaming, and secret redaction.
- Managed repository copies with code indexing, symbol awareness, LSP integration, patch proposals, controlled commands, tests, and local Git checkpoints.
- Agent, coding-agent, evaluation, workspace-orchestration, recovery, bundle, continuity, tools, skills, rules, and GitHub-integration workflows.

The guiding principle is **visibility before automation**. Neo records state and uses explicit confirmation for actions that can change a managed workspace or invoke external capabilities. It does not directly alter an original repository.

## Architecture at a glance

```text
React + Vite web application / CLI + TUI
              |
         FastAPI API
              |
  service, safety, and orchestration layers
              |
SQLite profile data + local files + managed repository copies
              |
optional providers: Ollama, OpenAI-compatible APIs, search services, GitHub
```

The backend can also serve the compiled frontend, making a single-container deployment possible.

## Run locally

Requirements: Python 3.12+, Node.js/npm for frontend development, and optionally Docker. Model-backed features need an available provider such as Ollama or an OpenAI-compatible endpoint. Web search is optional.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The Vite development server sends `/api` requests to the backend. For a containerized deployment, build and run the included `Dockerfile`; it serves Neo from port `8000` and persists application state in `/app/data`.

## Documentation

The detailed technical documentation is intentionally limited to two guides:

- [Frontend guide](docs/frontend.md) — application structure, UI conventions, API client, development, build, and extension guidance.
- [Backend guide](docs/backend.md) — API architecture, services, data and profiles, configuration, CLI, operations, and safety model.
- [Manual production test plan](docs/manual-production-test-plan.md) — release-gate scenarios for every user-facing workflow, integrations, and safety boundary.

## Repository map

```text
app/                 Python backend, API routes, services, models, and CLI/TUI
frontend/            React/Vite/Tailwind web application
docs/                Detailed frontend and backend documentation
tests/               Backend tests
Dockerfile           Production container build
pyproject.toml       Python package and developer tooling configuration
```

## License and contributions

Before publishing or accepting contributions, add the project’s chosen license and contribution policy. Keep implementation details in the two guides above so this README remains a focused platform overview.

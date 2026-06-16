# Neo Memory

Neo Memory is the local-first long-term memory layer for Neo, a personal AI
assistant. This repository is being implemented in phases so each subsystem can
be reviewed and tested independently.

## Implementation Phases

1. Database schema and SQLAlchemy models
2. Memory extraction pipeline
3. Retrieval engine
4. Context assembler
5. Qdrant archive integration
6. Reflection engine
7. Conflict resolution
8. FastAPI endpoints

## Current Status

All initial phases are implemented:

- SQLite-first SQLAlchemy models and Alembic migration
- Memory extraction into pending candidates
- Review workflow for accepting, rejecting, and merging candidates
- Structured retrieval for profile, preferences, goals, projects, events, and memories
- Context assembly package for Neo prompts
- Qdrant archive adapter for conversations, documents, and notes
- Reflection service that stores higher-level observations
- Conflict handling for replaced profile facts, preferences, and durable memories
- FastAPI endpoints for ingestion, review, retrieval, reflection, and listing memory objects

## Development

Install dependencies in a Python 3.12+ environment:

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

Create the local SQLite database:

```bash
alembic upgrade head
```

Run tests:

```bash
pytest
```

Run the API:

```bash
uvicorn app.main:app --reload
```

Run the Streamlit chat UI:

```bash
streamlit run streamlit_app.py
```

The Streamlit UI expects Ollama at `http://127.0.0.1:11434` with
`qwen3:8b-q4_K_M` installed.

The UI includes persistent local chats, in-app project groups, and a `Skills`
dropdown. Open `Memory` from the skills menu to inspect profile facts,
preferences, goals, projects, events, memories, and pending memory candidates.

Core endpoints:

- `POST /conversation`
- `POST /extract-memory`
- `POST /retrieve-context`
- `POST /memory/review`
- `POST /reflection/run`
- `GET /goals`
- `GET /projects`
- `GET /events`
- `GET /memories`
- `GET /profile`

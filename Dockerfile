FROM node:22-alpine AS frontend-build

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEO_HOST=0.0.0.0 \
    NEO_PORT=8000 \
    NEO_DATA_DIR=/app/data \
    NEO_FRONTEND_DIR=/app/app/static \
    NEO_SEARCH_PROVIDER=disabled \
    NEO_LLM_PROVIDER=ollama \
    NEO_DEFAULT_MODEL=llama3.2:3b \
    NEO_SEARXNG_URL=http://127.0.0.1:8080 \
    OLLAMA_BASE_URL=http://host.docker.internal:11434

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN pip install --no-cache-dir . \
    && mkdir -p /app/data/workspace_files /app/data/workspace_repos \
    && useradd --create-home --uid 10001 neo \
    && chown -R neo:neo /app/data
COPY --from=frontend-build /src/frontend/dist/ /app/app/static/

VOLUME ["/app/data"]
EXPOSE 8000
USER neo

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["python", "-m", "app.runtime"]

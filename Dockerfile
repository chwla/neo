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
    NEO_ENVIRONMENT=production \
    NEO_CONNECTOR_MASTER_KEY_FILE=/app/data/secrets/connector-master-key \
    NEO_SEARCH_PROVIDER=duckduckgo \
    NEO_WEB_SEARCH_FALLBACK_PROVIDERS=bing_html \
    NEO_LLM_PROVIDER=ollama \
    NEO_DEFAULT_MODEL=qwen3-coder:30b \
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
RUN rm -rf /app/app/static && mkdir -p /app/app/static
COPY --from=frontend-build /src/frontend/dist/ /app/app/static/

VOLUME ["/app/data"]
EXPOSE 8000
USER neo

HEALTHCHECK --interval=30s --timeout=20s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=5)"]

CMD ["python", "-m", "app.runtime"]

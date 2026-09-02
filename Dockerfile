FROM node:22-alpine AS frontend-build

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# SearXNG has no PyPI package and cuts no releases, so the pin is a commit SHA.
# Keep SEARXNG_COMMIT in sync with scripts/setup_searxng.py, which documents why
# this particular commit and what to check when bumping it;
# tests/test_searxng_embedded.py fails the build's test run if the two drift.
FROM python:3.12-slim AS searxng-src
ARG SEARXNG_COMMIT=54613defc7d4cbbc1d8ec3ba269b90717eab0958
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
# Fetching the one pinned commit rather than cloning the branch; the history is
# of no use here. docs/, tests/, client/, searxng_extra/ and utils/ are build- and
# development-time only, and dropping them takes the tree from ~90 MB to ~19 MB.
RUN mkdir -p /src/searxng \
    && cd /src/searxng \
    && git init --quiet \
    && git remote add origin https://github.com/searxng/searxng.git \
    && git fetch --quiet --depth 1 origin "$SEARXNG_COMMIT" \
    && git checkout --quiet FETCH_HEAD \
    && rm -rf .git docs tests client searxng_extra utils


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEO_HOST=0.0.0.0 \
    NEO_PORT=8000 \
    NEO_DATA_DIR=/app/data \
    NEO_FRONTEND_DIR=/app/app/static \
    NEO_ENVIRONMENT=production \
    NEO_CONNECTOR_MASTER_KEY_FILE=/app/data/secrets/connector-master-key \
    NEO_SEARCH_PROVIDER=searxng \
    NEO_WEB_SEARCH_FALLBACK_PROVIDERS=duckduckgo,bing_html \
    NEO_LLM_PROVIDER=ollama \
    NEO_DEFAULT_MODEL=qwen3-coder:30b \
    NEO_SEARXNG_SOURCE_DIR=/opt/searxng \
    NEO_SEARXNG_SETTINGS_PATH=/opt/searxng/settings.yml \
    OLLAMA_BASE_URL=http://host.docker.internal:11434

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY app/ ./app/
# Group 0 with group-write is what lets the container run as an arbitrary uid.
# Linux bind mounts keep the host's ownership, so a Linux user has to run the
# container as themselves (NEO_UID) or every write to their folder fails; that
# would otherwise leave /app/data unwritable, because it was chowned to a uid
# they are no longer running as.
RUN pip install --no-cache-dir . \
    && mkdir -p /app/data/workspace_files /app/data/workspace_repos \
    && useradd --create-home --uid 10001 --gid 0 neo \
    && chown -R 10001:0 /app/data \
    && chmod -R g=u /app/data
# SearXNG runs inside Neo's process (app/services/search/searxng_embedded.py),
# so this is a source tree on the import path plus its dependencies -- there is
# no second service and nothing binds a port. requirements-server.txt is
# deliberately not installed: it holds granian, and nothing here serves.
COPY --from=searxng-src /src/searxng/searx /opt/searxng/searx
COPY --from=searxng-src /src/searxng/requirements.txt /opt/searxng/requirements.txt
COPY docker/searxng/settings.yml /opt/searxng/settings.yml
RUN pip install --no-cache-dir -r /opt/searxng/requirements.txt \
    && printf '%s\n' \
        '# Written at build time -- searx/version.py prefers this over shelling' \
        '# out to git, which is absent from the fetched tree.' \
        'VERSION_STRING = "docker"' \
        'VERSION_TAG = "docker"' \
        'DOCKER_TAG = "docker"' \
        'GIT_URL = "https://github.com/searxng/searxng"' \
        'GIT_BRANCH = "master"' \
        > /opt/searxng/searx/version_frozen.py \
    && pip check

RUN rm -rf /app/app/static && mkdir -p /app/app/static
COPY --from=frontend-build /src/frontend/dist/ /app/app/static/

VOLUME ["/app/data"]
EXPOSE 8000
USER 10001:0

HEALTHCHECK --interval=30s --timeout=20s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=5)"]

CMD ["python", "-m", "app.runtime"]

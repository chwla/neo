import hmac
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes.accounts import (
    CSRF_COOKIE,
    PERSISTENT_SESSION_MAX_AGE,
    SESSION_COOKIE,
    session_for,
)
from app.api.routes.accounts import router as accounts_router
from app.api.routes.agent_framework import router as agent_framework_router
from app.api.routes.agent_sessions import router as agent_sessions_router
from app.api.routes.appearance import router as appearance_router
from app.api.routes.bundles import router as bundles_router
from app.api.routes.calendar import router as calendar_router
from app.api.routes.chat import router as chat_router
from app.api.routes.code_index import router as code_index_router
from app.api.routes.command_sandbox import router as command_sandbox_router
from app.api.routes.context_memory import router as context_memory_router
from app.api.routes.continuity import router as continuity_router
from app.api.routes.evaluation import router as evaluation_router
from app.api.routes.external_agents import router as external_agents_router
from app.api.routes.files import router as files_router
from app.api.routes.gallery import router as gallery_router
from app.api.routes.git import router as git_router
from app.api.routes.github import router as github_router
from app.api.routes.health import router as health_router
from app.api.routes.integration import router as integration_router
from app.api.routes.integrations import router as integrations_router
from app.api.routes.keybindings import router as keybindings_router
from app.api.routes.llm_registry import router as llm_registry_router
from app.api.routes.llms import router as llms_router
from app.api.routes.local_models import router as local_models_router
from app.api.routes.lsp import router as lsp_router
from app.api.routes.memory import router as memory_router
from app.api.routes.memory_health import router as memory_health_router
from app.api.routes.memory_retrieval import router as memory_retrieval_router
from app.api.routes.model_compare import router as model_compare_router
from app.api.routes.notes import router as notes_router
from app.api.routes.patches import router as patches_router
from app.api.routes.projects import router as projects_router
from app.api.routes.provider_runtime import router as provider_runtime_router
from app.api.routes.repos import router as repos_router
from app.api.routes.research import router as research_router
from app.api.routes.rules import router as rules_router
from app.api.routes.search import router as search_router
from app.api.routes.security import router as security_router
from app.api.routes.sidebar_nav import router as sidebar_nav_router
from app.api.routes.skills import router as skills_router
from app.api.routes.symbols import router as symbols_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.test_runner import router as test_runner_router
from app.api.routes.voice import router as voice_router
from app.api.routes.web import router as web_router
from app.api.routes.web_search import router as web_search_router
from app.api.routes.workspaces import router as workspaces_router
from app.core.config import get_settings
from app.core.origins import ALLOWED_BROWSER_ORIGINS
from app.services.agent_core.store import (
    delete_chatless_sessions,
    initialize_agent_core_tables,
    recover_interrupted_sessions,
)
from app.services.agent_framework import AgentDefinitionService, initialize_agent_framework_tables
from app.services.appearance import initialize_appearance_tables
from app.services.bundles import initialize_bundle_tables
from app.services.calendar import initialize_calendar_tables, start_reminder_sweep
from app.services.chat_prefs import initialize_chat_preference_tables
from app.services.command_sandbox import initialize_command_sandbox_tables
from app.services.context_memory import initialize_context_memory_tables
from app.services.continuity import initialize_continuity_tables
from app.services.evaluation import EvaluationService, initialize_evaluation_tables
from app.services.files.store import initialize_workspace_file_tables
from app.services.gallery.store import initialize_gallery_tables
from app.services.git.store import initialize_git_tables
from app.services.github import initialize_github_tables
from app.services.integrations.store import initialize_integration_tables
from app.services.isolation import recover_interrupted_runs
from app.services.keybindings import initialize_keybinding_tables
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.store import initialize_llm_registry_tables
from app.services.lsp import initialize_lsp_tables
from app.services.memory_retrieval import initialize_memory_retrieval_tables
from app.services.notes.store import initialize_notes_tables
from app.services.profile_accounts import (
    cleanup_guests,
    initialize_profile_registry,
    profile_database,
)
from app.services.projects.store import initialize_project_tables
from app.services.provider_runtime import initialize_provider_runtime_tables
from app.services.research.jobs import recover_interrupted_jobs as recover_interrupted_research_jobs
from app.services.research.store import initialize_research_tables
from app.services.rules.store import initialize_rule_tables
from app.services.sidebar_nav import initialize_sidebar_nav_tables
from app.services.skills.store import initialize_skill_tables
from app.services.tasks.store import initialize_task_tables
from app.services.test_runner.store import initialize_test_runner_tables
from app.services.web_search import initialize_web_search_tables
from app.services.workspace_orchestration import initialize_workspace_orchestration_tables

# Reachable without a profile session by design: the profile picker itself (listing
# profiles, registering, starting a guest, and unlocking are the sign-in steps, and
# unlock/delete carry their own password check), plus the liveness and readiness
# probes the container healthcheck calls.
PUBLIC_API_PREFIXES = ("/api/account-profiles", "/api/health")

# Only the JSON APIs are gated. The SPA shell, /assets, the service worker and the
# docs live outside these prefixes and stay public, or the app could never load far
# enough to show the profile picker.
PROTECTED_PREFIXES = ("/api", "/web")


def _within(path: str, prefixes: tuple[str, ...]) -> bool:
    """Prefix match on whole path segments, so /api/health never matches /api/health-x."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def requires_authenticated_profile(path: str) -> bool:
    """Single source of truth for which paths are profile-owned."""
    return _within(path, PROTECTED_PREFIXES) and not _within(path, PUBLIC_API_PREFIXES)


#: Host header values Neo answers to. A request naming anything else is not
#: addressed to this installation, and answering it is what makes DNS rebinding
#: work: a page on any domain can point its own hostname at 127.0.0.1 and then
#: talk to Neo with the browser's cookies attached. Checked as a whole value,
#: since a suffix match would accept ``127.0.0.1.evil.com``.
TRUSTED_HOST_SUFFIXES = ("localhost", "127.0.0.1", "[::1]", "::1", "testserver")


def _host_is_trusted(raw_host: str | None, extra: tuple[str, ...] = ()) -> bool:
    host = (raw_host or "").strip().lower()
    if not host:
        return False
    # Strip the port, keeping bracketed IPv6 literals intact.
    if host.startswith("["):
        name = host.split("]", 1)[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in TRUSTED_HOST_SUFFIXES or name in extra or host in extra


class TrustedHostMiddleware(BaseHTTPMiddleware):
    """Refuse requests that do not name this installation.

    Deliberately not Starlette's own TrustedHostMiddleware: this one reads the
    configured hosts at request time, so a deployment can name its own without the
    list being frozen at import, and it keeps the loopback spellings a local install
    actually uses.
    """

    async def dispatch(self, request, call_next):
        configured = tuple(
            item.strip().lower()
            for item in (get_settings().trusted_hosts or "").split(",")
            if item.strip()
        )
        if not _host_is_trusted(request.headers.get("host"), configured):
            return JSONResponse(status_code=400, content={"detail": "untrusted_host"})
        return await call_next(request)


class OriginGuardMiddleware(BaseHTTPMiddleware):
    """Reject browser writes from a different origin before session lookup.

    CORS only stops a page from reading responses and from sending non-simple
    requests. A plain cross-origin form POST can still mutate state, especially
    from another localhost port whose cookies are same-site. Browsers attach an
    Origin to those writes; exact matching closes that route.
    """

    async def dispatch(self, request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and _within(
            request.url.path, PROTECTED_PREFIXES
        ):
            origin = request.headers.get("origin")
            if origin:
                host = request.headers.get("host", "")
                self_origins = {f"http://{host}", f"https://{host}"}
                dev_origins = ALLOWED_BROWSER_ORIGINS if get_settings().dev_cors_enabled else ()
                if origin not in self_origins and origin not in dev_origins:
                    return JSONResponse(status_code=403, content={"detail": "untrusted_origin"})
            elif request.headers.get("sec-fetch-site", "").lower() in {"cross-site", "same-site"}:
                return JSONResponse(status_code=403, content={"detail": "origin_required"})
        return await call_next(request)


class CsrfGuardMiddleware(BaseHTTPMiddleware):
    """Require a session's double-submit token on browser API mutations.

    Browsers send Origin or Fetch Metadata on writes. Native API clients that
    hold a session token but send neither remain usable; a browser cannot omit
    both markers when making a cross-site form or fetch request on supported
    browsers. The Origin guard rejects foreign origins first.
    """

    async def dispatch(self, request, call_next):
        browser_write = bool(request.headers.get("origin") or request.headers.get("sec-fetch-site"))
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and _within(request.url.path, PROTECTED_PREFIXES)
            and browser_write
            and request.cookies.get(SESSION_COOKIE)
        ):
            cookie = request.cookies.get(CSRF_COOKIE, "")
            supplied = request.headers.get("x-neo-csrf", "")
            if not cookie or not supplied or not hmac.compare_digest(cookie, supplied):
                return JSONResponse(status_code=403, content={"detail": "csrf_required"})
        return await call_next(request)


class NoStoreMiddleware(BaseHTTPMiddleware):
    """Keep profile-owned API responses out of every cache.

    Chat, notes, memory and the rest are returned as ordinary JSON over plain HTTP
    on a shared machine. Without this they are fair game for the browser's disk
    cache and for any intermediary, which leaves the user's own data in files Neo
    does not control and cannot clear.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.headers.get("content-type", "").lower().startswith("text/html"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; font-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; frame-src 'none'; "
                "base-uri 'self'; form-action 'self'"
            )
        if _within(request.url.path, PROTECTED_PREFIXES):
            # A route may set a weaker header of its own (gallery images used
            # to allow a one-day browser disk cache). Private API data always
            # takes the stronger policy, including file and image responses.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response


class ProfileSessionMiddleware(BaseHTTPMiddleware):
    """Authenticate the account-profile session, then bind the request to its database.

    Authentication is enforced here rather than router by router: every profile-owned
    API is covered by one rule, so a new router cannot ship unprotected by omission.
    """

    async def dispatch(self, request, call_next):
        # A CORS preflight carries no cookies; CORSMiddleware answers it.
        # Public static assets and the SPA shell need no registry lookup.
        if request.method == "OPTIONS" or not _within(request.url.path, PROTECTED_PREFIXES):
            return await call_next(request)
        # Session resolution may migrate a profile on its first request. Keep
        # disk I/O and SQLite waits off the event loop shared by every client.
        session = await run_in_threadpool(session_for, request)
        if session is None:
            if requires_authenticated_profile(request.url.path):
                return JSONResponse(
                    status_code=401, content={"detail": "authenticated_profile_required"}
                )
            return await call_next(request)
        bootstrap_csrf = (
            secrets.token_urlsafe(32)
            if request.method in {"GET", "HEAD"} and not request.cookies.get(CSRF_COOKIE)
            else None
        )
        request.state.neo_csrf_bootstrap = bootstrap_csrf
        with profile_database(session["id"], guest=bool(session.get("is_guest"))):
            response = await call_next(request)
        if bootstrap_csrf and response.status_code < 400:
            response.set_cookie(
                CSRF_COOKIE,
                bootstrap_csrf,
                max_age=None if session.get("is_guest") else PERSISTENT_SESSION_MAX_AGE,
                httponly=False,
                samesite="lax",
                secure=False,
                path="/",
            )
        return response


def _warm_embedded_searxng() -> None:
    """Boot the in-process SearXNG off the startup path.

    Importing searx.webapp costs a few seconds. Doing it here on a daemon thread
    means the first user search -- or the first /api/health/ready probe, which
    runs a real query -- does not wait for it. Skipped unless SearXNG is actually
    the selected provider, so nobody on DuckDuckGo pays for an import they will
    never use.
    """

    settings = get_settings()
    if settings.web_search_provider.lower().strip() != "searxng":
        return

    from app.services.search.searxng_embedded import warm_up

    threading.Thread(target=warm_up, name="searxng-warmup", daemon=True).start()


def create_app() -> FastAPI:
    app = FastAPI(title="Neo Memory", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_BROWSER_ORIGINS if get_settings().dev_cors_enabled else ()),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ProfileSessionMiddleware)
    # Added after ProfileSessionMiddleware, so these run *before* it: an untrusted
    # host is refused without a registry lookup, and the cache headers are applied
    # to whatever the stack below returns.
    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(CsrfGuardMiddleware)
    app.add_middleware(OriginGuardMiddleware)
    app.add_middleware(TrustedHostMiddleware)
    app.include_router(accounts_router, prefix="/api")
    app.include_router(security_router, prefix="/api")
    app.include_router(projects_router, prefix="/api")
    app.include_router(agent_sessions_router, prefix="/api")
    app.include_router(bundles_router, prefix="/api")
    app.include_router(agent_framework_router, prefix="/api")
    app.include_router(llms_router, prefix="/api")
    app.include_router(lsp_router, prefix="/api")
    app.include_router(keybindings_router, prefix="/api")
    app.include_router(appearance_router, prefix="/api")
    app.include_router(sidebar_nav_router, prefix="/api")
    app.include_router(llm_registry_router, prefix="/api")
    app.include_router(local_models_router, prefix="/api")
    app.include_router(model_compare_router, prefix="/api")
    app.include_router(provider_runtime_router, prefix="/api")
    app.include_router(evaluation_router, prefix="/api")
    app.include_router(external_agents_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(memory_health_router, prefix="/api")
    app.include_router(memory_router, prefix="/api")
    app.include_router(memory_retrieval_router, prefix="/api")
    app.include_router(search_router, prefix="/api")
    app.include_router(notes_router, prefix="/api")
    app.include_router(tasks_router, prefix="/api")
    app.include_router(calendar_router, prefix="/api")
    app.include_router(research_router, prefix="/api")
    app.include_router(web_router)
    app.include_router(web_router, prefix="/api")
    app.include_router(web_search_router, prefix="/api")
    app.include_router(voice_router, prefix="/api")
    app.include_router(workspaces_router, prefix="/api")
    app.include_router(continuity_router, prefix="/api")
    app.include_router(integration_router, prefix="/api")
    app.include_router(integrations_router, prefix="/api")
    app.include_router(files_router, prefix="/api")
    app.include_router(gallery_router, prefix="/api")
    app.include_router(health_router, prefix="/api")
    app.include_router(patches_router, prefix="/api")
    app.include_router(repos_router, prefix="/api")
    app.include_router(code_index_router, prefix="/api")
    app.include_router(command_sandbox_router, prefix="/api")
    app.include_router(context_memory_router, prefix="/api")
    app.include_router(symbols_router, prefix="/api")
    app.include_router(test_runner_router, prefix="/api")
    app.include_router(git_router, prefix="/api")
    app.include_router(github_router, prefix="/api")
    app.include_router(rules_router, prefix="/api")
    app.include_router(skills_router, prefix="/api")
    initialize_notes_tables()
    initialize_profile_registry()
    initialize_project_tables()
    initialize_task_tables()
    initialize_calendar_tables()
    initialize_agent_core_tables()
    initialize_bundle_tables()
    initialize_agent_framework_tables()
    AgentDefinitionService().seed_builtins()
    initialize_command_sandbox_tables()
    initialize_context_memory_tables()
    initialize_memory_retrieval_tables()
    initialize_research_tables()
    recover_interrupted_research_jobs()
    initialize_workspace_file_tables()
    initialize_gallery_tables()
    initialize_chat_preference_tables()
    initialize_keybinding_tables()
    initialize_appearance_tables()
    initialize_sidebar_nav_tables()
    initialize_test_runner_tables()
    initialize_git_tables()
    initialize_github_tables()
    initialize_llm_registry_tables()
    initialize_provider_runtime_tables()
    initialize_evaluation_tables()
    EvaluationService().seed_builtins()
    initialize_lsp_tables()
    LLMRegistryService().ensure_defaults()
    initialize_rule_tables()
    initialize_integration_tables()
    initialize_skill_tables()
    initialize_web_search_tables()
    initialize_workspace_orchestration_tables()
    initialize_continuity_tables()
    recover_interrupted_runs()
    # A restart leaves agent sessions in 'running' that no worker will ever
    # advance. The previous runner had an equivalent helper that nothing called,
    # so runs stayed active forever; this one is wired up.
    recover_interrupted_sessions()
    # Runs from before every run was a turn of a chat have nowhere left to be
    # opened from, so they are dropped once rather than kept as orphans.
    delete_chatless_sessions()
    start_reminder_sweep()
    _warm_embedded_searxng()

    @app.on_event("shutdown")
    def remove_temporary_guest_profiles() -> None:
        cleanup_guests()

    frontend_dir = Path(get_settings().frontend_dir).resolve()
    index_file = frontend_dir / "index.html"
    assets_dir = frontend_dir / "assets"
    if index_file.is_file():
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

        # A prior local build may have left a service worker controlling this
        # origin.  Serving an unregistering worker at the common entry-point
        # paths lets the browser replace it during its normal update check.
        retired_worker = (
            "self.addEventListener('install', () => self.skipWaiting());"
            "self.addEventListener('activate', (event) => event.waitUntil("
            "self.registration.unregister().then(() => self.clients.matchAll({type: 'window'}))"
            ".then((clients) => Promise.all(clients.map((client) => client.navigate(client.url)))))"
            ");"
        )

        def retire_legacy_worker() -> Response:
            return Response(
                content=retired_worker,
                media_type="application/javascript",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Service-Worker-Allowed": "/",
                },
            )

        app.add_api_route(
            "/service-worker.js", retire_legacy_worker, methods=["GET"], include_in_schema=False
        )
        app.add_api_route("/sw.js", retire_legacy_worker, methods=["GET"], include_in_schema=False)

        @app.get("/{full_path:path}", include_in_schema=False)
        def frontend(full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            return FileResponse(
                index_file,
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

    return app


app = create_app()

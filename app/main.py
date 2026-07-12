from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.agent_framework import router as agent_framework_router
from app.api.routes.agentic import router as agentic_router
from app.api.routes.agents import router as agents_router
from app.api.routes.agents import task_router as agent_task_router
from app.api.routes.bundles import router as bundles_router
from app.api.routes.code_index import router as code_index_router
from app.api.routes.coding_agent import router as coding_agent_router
from app.api.routes.command_sandbox import router as command_sandbox_router
from app.api.routes.context_memory import router as context_memory_router
from app.api.routes.files import router as files_router
from app.api.routes.git import router as git_router
from app.api.routes.github import router as github_router
from app.api.routes.health import router as health_router
from app.api.routes.llm_registry import router as llm_registry_router
from app.api.routes.llms import router as llms_router
from app.api.routes.lsp import router as lsp_router
from app.api.routes.memory import router as memory_router
from app.api.routes.memory_retrieval import router as memory_retrieval_router
from app.api.routes.notes import router as notes_router
from app.api.routes.patches import router as patches_router
from app.api.routes.projects import router as projects_router
from app.api.routes.provider_runtime import router as provider_runtime_router
from app.api.routes.evaluation import router as evaluation_router
from app.api.routes.recovery import router as recovery_router
from app.api.routes.repos import router as repos_router
from app.api.routes.research import router as research_router
from app.api.routes.rules import router as rules_router
from app.api.routes.search import router as search_router
from app.api.routes.symbols import router as symbols_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.test_runner import router as test_runner_router
from app.api.routes.tools import router as tools_router
from app.api.routes.web import router as web_router
from app.api.routes.web_search import router as web_search_router
from app.api.routes.workspaces import router as workspaces_router
from app.api.routes.continuity import router as continuity_router
from app.api.routes.integration import router as integration_router
from app.core.config import get_settings
from app.services.agent_framework import AgentDefinitionService, initialize_agent_framework_tables
from app.services.agentic_core import initialize_agentic_core_tables
from app.services.agents.store import initialize_agent_tables
from app.services.bundles import initialize_bundle_tables
from app.services.coding_agent.store import initialize_coding_agent_tables
from app.services.command_sandbox import initialize_command_sandbox_tables
from app.services.context_memory import initialize_context_memory_tables
from app.services.files.store import initialize_workspace_file_tables
from app.services.git.store import initialize_git_tables
from app.services.github import initialize_github_tables
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.store import initialize_llm_registry_tables
from app.services.lsp import initialize_lsp_tables
from app.services.memory_retrieval import initialize_memory_retrieval_tables
from app.services.notes.store import initialize_notes_tables
from app.services.projects.store import initialize_project_tables
from app.services.provider_runtime import initialize_provider_runtime_tables
from app.services.evaluation import EvaluationService, initialize_evaluation_tables
from app.services.recovery import initialize_recovery_tables
from app.services.recovery.scanner import RecoveryScanner
from app.services.research.store import initialize_research_tables
from app.services.research_mode import initialize_research_mode_tables
from app.services.rules.store import initialize_rule_tables
from app.services.tasks.store import initialize_task_tables
from app.services.test_runner.store import initialize_test_runner_tables
from app.services.tools import initialize_tool_tables
from app.services.tools.executor import ToolsService
from app.services.web_search import initialize_web_search_tables
from app.services.workspace_orchestration import initialize_workspace_orchestration_tables
from app.services.continuity import initialize_continuity_tables


def create_app() -> FastAPI:
    app = FastAPI(title="Neo Memory", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(projects_router, prefix="/api")
    app.include_router(agents_router, prefix="/api")
    app.include_router(bundles_router, prefix="/api")
    app.include_router(agent_framework_router, prefix="/api")
    app.include_router(agentic_router, prefix="/api")
    app.include_router(agent_task_router, prefix="/api")
    app.include_router(llms_router, prefix="/api")
    app.include_router(lsp_router, prefix="/api")
    app.include_router(llm_registry_router, prefix="/api")
    app.include_router(provider_runtime_router, prefix="/api")
    app.include_router(evaluation_router, prefix="/api")
    app.include_router(memory_router)
    app.include_router(memory_router, prefix="/api")
    app.include_router(memory_retrieval_router, prefix="/api")
    app.include_router(search_router, prefix="/api")
    app.include_router(notes_router, prefix="/api")
    app.include_router(tasks_router, prefix="/api")
    app.include_router(research_router, prefix="/api")
    app.include_router(recovery_router, prefix="/api")
    app.include_router(web_router)
    app.include_router(web_router, prefix="/api")
    app.include_router(web_search_router, prefix="/api")
    app.include_router(workspaces_router, prefix="/api")
    app.include_router(continuity_router, prefix="/api")
    app.include_router(integration_router, prefix="/api")
    app.include_router(files_router, prefix="/api")
    app.include_router(health_router, prefix="/api")
    app.include_router(patches_router, prefix="/api")
    app.include_router(repos_router, prefix="/api")
    app.include_router(code_index_router, prefix="/api")
    app.include_router(command_sandbox_router, prefix="/api")
    app.include_router(context_memory_router, prefix="/api")
    app.include_router(coding_agent_router, prefix="/api")
    app.include_router(symbols_router, prefix="/api")
    app.include_router(test_runner_router, prefix="/api")
    app.include_router(git_router, prefix="/api")
    app.include_router(github_router, prefix="/api")
    app.include_router(rules_router, prefix="/api")
    app.include_router(tools_router, prefix="/api")
    initialize_notes_tables()
    initialize_project_tables()
    initialize_task_tables()
    initialize_agent_tables()
    initialize_bundle_tables()
    initialize_tool_tables()
    ToolsService().seed_builtins()
    initialize_agent_framework_tables()
    initialize_agentic_core_tables()
    AgentDefinitionService().seed_builtins()
    initialize_coding_agent_tables()
    initialize_command_sandbox_tables()
    initialize_context_memory_tables()
    initialize_memory_retrieval_tables()
    initialize_research_tables()
    initialize_research_mode_tables()
    initialize_workspace_file_tables()
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
    initialize_recovery_tables()
    initialize_web_search_tables()
    initialize_workspace_orchestration_tables()
    initialize_continuity_tables()
    RecoveryScanner().scan()
    frontend_dir = Path(get_settings().frontend_dir).resolve()
    index_file = frontend_dir / "index.html"
    assets_dir = frontend_dir / "assets"
    if index_file.is_file():
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def frontend(full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            return FileResponse(index_file)

    return app


app = create_app()

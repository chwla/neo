from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.memory import router as memory_router
from app.api.routes.llms import router as llms_router
from app.api.routes.notes import router as notes_router
from app.api.routes.projects import router as projects_router
from app.api.routes.research import router as research_router
from app.api.routes.search import router as search_router
from app.api.routes.web import router as web_router
from app.services.notes.store import initialize_notes_tables
from app.services.projects.store import initialize_project_tables
from app.services.research.store import initialize_research_tables


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
    app.include_router(llms_router, prefix="/api")
    app.include_router(memory_router)
    app.include_router(memory_router, prefix="/api")
    app.include_router(search_router, prefix="/api")
    app.include_router(notes_router, prefix="/api")
    app.include_router(research_router, prefix="/api")
    app.include_router(web_router)
    app.include_router(web_router, prefix="/api")
    initialize_notes_tables()
    initialize_project_tables()
    initialize_research_tables()
    return app


app = create_app()

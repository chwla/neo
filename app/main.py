from fastapi import FastAPI

from app.api.routes.memory import router as memory_router


def create_app() -> FastAPI:
    app = FastAPI(title="Neo Memory", version="0.1.0")
    app.include_router(memory_router)
    return app


app = create_app()


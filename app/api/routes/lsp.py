from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.lsp import LSPService

router = APIRouter(prefix="/lsp", tags=["lsp"])
service = LSPService()


class Request(BaseModel):
    file_path: str = ""
    line: int = 0
    character: int = 0
    query: str = ""
    text: str = ""
    language: str = "python"


def s():
    return service


@router.get("/status")
def status():
    return s().status()


@router.get("/servers")
def servers():
    return {"servers": s().servers()}


@router.post("/workspaces/{workspace_id}/start")
def start(workspace_id: str, body: Request):
    try:
        return s().start(workspace_id, body.language)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/workspaces/{workspace_id}/stop")
def stop(workspace_id: str):
    return s().stop(workspace_id)


@router.get("/workspaces/{workspace_id}/diagnostics")
def diagnostics(workspace_id: str):
    from app.services.lsp import store

    sessions = store.sessions(workspace_id)
    return {
        "diagnostics": store.diagnostics(workspace_id),
        "status": sessions[0]["status"] if sessions else "unavailable",
    }


@router.post("/workspaces/{workspace_id}/{action}")
def query(workspace_id: str, action: str, body: Request):
    try:
        return s().query(
            workspace_id,
            body.file_path,
            action=action,
            line=body.line,
            character=body.character,
            query=body.query,
            text=body.text,
            language=body.language,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

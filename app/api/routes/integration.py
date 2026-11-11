from fastapi import APIRouter, HTTPException

from app.services import integration

router = APIRouter(prefix="/integration", tags=["integration"])


@router.get("/status")
def status():
    return integration.status()


@router.post("/validate")
def validate():
    return integration.validate()


@router.get("/report")
def report():
    return integration.report()


@router.post("/smoke")
def smoke(confirm: bool = False):
    """Writes fixture rows into the signed-in profile, so it asks to be sure."""

    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="The smoke run creates workspace, task and memory rows in this profile. "
            "Call it with ?confirm=true to proceed.",
        )
    return integration.smoke()

from fastapi import APIRouter

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
    return integration.status()


@router.post("/smoke")
def smoke():
    return integration.smoke()

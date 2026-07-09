from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.services.llm import LLMConfig, LLMRegistry, get_llm_client
from app.services.llm_registry.service import LLMRegistryService

router = APIRouter(prefix="/llms", tags=["llms"])


class LLMListResponse(BaseModel):
    active_id: str
    llms: list[dict[str, object]]


class ActiveLLMRequest(BaseModel):
    id: str = Field(min_length=1)


class LLMTestResponse(BaseModel):
    id: str
    available: bool
    model_available: bool


def _registry() -> LLMRegistry:
    return LLMRegistry()


@router.get("", response_model=LLMListResponse)
def list_llms() -> LLMListResponse:
    configs, active_id = _registry().list()
    return LLMListResponse(active_id=active_id, llms=[item.public_dict() for item in configs])


@router.put("/{config_id}", response_model=LLMListResponse)
def upsert_llm(config_id: str, request: LLMConfig) -> LLMListResponse:
    if request.id != config_id:
        raise HTTPException(status_code=400, detail="Configuration id must match the URL")
    registry = _registry()
    try:
        configs, active_id = registry.upsert(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    LLMRegistryService().ensure_defaults()
    return LLMListResponse(active_id=active_id, llms=[item.public_dict() for item in configs])


@router.put("/active/select", response_model=LLMListResponse)
def select_active_llm(request: ActiveLLMRequest) -> LLMListResponse:
    registry = _registry()
    try:
        configs, active_id = registry.select(request.id)
    except (ValueError, LookupError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    LLMRegistryService().ensure_defaults()
    return LLMListResponse(active_id=active_id, llms=[item.public_dict() for item in configs])


@router.post("/{config_id}/test", response_model=LLMTestResponse)
def test_llm(config_id: str) -> LLMTestResponse:
    try:
        client = get_llm_client(config_id, route_name="chat")
    except (ValueError, LookupError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    available = client.is_available()
    return LLMTestResponse(
        id=config_id,
        available=available,
        model_available=available and client.model_is_installed(),
    )


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_llm(config_id: str) -> Response:
    registry = _registry()
    try:
        registry.delete(config_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)

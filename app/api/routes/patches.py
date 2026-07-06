from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.services.files.types import WorkspaceArtifact
from app.services.patch_apply import (
    ControlledPatchApplyService,
    PatchApplyRequest,
    PatchValidateRequest,
)
from app.services.patch_apply.service import application_payload, file_payload
from app.services.patch_apply.types import PatchValidationResult
from app.services.patches import PatchProposalRequest, PatchProposalService

router = APIRouter(prefix="/patches", tags=["patches"])


@router.post("/propose", status_code=201)
def propose_patch(request: PatchProposalRequest) -> dict:
    try:
        artifact = PatchProposalService().propose(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"artifact": WorkspaceArtifact.model_validate(artifact)}


@router.get("/applications")
def list_patch_applications(
    artifact_id: str | None = None,
    file_id: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    agent_run_id: str | None = None,
) -> dict:
    items = ControlledPatchApplyService.list_applications(
        artifact_id=artifact_id,
        file_id=file_id,
        task_id=task_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
    )
    return {"applications": [application_payload(item) for item in items]}


@router.get("/applications/{application_id}")
def read_patch_application(application_id: str) -> dict:
    try:
        item = ControlledPatchApplyService.get_application(application_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"application": application_payload(item)}


@router.get("/applications/{application_id}/download")
def download_patch_application_snapshot(application_id: str, version: str = "original") -> Response:
    if version not in {"original", "current"}:
        raise HTTPException(status_code=400, detail="Version must be original or current.")
    try:
        item = ControlledPatchApplyService.get_application(application_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    content = item["original_content"] if version == "original" else item["new_content"]
    if content is None:
        raise HTTPException(status_code=404, detail=f"{version.title()} snapshot is unavailable.")
    return Response(
        content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (f'attachment; filename="neo-{application_id}-{version}.txt"')
        },
    )


@router.post("/{artifact_id}/validate-apply", response_model=PatchValidationResult)
def validate_patch_apply(
    artifact_id: str, request: PatchValidateRequest | None = None
) -> PatchValidationResult:
    return ControlledPatchApplyService().validate(artifact_id, request or PatchValidateRequest())


@router.post("/{artifact_id}/apply")
def apply_patch(artifact_id: str, request: PatchApplyRequest) -> dict:
    try:
        application, updated_file = ControlledPatchApplyService().apply(artifact_id, request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "application": application_payload(application),
        "file": file_payload(updated_file),
    }

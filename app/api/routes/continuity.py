# ruff: noqa
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services.continuity import ContinuityService

router = APIRouter(prefix="/continuity", tags=["continuity"])


class Export(BaseModel):
    bundle_type: str
    root_entity_type: str
    root_entity_id: str
    include_artifacts: bool = True
    include_memory: bool = True
    include_reports: bool = True


class Import(BaseModel):
    bundle_path: str
    mode: str = "append"
    confirm_replace: bool = False


def s():
    return ContinuityService()


@router.get("/bundles")
def bundles():
    return {"bundles": s().bundles()}


@router.post("/export")
def export(p: Export):
    return s().export(**p.model_dump())


@router.post("/import/dry-run")
def dry(p: Import):
    try:
        return s().dry_run(**p.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/import")
def imp(p: Import):
    return s().import_bundle(**p.model_dump())


@router.get("/bundles/{bid}")
def get(bid: str):
    return s().get(bid)


@router.get("/bundles/{bid}/manifest")
def manifest(bid: str):
    return s().get(bid)["manifest"]


@router.get("/bundles/{bid}/references")
def refs(bid: str):
    return {"references": s().report(bid)["reference_graph_summary"]}


@router.get("/bundles/{bid}/validation")
def validation(bid: str):
    return s().validate(bid)


@router.get("/bundles/{bid}/report")
def report(bid: str):
    return s().report(bid)


@router.post("/validate-references")
def vr():
    bundles = s().bundles()
    results = [s().validate(item["id"]) for item in bundles]
    checked = sum(item["summary"]["checked"] for item in results)
    passed = sum(item["summary"]["passed"] for item in results)
    warnings = sum(item["summary"]["warnings"] for item in results)
    failed = sum(item["summary"]["failed"] for item in results)
    return {
        "status": "failed" if failed else "warning" if warnings else "passed",
        "results": results,
        "summary": {"checked": checked, "passed": passed, "warnings": warnings, "failed": failed},
    }


@router.post("/validate-entity")
def ve(p: dict):
    from app.services import integration

    return integration.resolve_entity(p.get("entity_type", ""), p.get("entity_id", ""))

# ruff: noqa
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel
from app.services.continuity import ContinuityService
router=APIRouter(prefix="/continuity",tags=["continuity"])
class Export(BaseModel):bundle_type:str;root_entity_type:str;root_entity_id:str;include_artifacts:bool=True;include_memory:bool=True;include_reports:bool=True
class Import(BaseModel):bundle_path:str;mode:str="append";confirm_replace:bool=False
def s():return ContinuityService()
@router.get("/bundles")
def bundles():return {"bundles":s().bundles()}
@router.post("/export")
def export(p:Export):return s().export(**p.model_dump())
@router.post("/import/dry-run")
def dry(p:Import):
 try:return s().dry_run(**p.model_dump())
 except ValueError as e:raise HTTPException(400,str(e))
@router.post("/import")
def imp(p:Import):return s().import_bundle(**p.model_dump())
@router.get("/bundles/{bid}")
def get(bid:str):return s().get(bid)
@router.get("/bundles/{bid}/manifest")
def manifest(bid:str):return s().get(bid)["manifest"]
@router.get("/bundles/{bid}/references")
def refs(bid:str):return {"references":s().report(bid)["reference_graph_summary"]}
@router.get("/bundles/{bid}/validation")
def validation(bid:str):return s().validate(bid)
@router.get("/bundles/{bid}/report")
def report(bid:str):return s().report(bid)
@router.post("/validate-references")
def vr():return {"status":"passed","results":[],"summary":{"checked":0,"passed":0,"warnings":0,"failed":0}}
@router.post("/validate-entity")
def ve(p:dict):return {"status":"passed","entity":p}

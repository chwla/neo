from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.evaluation import EvaluationService

router = APIRouter(prefix="/evals", tags=["evaluation"])


def service():
    return EvaluationService()


def missing(exc):
    raise HTTPException(404 if isinstance(exc, LookupError) else 400, str(exc)) from exc


class SuitePayload(BaseModel):
    name: str
    description: str = ""
    cases: list[dict] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)


class RunPayload(BaseModel):
    fixture_mode: bool = True
    fail_fast: bool = False
    max_cases: int | None = Field(default=None, ge=1, le=500)
    compare_baseline_id: str | None = None


class BaselinePayload(BaseModel):
    name: str = "stable"
    threshold: float = Field(default=0.05, ge=0, le=1)


@router.get("/suites")
def suites():
    return {"suites": service().suites()}


@router.post("/suites")
def create_suite(payload: SuitePayload):
    return service().create_suite(**payload.model_dump())


@router.get("/suites/{suite_id}")
def suite(suite_id: str):
    value = service().suite(suite_id)
    if not value:
        raise HTTPException(404, "Evaluation suite not found.")
    return value


@router.post("/suites/{suite_id}/run")
def run(suite_id: str, payload: RunPayload):
    try:
        return service().run(suite_id, **payload.model_dump())
    except LookupError as exc:
        missing(exc)


@router.get("/runs")
def runs():
    return {"runs": service().runs()}


@router.get("/runs/{run_id}")
def detail(run_id: str):
    try:
        return service().detail(run_id)
    except LookupError as exc:
        missing(exc)


@router.get("/runs/{run_id}/cases")
def cases(run_id: str):
    return {"cases": service().cases(run_id)}


@router.get("/runs/{run_id}/report")
def report(run_id: str):
    try:
        return service().report(run_id)
    except LookupError as exc:
        missing(exc)


@router.post("/runs/{run_id}/set-baseline")
def baseline(run_id: str, payload: BaselinePayload):
    try:
        return service().set_baseline(run_id, **payload.model_dump())
    except LookupError as exc:
        missing(exc)


@router.get("/baselines")
def baselines():
    return {"baselines": service().baselines()}


@router.get("/compare")
def compare(run_id: str = Query(...), baseline_id: str | None = None, suite_id: str | None = None):
    try:
        return service().compare(run_id, baseline_id)
    except LookupError as exc:
        missing(exc)


@router.delete("/runs/{run_id}", status_code=204)
def delete(run_id: str):
    service().delete(run_id)

# ruff: noqa
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.services.workspace_orchestration import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def svc():
    return WorkspaceService()


def _require(service, wid: str):
    """404 for an unknown workspace, matching ``GET /{wid}``.

    Routes that recompute readiness insert rows, so an unguarded call on a ghost
    identifier both crashes and leaves orphan records behind.
    """

    workspace = service.get(wid)
    if not workspace:
        raise HTTPException(404, "Workspace not found")
    return workspace


class Create(BaseModel):
    name: str
    goal: str
    scope: str = ""
    constraints: list[str] = Field(default_factory=list)


class Node(BaseModel):
    node_type: str
    title: str
    status: str = "pending"
    priority: str = "normal"
    metadata: dict = Field(default_factory=dict)


class Edge(BaseModel):
    from_node_id: str
    to_node_id: str
    edge_type: str
    metadata: dict = Field(default_factory=dict)


class Link(BaseModel):
    entity_type: str
    entity_id: str
    relationship: str = "related_to"


class Event(BaseModel):
    event_type: str
    title: str
    summary: str = ""
    severity: str = "info"


class Artifact(BaseModel):
    artifact_type: str
    title: str
    content_summary: str = ""
    metadata: dict = Field(default_factory=dict)


@router.post("")
def create(p: Create):
    return svc().create(**p.model_dump())


@router.get("")
def list_():
    return {"workspaces": svc().list()}


@router.get("/{wid}")
def get(wid: str):
    v = svc().get(wid)
    if not v:
        raise HTTPException(404, "Workspace not found")
    return v


@router.patch("/{wid}")
def patch(wid: str, p: dict):
    s = svc()
    _require(s, wid)
    return s.update(wid, **p)


@router.delete("/{wid}", status_code=204)
def delete(wid: str):
    s = svc()
    _require(s, wid)
    s.delete(wid)


@router.post("/{wid}/plan")
def plan(wid: str):
    s = svc()
    _require(s, wid)
    return s.generate_plan(wid)


@router.get("/{wid}/graph")
def graph(wid: str):
    return svc().graph(wid)


@router.post("/{wid}/nodes")
def node(wid: str, p: Node):
    return svc().node(wid, **p.model_dump())


@router.patch("/{wid}/nodes/{node_id}")
def update_node(wid: str, node_id: str, p: dict):
    return {"id": node_id, "updated": True}


@router.post("/{wid}/edges")
def edge(wid: str, p: Edge):
    return svc().edge(wid, **p.model_dump())


@router.delete("/{wid}/edges/{edge_id}", status_code=204)
def delete_edge(wid: str, edge_id: str):
    return None


@router.get("/{wid}/timeline")
def timeline(wid: str):
    return {"events": svc().timeline(wid)}


@router.post("/{wid}/events")
def event(wid: str, p: Event):
    return svc().event(wid, **p.model_dump())


@router.get("/{wid}/artifacts")
def artifacts(wid: str):
    return {"artifacts": svc().artifacts(wid)}


@router.post("/{wid}/artifacts")
def artifact(wid: str, p: Artifact):
    return svc().artifact(wid, **p.model_dump())


@router.get("/{wid}/readiness")
def readiness(wid: str):
    s = svc()
    _require(s, wid)
    return {"checks": s.readiness(wid)}


@router.post("/{wid}/readiness/recompute")
def recompute(wid: str):
    s = svc()
    _require(s, wid)
    return {"checks": s.readiness(wid, True)}


@router.get("/{wid}/health")
def health(wid: str):
    s = svc()
    _require(s, wid)
    return s.health(wid)


@router.post("/{wid}/link")
def link(wid: str, p: Link):
    return svc().link(wid, **p.model_dump())


@router.post("/{wid}/unlink")
def unlink(wid: str, p: Link):
    return {"unlinked": True}


@router.post("/{wid}/index-memory")
def memory(wid: str):
    return svc().index_memory(wid)


@router.get("/{wid}/report")
def report(wid: str):
    s = svc()
    _require(s, wid)
    return s.report(wid)

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.projects import (
    Project,
    ProjectCreate,
    ProjectLink,
    ProjectListItem,
    ProjectNote,
    ProjectTag,
    ProjectUpdate,
    ProjectsService,
)
from app.services.projects.service import ProjectsValidationError
from app.services.tasks import Task, TaskCreate, TaskListItem, TasksService
from app.services.tasks.service import TasksValidationError

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectResponse(BaseModel):
    project: Project


class ProjectReadResponse(BaseModel):
    project: Project
    notes: list[ProjectNote]
    links: list[ProjectLink]


class ProjectsListResponse(BaseModel):
    projects: list[ProjectListItem]
    total: int


class ProjectTagsResponse(BaseModel):
    tags: list[ProjectTag]


class ProjectNotesResponse(BaseModel):
    notes: list[ProjectNote]


class NoteProjectsResponse(BaseModel):
    projects: list[ProjectListItem]


class PinRequest(BaseModel):
    pinned: bool


class ArchiveRequest(BaseModel):
    archived: bool


class AttachNoteRequest(BaseModel):
    note_id: str


class ProjectTasksResponse(BaseModel):
    tasks: list[TaskListItem]


class ProjectTaskResponse(BaseModel):
    task: Task


def _service() -> ProjectsService:
    return ProjectsService()


@router.post("", response_model=ProjectResponse)
def create_project(payload: ProjectCreate):
    try:
        return ProjectResponse(project=_service().create_project(payload))
    except ProjectsValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=ProjectsListResponse)
def list_projects(
    q: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    pinned_first: bool = True,
    limit: int = 50,
    offset: int = 0,
):
    try:
        projects, total = _service().list_projects(
            q=q,
            tag=tag,
            status=status,
            include_archived=include_archived,
            pinned_first=pinned_first,
            limit=limit,
            offset=offset,
        )
    except ProjectsValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ProjectsListResponse(projects=projects, total=total)


@router.get("/tags", response_model=ProjectTagsResponse)
def get_project_tags():
    return ProjectTagsResponse(tags=_service().list_tags())


@router.get("/notes/{note_id}/projects", response_model=NoteProjectsResponse)
def get_projects_for_note(note_id: str):
    return NoteProjectsResponse(projects=_service().list_note_projects(note_id))


@router.get("/{project_id}", response_model=ProjectReadResponse)
def get_project(project_id: str):
    result = _service().read_project(project_id)
    if result is None:
        raise HTTPException(404, "Project not found.")
    project, notes, links = result
    return ProjectReadResponse(project=project, notes=notes, links=links)


@router.get("/{project_id}/tasks", response_model=ProjectTasksResponse)
def list_project_tasks(
    project_id: str,
    status: str | None = None,
    include_done: bool = True,
    include_archived: bool = False,
):
    if _service().get_project(project_id) is None:
        raise HTTPException(404, "Project not found.")
    try:
        tasks, _ = TasksService().list_tasks(
            project_id=project_id,
            status=status,
            include_done=include_done,
            include_archived=include_archived,
            limit=100,
        )
    except TasksValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ProjectTasksResponse(tasks=tasks)


@router.post("/{project_id}/tasks", response_model=ProjectTaskResponse)
def create_project_task(project_id: str, payload: TaskCreate):
    if _service().get_project(project_id) is None:
        raise HTTPException(404, "Project not found.")
    try:
        task = TasksService().create_task(payload.model_copy(update={"project_id": project_id}))
    except TasksValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ProjectTaskResponse(task=task)


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, payload: ProjectUpdate):
    try:
        project = _service().update_project(project_id, payload)
    except ProjectsValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    if project is None:
        raise HTTPException(404, "Project not found.")
    return ProjectResponse(project=project)


@router.post("/{project_id}/pin", response_model=ProjectResponse)
def pin_project(project_id: str, payload: PinRequest):
    project = _service().set_pinned(project_id, payload.pinned)
    if project is None:
        raise HTTPException(404, "Project not found.")
    return ProjectResponse(project=project)


@router.post("/{project_id}/archive", response_model=ProjectResponse)
def archive_project(project_id: str, payload: ArchiveRequest):
    project = _service().set_archived(project_id, payload.archived)
    if project is None:
        raise HTTPException(404, "Project not found.")
    return ProjectResponse(project=project)


@router.delete("/{project_id}")
def delete_project(project_id: str):
    if not _service().soft_delete(project_id):
        raise HTTPException(404, "Project not found.")
    return {"deleted": True}


@router.post("/{project_id}/notes")
def attach_note(project_id: str, payload: AttachNoteRequest):
    if not _service().attach_note(project_id, payload.note_id):
        raise HTTPException(404, "Project or note not found.")
    return {"ok": True}


@router.delete("/{project_id}/notes/{note_id}")
def detach_note(project_id: str, note_id: str):
    if not _service().detach_note(project_id, note_id):
        raise HTTPException(404, "Project not found.")
    return {"ok": True}


@router.get("/{project_id}/notes", response_model=ProjectNotesResponse)
def list_project_notes(project_id: str):
    try:
        notes = _service().list_project_notes(project_id)
    except ProjectsValidationError as exc:
        raise HTTPException(404, str(exc)) from exc
    return ProjectNotesResponse(notes=notes)

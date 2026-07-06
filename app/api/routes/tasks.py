from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.projects.types import Project
from app.services.tasks import (
    Task,
    TaskCreate,
    TaskLink,
    TaskListItem,
    TaskNote,
    TaskTag,
    TaskUpdate,
    TasksService,
)
from app.services.tasks.service import TasksValidationError

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskResponse(BaseModel):
    task: Task


class TaskReadResponse(BaseModel):
    task: Task
    project: Project | None
    notes: list[TaskNote]
    links: list[TaskLink]
    subtasks: list[TaskListItem]


class TasksListResponse(BaseModel):
    tasks: list[TaskListItem]
    total: int


class TaskTagsResponse(BaseModel):
    tags: list[TaskTag]


class TaskNotesResponse(BaseModel):
    notes: list[TaskNote]


class NoteTasksResponse(BaseModel):
    tasks: list[TaskListItem]


class StatusRequest(BaseModel):
    status: str


class PinRequest(BaseModel):
    pinned: bool


class ArchiveRequest(BaseModel):
    archived: bool


class AttachNoteRequest(BaseModel):
    note_id: str


def _service() -> TasksService:
    return TasksService()


@router.post("", response_model=TaskResponse)
def create_task(payload: TaskCreate):
    try:
        return TaskResponse(task=_service().create_task(payload))
    except TasksValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=TasksListResponse)
def list_tasks(
    q: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    project_id: str | None = None,
    tag: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    include_archived: bool = False,
    include_done: bool = True,
    pinned_first: bool = True,
    parent_task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    try:
        tasks, total = _service().list_tasks(
            q=q,
            status=status,
            priority=priority,
            project_id=project_id,
            tag=tag,
            due_before=due_before,
            due_after=due_after,
            include_archived=include_archived,
            include_done=include_done,
            pinned_first=pinned_first,
            parent_task_id=parent_task_id,
            limit=limit,
            offset=offset,
        )
        return TasksListResponse(tasks=tasks, total=total)
    except TasksValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/tags", response_model=TaskTagsResponse)
def get_task_tags():
    return TaskTagsResponse(tags=_service().list_tags())


@router.get("/notes/{note_id}/tasks", response_model=NoteTasksResponse)
def get_tasks_for_note(note_id: str):
    return NoteTasksResponse(tasks=_service().list_note_tasks(note_id))


@router.get("/{task_id}", response_model=TaskReadResponse)
def get_task(task_id: str):
    result = _service().read_task_detail(task_id)
    if result is None:
        raise HTTPException(404, "Task not found.")
    task, project, notes, links, subtasks = result
    return TaskReadResponse(task=task, project=project, notes=notes, links=links, subtasks=subtasks)


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(task_id: str, payload: TaskUpdate):
    try:
        task = _service().update_task(task_id, payload)
    except TasksValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    if task is None:
        raise HTTPException(404, "Task not found.")
    return TaskResponse(task=task)


@router.post("/{task_id}/status", response_model=TaskResponse)
def set_task_status(task_id: str, payload: StatusRequest):
    try:
        task = _service().set_status(task_id, payload.status)
    except TasksValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    if task is None:
        raise HTTPException(404, "Task not found.")
    return TaskResponse(task=task)


@router.post("/{task_id}/pin", response_model=TaskResponse)
def pin_task(task_id: str, payload: PinRequest):
    task = _service().set_pinned(task_id, payload.pinned)
    if task is None:
        raise HTTPException(404, "Task not found.")
    return TaskResponse(task=task)


@router.post("/{task_id}/archive", response_model=TaskResponse)
def archive_task(task_id: str, payload: ArchiveRequest):
    task = _service().set_archived(task_id, payload.archived)
    if task is None:
        raise HTTPException(404, "Task not found.")
    return TaskResponse(task=task)


@router.delete("/{task_id}")
def delete_task(task_id: str):
    if not _service().soft_delete(task_id):
        raise HTTPException(404, "Task not found.")
    return {"deleted": True}


@router.post("/{task_id}/notes")
def attach_note(task_id: str, payload: AttachNoteRequest):
    if not _service().attach_note(task_id, payload.note_id):
        raise HTTPException(404, "Task or note not found.")
    return {"ok": True}


@router.delete("/{task_id}/notes/{note_id}")
def detach_note(task_id: str, note_id: str):
    if not _service().detach_note(task_id, note_id):
        raise HTTPException(404, "Task not found.")
    return {"ok": True}


@router.get("/{task_id}/notes", response_model=TaskNotesResponse)
def list_task_notes(task_id: str):
    try:
        return TaskNotesResponse(notes=_service().list_task_notes(task_id))
    except TasksValidationError as exc:
        raise HTTPException(404, str(exc)) from exc

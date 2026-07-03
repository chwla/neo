from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.notes.types import NoteListItem

TaskStatus = Literal["todo", "doing", "blocked", "done", "archived"]
TaskPriority = Literal["low", "medium", "high", "critical"]


class Task(BaseModel):
    id: str
    title: str
    description: str = ""
    status: TaskStatus = "todo"
    priority: TaskPriority = "medium"
    due_at: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    archived: bool = False
    deleted: bool = False
    completed_at: str | None = None
    created_at: str
    updated_at: str
    subtask_count: int = 0
    open_subtask_count: int = 0


class TaskListItem(Task):
    preview: str = ""
    project_title: str | None = None
    linked_notes_count: int = 0


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    status: TaskStatus = "todo"
    priority: TaskPriority = "medium"
    due_at: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_at: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    tags: list[str] | None = None
    completed_at: str | None = None


class TaskTag(BaseModel):
    tag: str
    count: int


class TaskLink(BaseModel):
    id: str
    task_id: str
    link_type: str
    target_id: str | None = None
    target_url: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class TaskNote(NoteListItem):
    attached_at: str = ""

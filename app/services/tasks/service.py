from __future__ import annotations

import re
import uuid
from datetime import date, datetime

import app.services.projects.store as projects_store
import app.services.tasks.store as store
from app.services.projects.types import Project
from app.services.tasks.types import Task, TaskCreate, TaskLink, TaskListItem, TaskNote, TaskTag, TaskUpdate

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 50_000
MAX_TAG_LENGTH = 40
ALLOWED_STATUSES = {"todo", "doing", "blocked", "done", "archived"}
ALLOWED_PRIORITIES = {"low", "medium", "high", "critical"}


class TasksValidationError(ValueError):
    pass


class TasksService:
    def create_task(self, payload: TaskCreate) -> Task:
        status = _validate_status(payload.status)
        project_id = _validate_project(payload.project_id)
        now = store.now_iso()
        task = {
            "id": str(uuid.uuid4()),
            "title": _clean_title(payload.title),
            "description": _clean_description(payload.description),
            "status": status,
            "priority": _validate_priority(payload.priority),
            "due_at": _validate_due_at(payload.due_at),
            "project_id": project_id,
            "tags": _normalize_tags(payload.tags),
            "pinned": False,
            "archived": status == "archived",
            "deleted": False,
            "completed_at": now if status == "done" else None,
            "created_at": now,
            "updated_at": now,
        }
        return Task(**store.insert_task(task))

    def get_task(self, task_id: str) -> Task | None:
        task = store.get_task(task_id)
        return Task(**task) if task else None

    def read_task(self, task_id: str) -> tuple[Task, Project | None, list[TaskNote], list[TaskLink]] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        project = projects_store.get_project(task.project_id) if task.project_id else None
        notes = self.list_task_notes(task_id)
        links = store.list_links(task_id) or []
        return task, Project(**project) if project else None, notes, [TaskLink(**link) for link in links]

    def list_tasks(self, **filters) -> tuple[list[TaskListItem], int]:
        status = _validate_status(filters.get("status")) if filters.get("status") else None
        priority = _validate_priority(filters.get("priority")) if filters.get("priority") else None
        project_id = _validate_project(filters.get("project_id")) if filters.get("project_id") else None
        tag = _normalize_tag(filters.get("tag", "")) or None
        tasks, total = store.list_tasks(
            q=(filters.get("q") or "").strip() or None,
            status=status,
            priority=priority,
            project_id=project_id,
            tag=tag,
            due_before=_validate_due_at(filters.get("due_before")),
            due_after=_validate_due_at(filters.get("due_after")),
            include_archived=bool(filters.get("include_archived", False)),
            include_done=bool(filters.get("include_done", True)),
            pinned_first=bool(filters.get("pinned_first", True)),
            limit=max(1, min(int(filters.get("limit", 50)), 100)),
            offset=max(0, int(filters.get("offset", 0))),
        )
        return [TaskListItem(**{**task, "preview": _preview(task["description"])}) for task in tasks], total

    def update_task(self, task_id: str, payload: TaskUpdate) -> Task | None:
        existing = store.get_task(task_id)
        if existing is None:
            return None
        fields_set = payload.model_fields_set if hasattr(payload, "model_fields_set") else payload.__fields_set__
        updates: dict = {}
        if payload.title is not None:
            updates["title"] = _clean_title(payload.title)
        if payload.description is not None:
            updates["description"] = _clean_description(payload.description)
        if payload.priority is not None:
            updates["priority"] = _validate_priority(payload.priority)
        if "due_at" in fields_set:
            updates["due_at"] = _validate_due_at(payload.due_at)
        if "project_id" in fields_set:
            updates["project_id"] = _validate_project(payload.project_id)
        if payload.tags is not None:
            updates["tags"] = _normalize_tags(payload.tags)
        if payload.status is not None:
            status = _validate_status(payload.status)
            updates["status"] = status
            if status == "archived":
                updates["archived"] = True
            if status == "done":
                updates["completed_at"] = existing.get("completed_at") or store.now_iso()
            elif "completed_at" not in fields_set:
                updates["completed_at"] = None
        if "completed_at" in fields_set:
            updates["completed_at"] = _validate_due_at(payload.completed_at)
        if not updates:
            return Task(**existing)
        updated = store.update_task(task_id, updates)
        return Task(**updated) if updated else None

    def set_status(self, task_id: str, status: str) -> Task | None:
        return self.update_task(task_id, TaskUpdate(status=_validate_status(status)))

    def set_pinned(self, task_id: str, pinned: bool) -> Task | None:
        task = store.update_task(task_id, {"pinned": pinned})
        return Task(**task) if task else None

    def set_archived(self, task_id: str, archived: bool) -> Task | None:
        task = store.update_task(task_id, {"archived": archived})
        return Task(**task) if task else None

    def soft_delete(self, task_id: str) -> bool:
        return store.update_task(task_id, {"deleted": True}) is not None

    def list_tags(self) -> list[TaskTag]:
        return [TaskTag(**row) for row in store.list_tags()]

    def attach_note(self, task_id: str, note_id: str) -> bool:
        return store.attach_note(task_id, note_id)

    def detach_note(self, task_id: str, note_id: str) -> bool:
        return store.detach_note(task_id, note_id)

    def list_task_notes(self, task_id: str) -> list[TaskNote]:
        notes = store.list_task_notes(task_id)
        if notes is None:
            raise TasksValidationError("Task not found.")
        return [TaskNote(**{**note, "preview": _preview(note["body"])}) for note in notes]

    def list_note_tasks(self, note_id: str) -> list[TaskListItem]:
        tasks = store.list_note_tasks(note_id)
        return [TaskListItem(**{**task, "preview": _preview(task["description"])}) for task in tasks]


class TaskContextService:
    def answer_for_prompt(self, prompt: str) -> str | None:
        lowered = prompt.lower()
        if not _asks_task_read_question(lowered):
            return None
        tasks = self._tasks_for_prompt(lowered)
        if not tasks:
            return "No matching tasks found."
        if re.search(r"\bblocked\b", lowered):
            heading = "Blocked tasks:"
        elif re.search(r"\b(finish(?:ed)?|completed?|done)\b", lowered):
            heading = "Recently finished tasks:"
        elif re.search(r"\bwhat should i work on next\b", lowered):
            heading = "Work on next:"
        else:
            heading = "Open tasks:"
        lines = [heading]
        for task in tasks[:10]:
            details = [task.status, task.priority]
            if task.due_at:
                details.append(f"due {task.due_at}")
            if task.project_title:
                details.append(f"project {task.project_title}")
            lines.append(f"- {task.title} ({', '.join(details)})")
        return "\n".join(lines)

    def context_for_prompt(self, prompt: str) -> str:
        lowered = prompt.lower()
        if not _looks_task_related(lowered):
            return "No task context loaded."
        tasks = self._tasks_for_prompt(lowered)
        if not tasks:
            return "Task context loaded: no matching tasks."
        lines = ["Relevant tasks:"]
        for task in tasks[:10]:
            details = [task.status, task.priority]
            if task.due_at:
                details.append(f"due {task.due_at}")
            if task.project_title:
                details.append(f"project {task.project_title}")
            line = f"- {task.title} ({', '.join(details)})"
            if task.description:
                line += f": {_preview(task.description)[:220]}"
            lines.append(line)
        return "\n".join(lines)

    def _tasks_for_prompt(self, lowered: str) -> list[TaskListItem]:
        filters: dict = {"limit": 10}
        if re.search(r"\bblocked\b", lowered):
            filters["status"] = "blocked"
        elif re.search(r"\b(finish(?:ed)?|completed?|done)\b", lowered):
            filters["status"] = "done"
        else:
            filters["include_done"] = False
        matches = projects_store.context_candidates(lowered, limit=1)
        if matches:
            filters["project_id"] = matches[0]["id"]
        tasks, _ = TasksService().list_tasks(**filters)
        return tasks


def _clean_title(value: str | None) -> str:
    title = " ".join((value or "").split())
    if not title:
        raise TasksValidationError("Task title is required.")
    if len(title) > MAX_TITLE_LENGTH:
        raise TasksValidationError("Task title is too long.")
    return title


def _clean_description(value: str | None) -> str:
    description = (value or "").strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise TasksValidationError("Task description is too long.")
    return description


def _validate_status(value: str) -> str:
    if value not in ALLOWED_STATUSES:
        raise TasksValidationError("Invalid task status.")
    return value


def _validate_priority(value: str) -> str:
    if value not in ALLOWED_PRIORITIES:
        raise TasksValidationError("Invalid task priority.")
    return value


def _validate_due_at(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            return date.fromisoformat(cleaned).isoformat()
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).isoformat()
    except ValueError as exc:
        raise TasksValidationError("due_at must be a valid ISO date or datetime.") from exc


def _validate_project(project_id: str | None) -> str | None:
    if project_id is None or not project_id.strip():
        return None
    if projects_store.get_project(project_id) is None:
        raise TasksValidationError("Project not found.")
    return project_id


def _normalize_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if len(raw.strip()) > MAX_TAG_LENGTH:
            raise TasksValidationError("Task tag is too long.")
        tag = _normalize_tag(raw)
        if tag and tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    return cleaned


def _normalize_tag(tag: str) -> str:
    return re.sub(r"\s+", "-", (tag or "").strip().lower())


def _preview(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:180]


def _looks_task_related(prompt_lower: str) -> bool:
    return bool(re.search(r"\b(task|tasks|todo|to-do|blocked|work on next|finish(?:ed)? recently|completed? recently)\b", prompt_lower))


def _asks_task_read_question(prompt_lower: str) -> bool:
    return bool(
        re.search(
            r"\b(what should i work on next|what (?:task|tasks|is blocked|did i finish)|"
            r"tasks? (?:are|is) open|show (?:me )?(?:my )?tasks|list (?:my )?tasks|"
            r"what did i (?:finish|complete))\b",
            prompt_lower,
        )
    )

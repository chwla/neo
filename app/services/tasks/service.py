from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone

import app.services.projects.store as projects_store
import app.services.tasks.store as store
from app.services.chat_intent import is_internal_chat_command, resolve_internal_chat_intent
from app.services.projects.types import Project
from app.services.tasks.types import (
    Task,
    TaskCreate,
    TaskLink,
    TaskListItem,
    TaskNote,
    TaskTag,
    TaskUpdate,
)

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
            "parent_task_id": _validate_parent_task(payload.parent_task_id),
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

    def read_task(
        self, task_id: str
    ) -> tuple[Task, Project | None, list[TaskNote], list[TaskLink]] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        project = projects_store.get_project(task.project_id) if task.project_id else None
        notes = self.list_task_notes(task_id)
        links = store.list_links(task_id) or []
        return (
            task,
            Project(**project) if project else None,
            notes,
            [TaskLink(**link) for link in links],
        )

    def read_task_detail(
        self, task_id: str
    ) -> tuple[Task, Project | None, list[TaskNote], list[TaskLink], list[TaskListItem]] | None:
        result = self.read_task(task_id)
        if result is None:
            return None
        task, project, notes, links = result
        return task, project, notes, links, self.list_subtasks(task_id)

    def list_subtasks(self, parent_task_id: str) -> list[TaskListItem]:
        rows = store.list_subtasks(parent_task_id)
        if rows is None:
            raise TasksValidationError("Task not found.")
        return [TaskListItem(**{**task, "preview": _preview(task["description"])}) for task in rows]

    def list_tasks(self, **filters) -> tuple[list[TaskListItem], int]:
        status = _validate_status(filters.get("status")) if filters.get("status") else None
        priority = _validate_priority(filters.get("priority")) if filters.get("priority") else None
        project_id = (
            _validate_project(filters.get("project_id")) if filters.get("project_id") else None
        )
        tag = _normalize_tag(filters.get("tag", "")) or None
        tasks, total = store.list_tasks(
            q=(filters.get("q") or "").strip() or None,
            status=status,
            priority=priority,
            project_id=project_id,
            parent_task_id=(filters.get("parent_task_id") or "").strip() or None,
            tag=tag,
            due_before=_validate_due_at(filters.get("due_before")),
            due_after=_validate_due_at(filters.get("due_after")),
            include_archived=bool(filters.get("include_archived", False)),
            include_done=bool(filters.get("include_done", True)),
            pinned_first=bool(filters.get("pinned_first", True)),
            sort_mode=filters.get("sort_mode"),
            limit=max(1, min(int(filters.get("limit", 50)), 100)),
            offset=max(0, int(filters.get("offset", 0))),
        )
        return [
            TaskListItem(**{**task, "preview": _preview(task["description"])}) for task in tasks
        ], total

    def update_task(self, task_id: str, payload: TaskUpdate) -> Task | None:
        existing = store.get_task(task_id)
        if existing is None:
            return None
        fields_set = (
            payload.model_fields_set
            if hasattr(payload, "model_fields_set")
            else payload.__fields_set__
        )
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
        if "parent_task_id" in fields_set:
            parent_task_id = _validate_parent_task(payload.parent_task_id)
            if parent_task_id == task_id:
                raise TasksValidationError("A task cannot be its own parent.")
            updates["parent_task_id"] = parent_task_id
        if payload.tags is not None:
            updates["tags"] = _normalize_tags(payload.tags)
        if payload.status is not None:
            status = _validate_status(payload.status)
            updates["status"] = status
            if status == "archived":
                updates["archived"] = True
            elif existing.get("status") == "archived":
                updates["archived"] = False
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
        existing = store.get_task(task_id)
        if existing is None:
            return None
        updates: dict = {"archived": archived}
        if not archived and existing.get("status") == "archived":
            updates.update({"status": "todo", "completed_at": None})
        task = store.update_task(task_id, updates)
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
        return [
            TaskListItem(**{**task, "preview": _preview(task["description"])}) for task in tasks
        ]


class TaskContextService:
    def answer_for_prompt(self, prompt: str) -> str | None:
        if not is_internal_chat_command(prompt, "tasks"):
            return None
        command = resolve_internal_chat_intent(prompt)
        if command is not None and command.action == "operation":
            if re.match(r"^\s*(?:please\s+)?create\s+(?:a\s+)?task\b", prompt, re.I):
                return (
                    "Open Tasks and choose New Task to review the title, priority, and any "
                    "linked project before creating it. Chat does not create tasks implicitly."
                )
        lowered = prompt.lower()
        intent = _task_query_intent(lowered)
        if intent is None:
            return None
        tasks = self._tasks_for_prompt(lowered, intent)
        if not tasks:
            return "I found no stored tasks matching that request."
        if intent == "next":
            return _format_next_tasks(tasks)
        heading = {
            "blocked": "Blocked tasks:",
            "completed": "Recently completed tasks:",
            "due": "Tasks due soon:",
            "critical": "Critical tasks:",
        }.get(intent, "Open tasks:")
        return "\n".join(
            [
                "Based on your stored tasks:",
                heading,
                *[_format_task_line(task, intent) for task in tasks[:10]],
            ]
        )

    def context_for_prompt(self, prompt: str) -> str:
        lowered = prompt.lower()
        if not _looks_task_related(lowered):
            return "No task context loaded."
        intent = _task_query_intent(lowered) or "open"
        tasks = self._tasks_for_prompt(lowered, intent)
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

    def _tasks_for_prompt(self, lowered: str, intent: str) -> list[TaskListItem]:
        filters: dict = {"limit": 10}
        if intent == "blocked":
            filters["status"] = "blocked"
        elif intent == "completed":
            filters["status"] = "done"
            filters["sort_mode"] = "completed_recent"
        elif intent == "due":
            filters["include_done"] = False
            filters["due_before"] = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
            filters["sort_mode"] = "due_soon"
        elif intent == "critical":
            filters["priority"] = "critical"
            filters["include_done"] = False
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


def _validate_parent_task(parent_task_id: str | None) -> str | None:
    if parent_task_id is None or not parent_task_id.strip():
        return None
    if store.get_task(parent_task_id) is None:
        raise TasksValidationError("Parent task not found.")
    return parent_task_id


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
    return _task_query_intent(prompt_lower) is not None


def _task_query_intent(prompt_lower: str) -> str | None:
    text = re.sub(r"\s+", " ", prompt_lower.strip())
    if re.search(
        r"\b(python|javascript|typescript|asyncio|code|function|class|thread|process|sql|algorithm)\b",
        text,
    ) and not re.search(r"\b(my|stored|project)\b", text):
        return None
    if re.search(
        r"\b(what|which|show|list)\b.{0,30}\b(blocked tasks?|tasks? (?:are )?blocked)\b", text
    ) or re.fullmatch(r"what(?: all)? is blocked(?:(?: right)? now| for .+)?[?!.]*", text):
        return "blocked"
    if re.search(
        r"\b(what|which|show|list)\b.{0,35}\b(finish(?:ed)?|complete(?:d)?|done)\b", text
    ) or re.search(r"\b(recently completed|completed recently|finished recently)\b", text):
        return "completed"
    if re.search(
        r"\b(what|which|show|list)\b.{0,30}\b(due soon|tasks? due|upcoming tasks?)\b", text
    ) or re.fullmatch(r"what(?: all)? is due(?: soon)?[?!.]*", text):
        return "due"
    if re.search(
        r"\b(show|list|what|which)\b.{0,30}\b(critical|urgent|highest priority) tasks?\b", text
    ):
        return "critical"
    if re.search(
        r"\b(what should i (?:work on|do|focus on)|what to work on|prioriti[sz]e)\b.{0,20}\b(next|today|now)\b",
        text,
    ):
        return "next"
    if re.search(r"\b(tasks?|to-?dos?)\b.{0,30}\b(open|pending|active)\b", text) or re.search(
        r"\b(open|pending|active) tasks?\b", text
    ):
        return "open"
    if re.search(r"\b(show|list|find|get|what|which)\b.{0,40}\btasks?\b", text):
        return "open"
    return None


def _format_task_line(task: TaskListItem, intent: str) -> str:
    details: list[str] = []
    if task.project_title:
        details.append(task.project_title)
    details.append(task.priority)
    if task.due_at:
        details.append(f"due {task.due_at}")
    if intent == "completed" and task.completed_at:
        details.append(f"completed {task.completed_at}")
    line = f"- {task.title} — {', '.join(details)}"
    if intent == "blocked" and task.description:
        line += f" — {task.description[:220]}"
    return line


def _format_next_tasks(tasks: list[TaskListItem]) -> str:
    lines = ["Based on your stored tasks:"]
    doing = [task for task in tasks if task.status == "doing"]
    blocked = [task for task in tasks if task.status == "blocked"]
    actionable = [task for task in tasks if task.status != "blocked"]
    if doing:
        lines.extend(["Doing:", *[_format_task_line(task, "next") for task in doing[:4]]])
    if blocked:
        lines.extend(["Blocked:", *[_format_task_line(task, "blocked") for task in blocked[:4]]])
    if actionable:
        best = actionable[0]
        lines.extend(["Best next task:", _format_task_line(best, "next")])
    remaining = [
        task
        for task in tasks
        if task not in doing and task not in blocked and task not in actionable[:1]
    ]
    if remaining:
        lines.extend(
            ["Other open tasks:", *[_format_task_line(task, "next") for task in remaining[:5]]]
        )
    return "\n".join(lines)

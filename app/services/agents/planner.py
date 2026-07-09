from __future__ import annotations

import json
import re
from typing import Callable

import app.services.projects.store as projects_store
from app.services.agents.service import AgentsService, AgentsValidationError
from app.services.agents.types import (
    AgentRunCreate,
    AgentTaskPlan,
    PlanTasksRequest,
    PlanTasksResult,
    PlannedTask,
    RunFromObjectiveRequest,
)
from app.services.llm import LLMMessage, get_llm_client
from app.services.tasks import Task, TaskCreate, TasksService


class AgentPlannerValidationError(ValueError):
    pass


class AgentTaskPlanner:
    def __init__(self, llm_factory: Callable | None = None) -> None:
        self.llm_factory = llm_factory or (
            lambda: get_llm_client(
                num_predict=900, timeout=120, route_name="agent"
            )
        )
        self._cache: dict[tuple[str, str | None], AgentTaskPlan] = {}

    def plan(self, objective: str, project_id: str | None = None) -> AgentTaskPlan:
        objective = _validate_objective(objective)
        project = _resolve_project(objective, project_id)
        cache_key = (objective, project["id"] if project else None)
        if cache_key in self._cache:
            return self._cache[cache_key].model_copy(deep=True)
        try:
            raw = self._model_plan(objective, project)
            title = _clean_title(objective)
            subtasks = _clean_subtasks(raw.get("subtasks"), project["id"] if project else None)
            risks = _clean_list(raw.get("risks"))
            assumptions = _clean_list(raw.get("assumptions"))
        except Exception:
            title = _clean_title(objective)
            subtasks = _fallback_subtasks(objective, project["id"] if project else None)
            risks = ["Requirements may need refinement before implementation."]
            assumptions = [
                "The objective will be completed through bounded, task-linked assisted runs."
            ]
        summary = "\n".join(f"{item.order}. {item.title}" for item in subtasks)
        parent = PlannedTask(
            title=title,
            description=(
                f"{objective}\n\nGenerated task plan:\n{summary}\n\n"
                "Agent run guidance: report what was done and recommend the next subtask."
            ),
            status="doing",
            priority="medium",
            project_id=project["id"] if project else None,
            tags=["agent", "auto-created"],
        )
        plan = AgentTaskPlan(
            objective=objective,
            project_id=parent.project_id,
            parent_task=parent,
            subtasks=subtasks,
            risks=risks,
            assumptions=assumptions,
        )
        self._cache[cache_key] = plan
        return plan.model_copy(deep=True)

    def _model_plan(self, objective: str, project: dict | None) -> dict:
        project_context = (
            f"Project: {project['title']}\nDescription: {project.get('description') or '(none)'}"
            if project
            else "No project selected or inferred."
        )
        result = self.llm_factory().chat_with_metadata(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "You are Neo Agent Task Planner v1. Decompose only the supplied objective. "
                        "Return strict JSON with parent_title, subtasks, risks, assumptions. "
                        "subtasks must contain 3 to 8 concrete ordered items with title, description, and priority. "
                        "Do not include shell, filesystem, browser, email, purchase, deletion, or Memory actions."
                    ),
                ),
                LLMMessage(role="user", content=f"Objective:\n{objective}\n\n{project_context}"),
            ],
            temperature=0.1,
            num_predict=900,
        )
        return _parse_json_object(result.content)


class AgentTaskPlanningService:
    def __init__(
        self, planner: AgentTaskPlanner | None = None, agents: AgentsService | None = None
    ) -> None:
        self.planner = planner or get_agent_task_planner()
        self.agents = agents or AgentsService()
        self.tasks = TasksService()

    def plan_tasks(self, payload: PlanTasksRequest) -> PlanTasksResult:
        plan = self.planner.plan(payload.objective, payload.project_id)
        if payload.dry_run:
            return PlanTasksResult(plan=plan, created=False, tasks=[])
        parent, subtasks = self._create_tasks(plan)
        return PlanTasksResult(plan=plan, created=True, tasks=[parent, *subtasks])

    def run_from_objective(self, payload: RunFromObjectiveRequest):
        if payload.mode != "assist":
            raise AgentPlannerValidationError("Agent Task Planner v1 supports assist mode only.")
        if not payload.auto_create_tasks:
            raise AgentPlannerValidationError(
                "auto_create_tasks must be true for an objective run."
            )
        plan = self.planner.plan(payload.objective, payload.project_id)
        parent, subtasks = self._create_tasks(plan)
        try:
            run = self.agents.create_run(
                AgentRunCreate(
                    task_id=parent.id,
                    objective=plan.objective,
                    mode=payload.mode,
                )
            )
        except AgentsValidationError as exc:
            raise AgentPlannerValidationError(str(exc)) from exc
        return run, parent, subtasks, plan

    def _create_tasks(self, plan: AgentTaskPlan) -> tuple[Task, list[Task]]:
        parent = self.tasks.create_task(
            TaskCreate(
                title=plan.parent_task.title,
                description=plan.parent_task.description,
                status="doing",
                priority=plan.parent_task.priority,
                project_id=plan.project_id,
                tags=["agent", "auto-created"],
            )
        )
        subtasks = [
            self.tasks.create_task(
                TaskCreate(
                    title=item.title,
                    description=item.description,
                    status="todo",
                    priority=item.priority,
                    project_id=plan.project_id,
                    parent_task_id=parent.id,
                    tags=["agent", "subtask"],
                )
            )
            for item in plan.subtasks
        ]
        return parent, subtasks


def _validate_objective(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    if not cleaned:
        raise AgentPlannerValidationError("Objective is required.")
    if len(cleaned) > 10_000:
        raise AgentPlannerValidationError("Objective is too long.")
    return cleaned


def _resolve_project(objective: str, project_id: str | None) -> dict | None:
    if project_id:
        project = projects_store.get_project(project_id)
        if project is None:
            raise AgentPlannerValidationError("Project not found.")
        return project
    matches = projects_store.context_candidates(objective.lower(), limit=1)
    return matches[0] if matches else None


def _parse_json_object(content: str) -> dict:
    cleaned = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.S | re.I)
    if fenced:
        cleaned = fenced.group(1)
    else:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Planner output must be an object.")
    return data


def _clean_title(value: str) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip().rstrip(".")
    if not title:
        raise ValueError("Planner title is empty.")
    return title[:160]


def _clean_subtasks(raw: object, project_id: str | None) -> list[PlannedTask]:
    if not isinstance(raw, list) or not 3 <= len(raw) <= 8:
        raise ValueError("Planner must return 3 to 8 subtasks.")
    result: list[PlannedTask] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("Invalid subtask.")
        title = _clean_title(item.get("title", ""))
        if len(title.split()) < 2:
            raise ValueError("Subtask titles must be concrete.")
        description = re.sub(
            r"\s+", " ", str(item.get("description") or f"Complete: {title}.")
        ).strip()
        priority = str(item.get("priority") or "medium").lower()
        if priority not in {"low", "medium", "high", "critical"}:
            priority = "medium"
        result.append(
            PlannedTask(
                title=title,
                description=description[:2000],
                priority=priority,
                status="todo",
                project_id=project_id,
                tags=["agent", "subtask"],
                order=index,
            )
        )
    return result


def _fallback_subtasks(objective: str, project_id: str | None) -> list[PlannedTask]:
    coding = bool(
        re.search(
            r"\b(build|implement|code|api|backend|frontend|database|ui|test|software|app)\b",
            objective,
            re.I,
        )
    )
    titles = (
        [
            "Clarify requirements and success criteria",
            "Inspect the existing implementation and constraints",
            "Design the required data and API changes",
            "Implement the core backend behavior",
            "Implement the frontend and user workflow",
            "Add regression tests and validate end to end",
            "Prepare the final implementation report",
        ]
        if coding
        else [
            "Clarify the objective and success criteria",
            "Gather the available context and constraints",
            "Design an ordered execution approach",
            "Produce the core objective deliverable",
            "Review risks dependencies and gaps",
            "Validate the result against success criteria",
            "Prepare recommendations and next steps",
        ]
    )
    return [
        PlannedTask(
            title=title,
            description=f"For the objective '{objective}', {title.lower()}.",
            priority="medium",
            status="todo",
            project_id=project_id,
            tags=["agent", "subtask"],
            order=index,
        )
        for index, title in enumerate(titles, start=1)
    ]


def _clean_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [re.sub(r"\s+", " ", str(item)).strip()[:500] for item in value if str(item).strip()][:8]


_planner: AgentTaskPlanner | None = None


def get_agent_task_planner() -> AgentTaskPlanner:
    global _planner
    if _planner is None:
        _planner = AgentTaskPlanner()
    return _planner

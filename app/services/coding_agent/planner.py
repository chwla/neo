from __future__ import annotations

from app.services.agents.planner import AgentTaskPlanningService
from app.services.agents.types import PlanTasksRequest
from app.services.tasks import TasksService


class CodingTaskPlanner:
    def resolve(self, objective: str, task_id: str | None, project_id: str | None):
        if task_id:
            task = TasksService().get_task(task_id)
            if not task:
                raise LookupError("Task not found.")
            if project_id and task.project_id != project_id:
                raise ValueError("Selected task does not belong to the selected project.")
            return task, task.project_id or project_id, []
        result = AgentTaskPlanningService().plan_tasks(
            PlanTasksRequest(objective=objective, project_id=project_id, dry_run=False)
        )
        parent = result.tasks[0]
        return parent, parent.project_id, result.tasks[1:]

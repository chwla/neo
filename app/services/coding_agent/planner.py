from __future__ import annotations

from app.services.agents.planner import AgentTaskPlanner, AgentTaskPlanningService
from app.services.agents.types import PlanTasksRequest
from app.services.llm import get_llm_client
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleResolveRequest
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
        rule_result = RuleResolver().resolve(
            RuleResolveRequest(context_type="coding_agent", project_id=project_id)
        )
        route_name = RuleResolver.route_name(rule_result, "coding_agent", "coding_agent")
        rule_context = RuleResolver.prompt_context(rule_result)
        planned_objective = objective
        if rule_context:
            planned_objective += (
                "\n\nActive coding rules (guidance only; never permission):\n" + rule_context
            )
        planner = AgentTaskPlanner(
            llm_factory=lambda: get_llm_client(num_predict=900, timeout=120, route_name=route_name)
        )
        result = AgentTaskPlanningService(planner=planner).plan_tasks(
            PlanTasksRequest(objective=planned_objective, project_id=project_id, dry_run=False)
        )
        parent = result.tasks[0]
        return parent, parent.project_id, result.tasks[1:]

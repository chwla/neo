import os
import tempfile
import unittest

from app.core.config import get_settings
from app.services.agents import AgentsService
from app.services.agents.planner import (
    AgentPlannerValidationError,
    AgentTaskPlanner,
    AgentTaskPlanningService,
)
from app.services.agents.store import initialize_agent_tables
from app.services.agents.types import AgentRunCreate, PlanTasksRequest, RunFromObjectiveRequest
from app.services.notes.store import initialize_notes_tables
from app.services.projects import ProjectCreate, ProjectsService
from app.services.projects.store import initialize_project_tables
from app.services.tasks import TaskCreate, TasksService
from app.services.tasks.store import initialize_task_tables


class FailingPlannerModel:
    def chat_with_metadata(self, *_args, **_kwargs):
        raise RuntimeError("planner model unavailable")


class PassiveRunner:
    def __init__(self):
        self.started = []

    def start(self, run_id):
        self.started.append(run_id)


class AgentTaskPlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmpdir.name}/planner.db"
        get_settings.cache_clear()
        initialize_notes_tables()
        initialize_project_tables()
        initialize_task_tables()
        initialize_agent_tables()
        self.tasks = TasksService()
        self.projects = ProjectsService()
        self.project = self.projects.create_project(ProjectCreate(title="Neo"))
        self.runner = PassiveRunner()
        self.service = AgentTaskPlanningService(
            planner=AgentTaskPlanner(llm_factory=lambda: FailingPlannerModel()),
            agents=AgentsService(runner=self.runner),
        )

    def tearDown(self):
        get_settings.cache_clear()
        os.environ.pop("NEO_DATABASE_URL", None)
        self.tmpdir.cleanup()

    def test_dry_run_returns_parent_and_three_to_eight_subtasks_without_writes(self):
        result = self.service.plan_tasks(
            PlanTasksRequest(
                objective="Build File Workspace v1",
                project_id=self.project.id,
                dry_run=True,
            )
        )
        self.assertFalse(result.created)
        self.assertEqual(result.tasks, [])
        self.assertEqual(result.plan.parent_task.status, "doing")
        self.assertEqual(result.plan.project_id, self.project.id)
        self.assertGreaterEqual(len(result.plan.subtasks), 3)
        self.assertLessEqual(len(result.plan.subtasks), 8)
        self.assertEqual(self.tasks.list_tasks(include_archived=True)[1], 0)

    def test_create_mode_persists_parent_subtasks_counts_and_filter(self):
        result = self.service.plan_tasks(
            PlanTasksRequest(
                objective="Build File Workspace v1",
                project_id=self.project.id,
                dry_run=False,
            )
        )
        parent, subtasks = result.tasks[0], result.tasks[1:]
        self.assertTrue(result.created)
        self.assertEqual(parent.status, "doing")
        self.assertEqual(parent.tags, ["agent", "auto-created"])
        self.assertTrue(all(task.parent_task_id == parent.id for task in subtasks))
        self.assertTrue(all(task.status == "todo" for task in subtasks))
        filtered, total = self.tasks.list_tasks(parent_task_id=parent.id)
        self.assertEqual(total, len(subtasks))
        self.assertEqual([item.id for item in filtered], [item.id for item in subtasks])
        detail = self.tasks.read_task_detail(parent.id)
        self.assertEqual(detail[0].subtask_count, len(subtasks))
        self.assertEqual(detail[0].open_subtask_count, len(subtasks))
        self.assertEqual(len(detail[4]), len(subtasks))

    def test_run_from_objective_creates_tasks_and_task_linked_run(self):
        run, parent, subtasks, plan = self.service.run_from_objective(
            RunFromObjectiveRequest(
                objective="Build File Workspace v1",
                project_id=self.project.id,
            )
        )
        self.assertEqual(run.task_id, parent.id)
        self.assertEqual(run.project_id, self.project.id)
        self.assertEqual(run.objective, plan.objective)
        self.assertGreaterEqual(len(subtasks), 3)
        self.assertEqual(self.runner.started, [run.id])
        initialize_task_tables()
        persisted = TasksService().read_task_detail(parent.id)
        self.assertEqual(len(persisted[4]), len(subtasks))

    def test_existing_task_run_flow_still_works(self):
        task = self.tasks.create_task(TaskCreate(title="Existing task"))
        run = AgentsService(runner=self.runner).create_run(AgentRunCreate(task_id=task.id))
        self.assertEqual(run.task_id, task.id)

    def test_empty_objective_and_unknown_project_are_rejected(self):
        with self.assertRaises(AgentPlannerValidationError):
            self.service.plan_tasks(PlanTasksRequest(objective="", dry_run=True))
        with self.assertRaises(AgentPlannerValidationError):
            self.service.plan_tasks(
                PlanTasksRequest(objective="Build feature", project_id="missing", dry_run=True)
            )


if __name__ == "__main__":
    unittest.main()

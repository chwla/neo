"""Bounded, manually-started execution engine for Agent Runner v1."""

from __future__ import annotations

import re
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

import app.services.agents.store as store
from app.services.agents.safety import runner_system_prompt, validate_plan
from app.services.code_index.service import CodeIndexService
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import ArtifactCreate
from app.services.git.service import GitContextService
from app.services.llm import LLMMessage, get_llm_client
from app.services.patches import PatchProposalRequest, PatchProposalService
from app.services.symbol_awareness.service import SymbolAwarenessService
from app.services.tasks import TasksService
from app.services.test_runner.service import TestRunnerContextService
from app.services.web_search import WebSearchService


class AgentRunner:
    def __init__(
        self, *, llm_factory: Callable | None = None, web_factory: Callable | None = None
    ) -> None:
        self.llm_factory = llm_factory or (lambda: get_llm_client(num_predict=900, timeout=180))
        self.web_factory = web_factory or WebSearchService
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="neo-agent")
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def start(self, run_id: str) -> None:
        with self._lock:
            current = self._futures.get(run_id)
            if current and not current.done():
                return
            future = self._executor.submit(self.run_sync, run_id)
            self._futures[run_id] = future
            future.add_done_callback(lambda _future: self._forget(run_id))

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._futures.pop(run_id, None)

    def run_sync(self, run_id: str) -> None:
        run = store.get_run(run_id)
        if run is None or run["status"] == "cancelled":
            return
        try:
            now = store.now_iso()
            store.update_run(
                run_id,
                {"status": "planning", "started_at": run.get("started_at") or now, "error": None},
            )
            context_text = self._read_context(run)
            if self._cancelled(run_id):
                return
            self._complete_named_step(run_id, "read_context", context_text)

            plan = validate_plan(self._build_plan(run["objective"], context_text))
            store.update_run(run_id, {"plan": plan})
            self._complete_named_step(
                run_id,
                "plan",
                "\n".join(f"{i + 1}. {item['title']}" for i, item in enumerate(plan)),
            )
            self._ensure_plan_steps(run_id, plan)
            store.update_run(run_id, {"status": "running"})

            outputs: dict[str, str] = {
                step["step_type"]: step["output_text"]
                for step in store.list_steps(run_id)
                if step["status"] == "completed" and step.get("output_text")
            }
            for step in store.list_steps(run_id):
                if step["step_type"] in {"read_context", "plan"} or step["status"] in {
                    "completed",
                    "skipped",
                    "cancelled",
                }:
                    continue
                if self._cancelled(run_id):
                    return
                if step["requires_approval"] and step.get("approval_status") != "approved":
                    store.update_step(step["id"], {"status": "waiting_approval"})
                    store.update_run(run_id, {"status": "waiting_approval"})
                    return
                started = store.now_iso()
                store.update_step(
                    step["id"], {"status": "running", "started_at": started, "error": None}
                )
                try:
                    output = self._execute_step(step["step_type"], run, context_text, outputs)
                except Exception as exc:
                    self._fail(run_id, step["id"], str(exc))
                    return
                if self._cancelled(run_id):
                    return
                outputs[step["step_type"]] = output
                store.update_step(
                    step["id"],
                    {
                        "status": "completed",
                        "output_text": output,
                        "completed_at": store.now_iso(),
                    },
                )

            final_output = outputs.get("final") or outputs.get("draft")
            if not final_output:
                raise RuntimeError("Agent run produced no final output.")
            _file_context, files_considered = WorkspaceFilesService().context_for_task(
                run["task_id"], run.get("project_id")
            )
            if files_considered:
                final_output = f"{final_output.rstrip()}\n\nFiles considered:\n" + "\n".join(
                    files_considered
                )
            if run.get("project_id"):
                index_context, index_files = CodeIndexService().context_for_project(
                    run["project_id"], run["objective"]
                )
                if index_files:
                    final_output = (
                        f"{final_output.rstrip()}\n\nCodebase index used:\n"
                        f"{index_context[:2400]}\nTarget files considered:\n"
                        + "\n".join(f"- {path}" for path in index_files)
                    )
                symbol_context, symbol_files = SymbolAwarenessService().context_for_project(
                    run["project_id"], run["objective"]
                )
                if symbol_files:
                    final_output = (
                        f"{final_output.rstrip()}\n\nSymbol awareness used:\n"
                        f"{symbol_context[:2400]}\nRelated files considered:\n"
                        + "\n".join(f"- {path}" for path in symbol_files)
                    )
            completed = store.now_iso()
            store.update_run(
                run_id,
                {
                    "status": "completed",
                    "final_output": final_output,
                    "completed_at": completed,
                    "error": None,
                },
            )
            store.insert_artifact(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "artifact_type": "final_output",
                    "title": run["title"],
                    "content": final_output,
                    "note_id": None,
                    "task_id": run["task_id"],
                    "project_id": run.get("project_id"),
                    "metadata": {"mode": run["mode"]},
                    "created_at": completed,
                }
            )
            WorkspaceFilesService().create_artifact(
                ArtifactCreate(
                    title=run["title"],
                    artifact_type="analysis",
                    content=final_output,
                    source_type="agent_run",
                    source_id=run_id,
                    task_id=run["task_id"],
                    project_id=run.get("project_id"),
                    agent_run_id=run_id,
                    metadata={"mode": run["mode"]},
                )
            )
        except Exception as exc:
            if not self._cancelled(run_id):
                store.update_run(
                    run_id,
                    {
                        "status": "failed",
                        "error": str(exc),
                        "completed_at": store.now_iso(),
                    },
                )

    def _read_context(self, run: dict) -> str:
        tasks_service = TasksService()
        result = tasks_service.read_task(run["task_id"])
        if result is None:
            raise RuntimeError("Task no longer exists.")
        task, project, notes, _links = result
        lines = [
            f"Task: {task.title}",
            f"Description: {task.description or '(none)'}",
            f"Status: {task.status}",
            f"Priority: {task.priority}",
            f"Due: {task.due_at or '(none)'}",
        ]
        if project:
            lines.extend(
                [
                    f"Project: {project.title}",
                    f"Project status: {project.status}",
                    f"Project description: {project.description or '(none)'}",
                ]
            )
        if notes:
            lines.append("Linked notes:")
            for note in notes[:5]:
                excerpt = (note.summary or note.body or "").strip().replace("\n", " ")[:1200]
                lines.append(f"- {note.title}: {excerpt}")
        file_context, files_considered = WorkspaceFilesService().context_for_task(
            task.id, project.id if project else None
        )
        if file_context:
            lines.extend(
                ["Attached workspace files:", file_context, "Files considered:", *files_considered]
            )
        if project:
            index_query = run.get("objective") or f"{task.title} {task.description or ''}"
            index_context, index_files = CodeIndexService().context_for_project(
                project.id, index_query
            )
            lines.extend(["Codebase index used:", index_context])
            if index_files:
                lines.extend(
                    ["Index target files considered:", *[f"- {item}" for item in index_files]]
                )
            symbol_context, symbol_files = SymbolAwarenessService().context_for_project(
                project.id, index_query
            )
            lines.extend(["Symbol awareness used:", symbol_context])
            if symbol_files:
                lines.extend(
                    [
                        "Symbol-related files considered:",
                        *[f"- {item}" for item in symbol_files],
                    ]
                )
        test_context = TestRunnerContextService().context_for_task(
            task.id, project.id if project else None
        )
        if test_context:
            lines.extend(["Controlled test runner context:", test_context])
        git_context = GitContextService().context_for_task(
            task.id, project.id if project else None
        )
        if git_context:
            lines.extend(["Controlled Git context (read-only):", git_context])
        subtasks = tasks_service.list_subtasks(task.id)
        if subtasks:
            lines.append("Created task plan:")
            for index, subtask in enumerate(subtasks[:8], start=1):
                lines.append(f"{index}. {subtask.title} [{subtask.status}, {subtask.priority}]")
            lines.append(f"Recommended next subtask: {subtasks[0].title}")
        recent, _ = store.list_runs(task_id=task.id, limit=6)
        previous = [item for item in recent if item["id"] != run["id"]][:5]
        if previous:
            lines.append("Recent runs:")
            for item in previous:
                lines.append(
                    f"- {item['title']} [{item['status']}]: {(item.get('final_output') or item.get('error') or '')[:300]}"
                )
        return "\n".join(lines)[:15000]

    def _build_plan(self, objective: str, context_text: str) -> list[dict]:
        plan = [
            {"title": "Understand the selected task", "type": "think", "requires_approval": False},
        ]
        if _needs_web(objective, context_text):
            plan.append(
                {
                    "title": "Gather current supporting information",
                    "type": "web_search",
                    "requires_approval": False,
                }
            )
        plan.extend(
            [
                {
                    "title": "Draft a useful task output",
                    "type": "draft",
                    "requires_approval": False,
                },
            ]
        )
        if _looks_coding_task(objective, context_text) and (
            "Attached workspace files:" in context_text
            or "Index target files considered:" in context_text
        ):
            plan.append(
                {
                    "title": "Create a reviewable patch proposal",
                    "type": "patch_proposal",
                    "requires_approval": False,
                }
            )
        plan.append({"title": "Finalize the output", "type": "final", "requires_approval": False})
        return plan

    def _ensure_plan_steps(self, run_id: str, plan: list[dict]) -> None:
        existing = store.list_steps(run_id)
        existing_types = [(item["step_index"], item["step_type"]) for item in existing]
        for offset, item in enumerate(plan, start=2):
            if (offset, item["type"]) in existing_types:
                continue
            now = store.now_iso()
            store.insert_step(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "step_index": offset,
                    "step_type": item["type"],
                    "title": item["title"],
                    "status": "pending",
                    "input": {},
                    "output_text": None,
                    "error": None,
                    "requires_approval": item["requires_approval"],
                    "approval_status": None,
                    "created_at": now,
                    "updated_at": now,
                    "started_at": None,
                    "completed_at": None,
                }
            )

    def _complete_named_step(self, run_id: str, step_type: str, output: str) -> None:
        step = next(
            (item for item in store.list_steps(run_id) if item["step_type"] == step_type), None
        )
        if step is None:
            raise RuntimeError(f"Missing {step_type} step.")
        now = store.now_iso()
        store.update_step(
            step["id"],
            {
                "status": "completed",
                "started_at": step.get("started_at") or now,
                "completed_at": now,
                "output_text": output,
            },
        )

    def _execute_step(
        self, step_type: str, run: dict, context: str, outputs: dict[str, str]
    ) -> str:
        if step_type == "think":
            return self._llm(
                run,
                f"Analyze the selected task using this context. Identify constraints, missing information, risks, and the most useful deliverable.\n\n{context}",
            )
        if step_type == "web_search":
            web = self.web_factory().build_context_forced(run["objective"])
            if web.context_text:
                return web.context_text[:9000]
            warning = (
                web.warning
                or (web.search.error if web.search else None)
                or "No reliable web evidence was found."
            )
            return f"Web Search unavailable or insufficient: {warning}"
        if step_type == "draft":
            structure = (
                "Use headings: Summary, Work done / proposed, Files or areas involved, Risks, Next steps."
                if _looks_coding_task(run["objective"], context)
                else "Use headings: Answer, Evidence / reasoning, Recommendation, Next steps."
            )
            evidence = outputs.get("web_search", "No web evidence was requested.")
            reasoning = outputs.get("think", "")
            return self._llm(
                run,
                f"Create the task deliverable. {structure}\n\nTask context:\n{context}\n\nReasoning:\n{reasoning}\n\nWeb evidence:\n{evidence}",
            )
        if step_type == "final":
            draft = outputs.get("draft", "").strip()
            if not draft:
                raise RuntimeError("Draft output is missing.")
            patch = outputs.get("patch_proposal", "").strip()
            if patch:
                return f"{draft}\n\n{patch}"
            return draft
        if step_type == "patch_proposal":
            artifact = PatchProposalService(generator=self._patch_generator).propose(
                PatchProposalRequest(
                    objective=run["objective"],
                    task_id=run["task_id"],
                    project_id=run.get("project_id"),
                    agent_run_id=run["id"],
                )
            )
            targets = artifact["metadata"].get("target_filenames", [])
            return (
                "Patch proposal created:\n"
                f"- {artifact['title']}\n"
                f"- Target files: {', '.join(targets)}\n"
                "- Review needed; this patch has not been applied."
            )
        if step_type in {"summarize", "research"}:
            return outputs.get("draft") or outputs.get("think") or "No additional output."
        raise RuntimeError(f"Step type '{step_type}' has no executable v1 behavior.")

    def _llm(self, run: dict, user_prompt: str) -> str:
        client = self.llm_factory()
        result = client.chat_with_metadata(
            [
                LLMMessage(role="system", content=runner_system_prompt()),
                LLMMessage(role="user", content=f"Objective: {run['objective']}\n\n{user_prompt}"),
            ],
            temperature=0.2,
            num_predict=900,
        )
        content = result.content.strip()
        if not content:
            raise RuntimeError("The configured model returned an empty response.")
        return content

    def _patch_generator(self, prompt: str) -> str:
        client = self.llm_factory()
        result = client.chat_with_metadata(
            [
                LLMMessage(
                    role="system",
                    content=(
                        "Create proposal-only unified diffs from supplied workspace files. "
                        "Never claim files were changed or tests were run."
                    ),
                ),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.1,
            num_predict=2400,
        )
        return result.content.strip()

    def _cancelled(self, run_id: str) -> bool:
        run = store.get_run(run_id)
        return run is None or run["status"] == "cancelled"

    def _fail(self, run_id: str, step_id: str, error: str) -> None:
        now = store.now_iso()
        store.update_step(step_id, {"status": "failed", "error": error, "completed_at": now})
        store.update_run(run_id, {"status": "failed", "error": error, "completed_at": now})


def _needs_web(objective: str, context: str) -> bool:
    return bool(
        re.search(
            r"\b(latest|current|today|recent|web|search|research|sources?|compare|market|price|release)\b",
            f"{objective} {context}",
            re.I,
        )
    )


def _looks_coding_task(objective: str, context: str) -> bool:
    return bool(
        re.search(
            r"\b(code|coding|implement|bug|api|backend|frontend|database|test|file|function|class)\b",
            f"{objective} {context}",
            re.I,
        )
    )


_runner: AgentRunner | None = None


def get_agent_runner() -> AgentRunner:
    global _runner
    if _runner is None:
        _runner = AgentRunner()
    return _runner

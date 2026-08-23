"""Command and test tools.

Both go through ``CommandSandboxService``'s propose -> approve -> execute cycle
rather than calling ``subprocess`` directly. That is deliberate even though the
agent-core permission layer has already decided the call may run: the sandbox
carries its own argv allowlist, metacharacter rejection and single-use
``claim_for_execution`` gate, and routing through it means those can only ever
tighten what the mode allowed.
"""

from __future__ import annotations

from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.agent_core.types import Evidence, ToolResult
from app.services.command_sandbox.service import CommandSandboxService
from app.services.command_sandbox.types import CommandRequest

_OUTPUT_LIMIT = 12_000


def _as_argv(value) -> list[str]:
    """Accept a list, or split a string, but never hand a shell string onward.

    Models frequently emit ``"pytest tests/ -q"`` as one string. Splitting it
    here is a convenience; the sandbox still rejects anything containing shell
    metacharacters, so this cannot become a shell injection.
    """

    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return value.split()
    raise ValueError("`command` must be a non-empty argv array.")


def _render_argv(arguments: dict) -> str:
    command = arguments.get("command")
    return " ".join(command) if isinstance(command, list) else str(command or "")


def _render(record: dict) -> str:
    stdout = (record.get("stdout_text") or "").strip()
    stderr = (record.get("stderr_text") or "").strip()
    parts = [f"exit code: {record.get('exit_code')}", f"status: {record.get('status')}"]
    if stdout:
        parts.append(f"stdout:\n{stdout[:_OUTPUT_LIMIT]}")
    if stderr:
        parts.append(f"stderr:\n{stderr[:_OUTPUT_LIMIT]}")
    if record.get("output_truncated"):
        parts.append("(output truncated)")
    return "\n\n".join(parts)


#: What to do instead, per category. A blocked command is fed back to the model as
#: a tool error, and an error that only says "no" costs a retry of the same shape;
#: one that names the allowed set and the other tool is recovered from in one step.
_RECOVERY: dict[str, str] = {
    "read_only": (
        "run_command only runs read-only inspection: pwd, ls, find, grep, rg, cat, head, "
        "tail, wc, tree. It cannot execute scripts. To run tests, lint or a build, use "
        "run_tests (pytest, python -m pytest, npm test, npm run test/build/lint, ruff "
        "check, mypy). To check a file for syntax errors, read it and inspect it instead."
    ),
    "test": (
        "run_tests allows: pytest, python -m pytest, npm test, npm run test/build/lint, "
        "ruff check, mypy. Arbitrary scripts cannot be executed."
    ),
}


def _recovery_hint(category: str) -> str:
    return _RECOVERY.get(category, "That command is not allowed by Neo's sandbox policy.")


def _execute(arguments: dict, context: ToolContext, category: str, kind: str) -> ToolResult:
    if not context.repo_id:
        raise ValueError("This action needs a repository. Attach one to the session first.")
    argv = _as_argv(arguments.get("command"))
    service = CommandSandboxService()
    request = CommandRequest(
        workspace_id=context.repo_id,
        command=argv,
        cwd=str(arguments.get("cwd") or "."),
        category=category,
        created_by="agent",
    )

    proposed = service.propose(request)
    decision = proposed.get("policy_decision") or {}
    if not decision.get("allowed"):
        reason = decision.get("reason") or "The sandbox policy does not allow that command."
        raise ValueError(f"{reason}. {_recovery_hint(category)}")

    service.approve(proposed["id"], True)
    record = service.execute(proposed["id"])

    passed = record.get("status") == "completed" and record.get("exit_code") == 0
    call_id = str(context.extras.get("call_id") or "")
    return ToolResult(
        call_id=call_id,
        name=kind,
        status="ok",
        content=_render(record),
        duration_ms=int(record.get("duration_ms") or 0),
        evidence=Evidence(
            kind="tests" if category == "test" else "command",
            tool_call_id=call_id,
            passed=passed,
            detail=f"{' '.join(argv)} → exit {record.get('exit_code')}",
        ),
    )


def run_command(arguments: dict, context: ToolContext) -> ToolResult:
    return _execute(arguments, context, "read_only", "run_command")


def run_tests(arguments: dict, context: ToolContext) -> ToolResult:
    return _execute(arguments, context, "test", "run_tests")


TOOLS = [
    AgentTool(
        name="run_command",
        description=(
            "Run a read-only inspection command inside the managed repository copy. "
            "Allowed programs: pwd, ls, find, grep, rg, cat, head, tail, wc, tree."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Argv array, e.g. ['ls', '-la', 'app'].",
                },
                "cwd": {"type": "string", "description": "Directory relative to the repo root."},
            },
            "required": ["command"],
        },
        risk="command",
        requires_repo=True,
        handler=run_command,
        summary=lambda a: (
            f"Run `{_render_argv(a)}`"
        ),
    ),
    AgentTool(
        name="run_tests",
        description=(
            "Run a test, lint or build command inside the managed repository copy. "
            "Allowed: pytest, python -m pytest, npm test, npm run test/build/lint, "
            "ruff check, mypy. "
            "Use this to verify your work; a passing run is recorded as verification evidence."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Argv array, e.g. ['pytest', 'tests/test_x.py', '-q'].",
                },
                "cwd": {"type": "string", "description": "Directory relative to the repo root."},
            },
            "required": ["command"],
        },
        risk="command",
        requires_repo=True,
        handler=run_tests,
        summary=lambda a: (
            f"Run tests: `{_render_argv(a)}`"
        ),
    ),
]

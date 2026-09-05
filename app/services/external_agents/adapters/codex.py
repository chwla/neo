"""Codex: argv, and its ``exec --json`` stream translated into Neo events.

Written against recorded output -- see ``docs/external-agents/cli-surface.md``.
Three properties of this CLI shape the code here:

* **Fresh and resumed runs take structurally different argv.** ``codex exec
  resume`` accepts neither ``--cd`` nor ``-s/--sandbox``. The working directory
  therefore comes from the process (which the runner sets regardless) and the
  sandbox from a ``-c sandbox_mode=...`` config override. Getting this wrong is
  not a soft failure: the CLI exits 2 with "unexpected argument".

* **Work arrives as ``item.started``/``item.completed`` pairs** sharing an
  ``item.id``, which maps onto Neo's ``tool.call``/``tool.result`` correlated by
  ``call_id``. Because a completion can in principle arrive without its start,
  the translator keeps the set of ids it has opened and synthesises the missing
  call rather than emitting a result the transcript has nowhere to put.

* **``agent_message`` items are not deltas.** Several arrive per turn -- the
  early ones are narration, the last is the answer -- so every one streams as a
  chunk but only the last becomes the run's final text.

Codex reports token counts and no cost. Nothing here invents one.
"""

from __future__ import annotations

from typing import Any

from app.services.agent_core import events
from app.services.external_agents.types import ExternalEvent, Invocation, InvocationContext

#: Sandbox policy by Neo mode. Codex has no interactive approval channel in
#: `exec` (`-a/--ask-for-approval` is rejected there), so `normal` and `auto`
#: both land on workspace-write. Neo shows the trace but cannot gate a call.
_SANDBOX = {"plan": "read-only", "normal": "workspace-write", "auto": "workspace-write"}

#: Item types that represent the agent doing something, rather than saying it.
_WORK_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})


def build_argv(
    binary: str,
    prompt: str,
    *,
    mode: str,
    cwd: str,
    resume_thread_id: str | None = None,
    model: str | None = None,
    unsafe: bool = False,
) -> list[str]:
    # See the note in the Claude adapter: a restrictive mode wins over an
    # unsafe flag, here too.
    unsafe = unsafe and mode == "auto"
    sandbox = "danger-full-access" if unsafe else _SANDBOX.get(mode, "workspace-write")

    if resume_thread_id:
        # No --cd and no -s on this subcommand; the sandbox has to be a config
        # override and the directory comes from the process's own cwd.
        argv = [binary, "exec", "resume", resume_thread_id, "--json"]
        argv += ["-c", f'sandbox_mode="{sandbox}"']
    else:
        argv = [binary, "exec", "--json", "--cd", cwd, "-s", sandbox]

    if model:
        argv += ["-m", model]
    if unsafe:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    argv.append(prompt)
    return argv


class Translator:
    """Turns Codex's JSONL into Neo events, correlating started/completed pairs."""

    def __init__(self) -> None:
        self._open: set[str] = set()

    def feed(self, record: dict[str, Any]) -> list[ExternalEvent]:
        kind = record.get("type")

        if kind == "thread.started":
            thread_id = record.get("thread_id")
            return [
                ExternalEvent(
                    type=events.RUN_STARTED,
                    payload={"objective": "", "executor": "codex"},
                    external_session_id=thread_id,
                    meta={"thread_id": thread_id},
                )
            ]

        if kind in {"item.started", "item.completed"}:
            return self._item(kind, record.get("item") or {})

        if kind == "turn.completed":
            usage = record.get("usage") or {}
            return [
                ExternalEvent(
                    outcome="completed",
                    meta={
                        "usage": usage,
                        # Surfaced under the names the rest of Neo counts in, so
                        # the anchor's token totals mean the same thing for an
                        # external turn as for a local one.
                        "prompt_tokens": int(usage.get("input_tokens") or 0),
                        "completion_tokens": int(usage.get("output_tokens") or 0),
                    },
                )
            ]

        if kind == "turn.failed":
            message = _message(record.get("error"))
            return [ExternalEvent(outcome="failed", error=message or "the turn failed")]

        if kind == "error":
            # A stream-level error. Recorded as a status line so the reason is
            # visible in the trace; whether the run dies is decided by the
            # turn.failed that follows, or by the exit code.
            message = _message(record) or "Codex reported an error"
            return [ExternalEvent(type=events.STATUS, payload={"content": message[:2000]})]

        return []

    def _item(self, kind: str, item: dict[str, Any]) -> list[ExternalEvent]:
        item_type = item.get("type")
        item_id = str(item.get("id") or "")

        if item_type == "agent_message":
            text = item.get("text") or ""
            if kind != "item.completed" or not text.strip():
                return []
            return [
                ExternalEvent(
                    type=events.CHUNK,
                    payload={
                        "content": text,
                        "provider_name": "codex",
                        "route_name": "external",
                    },
                    final_text=text,
                )
            ]

        if item_type == "reasoning":
            text = item.get("text") or item.get("summary") or ""
            if kind != "item.completed" or not str(text).strip():
                return []
            return [ExternalEvent(type=events.THINKING, payload={"content": str(text)})]

        if item_type == "error":
            # An item-level error is frequently a warning the run survives (a
            # model-metadata notice, for one), so it informs rather than fails.
            message = _message(item)
            return (
                [ExternalEvent(type=events.STATUS, payload={"content": message[:2000]})]
                if message and kind == "item.completed"
                else []
            )

        if item_type not in _WORK_ITEMS:
            return []

        out: list[ExternalEvent] = []
        if item_id not in self._open:
            self._open.add(item_id)
            out.append(
                ExternalEvent(
                    type=events.TOOL_CALL,
                    payload={
                        "call_id": item_id,
                        "name": item_type,
                        "arguments": _arguments(item),
                        "summary": _summarize(item),
                    },
                )
            )
        if kind == "item.completed":
            self._open.discard(item_id)
            status = str(item.get("status") or "")
            exit_code = item.get("exit_code")
            failed = status == "failed" or (isinstance(exit_code, int) and exit_code != 0)
            out.append(
                ExternalEvent(
                    type=events.TOOL_RESULT,
                    payload={
                        "call_id": item_id,
                        "status": "error" if failed else "ok",
                        "content": _output(item),
                    },
                )
            )
        return out


def translate(record: dict[str, Any]) -> list[ExternalEvent]:
    """Stateless single-record translation, for tests and one-off inspection."""

    return Translator().feed(record)


def _message(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("message") or "")
    return str(value or "")


def _arguments(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") == "command_execution":
        return {"command": item.get("command") or ""}
    if item.get("type") == "file_change":
        return {"changes": item.get("changes") or []}
    return {
        key: value
        for key, value in item.items()
        if key not in {"id", "type", "status", "aggregated_output"}
    }


def _output(item: dict[str, Any]) -> str:
    if item.get("type") == "file_change":
        changes = item.get("changes") or []
        return "\n".join(
            f"{change.get('kind', 'change')}: {change.get('path', '')}"
            for change in changes
            if isinstance(change, dict)
        )
    return str(item.get("aggregated_output") or "")


def _summarize(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type == "command_execution":
        command = " ".join(str(item.get("command") or "").split())
        return f"command: {command[:120]}"
    if item_type == "file_change":
        changes = item.get("changes") or []
        paths = [
            str(change.get("path", "")).rsplit("/", 1)[-1]
            for change in changes
            if isinstance(change, dict)
        ]
        return f"edit: {', '.join(paths)[:120]}" if paths else "edit"
    return str(item_type or "step")


def invocation(context: InvocationContext) -> Invocation:
    """This CLI's command line and stream reader for one turn.

    Codex mints its own thread id and reports it on ``thread.started``, so
    unlike Claude Code there is nothing to assign up front. A fresh translator
    per invocation because it carries the started/completed correlation state
    for exactly one run.
    """

    argv = build_argv(
        context.binary,
        context.prompt,
        mode=context.mode,
        cwd=context.cwd,
        resume_thread_id=context.resume_id,
        # Left unset so Codex uses the model in the user's own config -- Neo
        # drives their CLI, it does not second-guess how they configured it.
        model=context.model,
        unsafe=context.unsafe,
    )
    return Invocation(argv=argv, translate=Translator().feed, session_id=None)


__all__ = ["Translator", "build_argv", "invocation", "translate"]

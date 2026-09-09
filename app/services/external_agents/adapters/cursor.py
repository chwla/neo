"""Cursor: argv, and its ``stream-json`` stream translated into Neo events.

**Written against Cursor's published documentation rather than recorded output** --
the binary was not installed when this was written, and the Cursor section of
``docs/external-agents/cli-surface.md`` lists what a real run still has to confirm.
Everything here is therefore defensive in the way the other adapters need not be:
an unrecognised shape is skipped rather than assumed.

The stream is close to Claude Code's, and deliberately reads like it. Three
differences carry all the work:

* **The chat id is Cursor's to mint.** There is no ``--session-id``, so Neo records
  what ``system``/``init`` reports rather than assigning one, as it does for Codex.
* **A tool call names its tool by its *key*.** The payload is a single-entry object
  -- ``{"readToolCall": {"args": …, "result": {"success": …}}}`` -- so the tool name
  is the key, not a field. The set of keys is open-ended, which is why nothing here
  branches on particular ones.
* **There is no plan mode.** The CLI has no read-only flag at all, so a restrictive
  Neo mode cannot be honoured and must not be silently downgraded. ``build_argv``
  refuses rather than pretending, and the capability record says ``plan_mode=False``
  so a run never gets this far.
"""

from __future__ import annotations

from typing import Any

from app.services.agent_core import events
from app.services.external_agents.types import (
    ExternalAgentError,
    ExternalEvent,
    Invocation,
    InvocationContext,
)

#: Cursor's own suffix on every tool key, stripped for display: `readToolCall`
#: reads better in a transcript as `read`.
_TOOL_SUFFIX = "ToolCall"


def build_argv(
    binary: str,
    prompt: str,
    *,
    mode: str,
    resume_id: str | None = None,
    model: str | None = None,
    unsafe: bool = False,
) -> list[str]:
    """The command line for one turn.

    A plan-mode run raises rather than returning argv. Cursor has no flag that
    keeps a run off the repository, so the honest options are to refuse or to run
    something the user did not ask for -- and a mode chosen to protect a
    repository is the last place to quietly do the second.
    """

    if mode == "plan":
        raise ExternalAgentError(
            "Cursor has no plan mode -- it offers no read-only or repository-safe "
            "run. Switch this chat to Normal, or pick an engine that does."
        )

    argv = [binary, "-p", prompt, "--output-format", "stream-json"]
    if resume_id:
        argv += ["--resume", resume_id]
    if model:
        argv += ["--model", model]
    if unsafe and mode == "auto":
        # Same rule as the other adapters: an unsafe flag is only honoured by the
        # least restrictive mode, so a contradiction resolves the safe way.
        argv.append("--force")
    return argv


def _tool_name(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The tool's name and body, from the single key the call is filed under."""

    for key, body in payload.items():
        if isinstance(body, dict):
            name = key[: -len(_TOOL_SUFFIX)] if key.endswith(_TOOL_SUFFIX) else key
            return name or key, body
    return "tool", {}


def _text_blocks(record: dict[str, Any]) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text") or ""
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class Translator:
    """Turns Cursor's JSONL into Neo events, correlating started/completed pairs."""

    def __init__(self) -> None:
        self._open: set[str] = set()

    def feed(self, record: dict[str, Any]) -> list[ExternalEvent]:
        kind = record.get("type")

        if kind == "system" and record.get("subtype") == "init":
            session_id = record.get("session_id")
            return [
                ExternalEvent(
                    type=events.RUN_STARTED,
                    payload={"objective": "", "executor": "cursor"},
                    external_session_id=session_id,
                    meta={
                        "session_id": session_id,
                        "model": record.get("model"),
                        "permission_mode": record.get("permissionMode"),
                    },
                )
            ]

        if kind == "assistant":
            text = _text_blocks(record)
            if not text.strip():
                return []
            return [
                ExternalEvent(
                    type=events.CHUNK,
                    payload={
                        "content": text,
                        "provider_name": "cursor",
                        "model_name": (record.get("message") or {}).get("model"),
                        "route_name": "external",
                    },
                    final_text=text,
                )
            ]

        if kind == "tool_call":
            return self._tool_call(record)

        if kind == "result":
            failed = bool(record.get("is_error")) or record.get("subtype") != "success"
            text = record.get("result") if isinstance(record.get("result"), str) else ""
            meta = {
                "duration_ms": record.get("duration_ms"),
                "request_id": record.get("request_id"),
            }
            return [
                ExternalEvent(
                    outcome="failed" if failed else "completed",
                    final_text=text or None,
                    meta={key: value for key, value in meta.items() if value is not None},
                    error=(text or record.get("subtype") or "the run failed") if failed else None,
                )
            ]

        # A shape this adapter has not seen is not a reason to abandon a run that
        # is otherwise going fine -- doubly so here, where none of it is recorded.
        return []

    def _tool_call(self, record: dict[str, Any]) -> list[ExternalEvent]:
        call_id = str(record.get("call_id") or "")
        payload = record.get("tool_call")
        if not isinstance(payload, dict):
            return []
        name, body = _tool_name(payload)
        arguments = body.get("args") if isinstance(body.get("args"), dict) else {}

        out: list[ExternalEvent] = []
        if call_id not in self._open:
            self._open.add(call_id)
            out.append(
                ExternalEvent(
                    type=events.TOOL_CALL,
                    payload={
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                        "summary": _summarize(name, arguments),
                    },
                )
            )
        if record.get("subtype") == "completed":
            self._open.discard(call_id)
            result = body.get("result") if isinstance(body.get("result"), dict) else {}
            # Documented as a one-of: `success` carries the body, anything else is
            # the failure. Absence is treated as success, because a completed call
            # that reported no error did not fail.
            failed = "success" not in result and bool(result)
            out.append(
                ExternalEvent(
                    type=events.TOOL_RESULT,
                    payload={
                        "call_id": call_id,
                        "status": "error" if failed else "ok",
                        "content": _output(result),
                    },
                )
            )
        return out


def _output(result: dict[str, Any]) -> str:
    body = result.get("success") if isinstance(result.get("success"), dict) else result
    if not isinstance(body, dict):
        return "" if body is None else str(body)
    for key in ("content", "output", "message", "text"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _summarize(name: str, arguments: dict[str, Any]) -> str:
    """A one-line description for the trace, mirroring Neo's own tool summaries."""

    for key in ("path", "file_path", "command", "pattern", "query", "url"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}: {' '.join(value.split())[:120]}"
    return name


def translate(record: dict[str, Any]) -> list[ExternalEvent]:
    """Stateless single-record translation, for tests and one-off inspection."""

    return Translator().feed(record)


def invocation(context: InvocationContext) -> Invocation:
    """This CLI's command line and stream reader for one turn.

    Like Codex and unlike Claude Code, the conversation id is the CLI's to mint,
    so nothing is assigned up front and ``session_id`` stays None until ``init``.
    """

    argv = build_argv(
        context.binary,
        context.prompt,
        mode=context.mode,
        resume_id=context.resume_id,
        model=context.model,
        unsafe=context.unsafe,
    )
    return Invocation(argv=argv, translate=Translator().feed, session_id=None)


__all__ = ["Translator", "build_argv", "invocation", "translate"]

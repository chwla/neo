"""Antigravity: argv, and its ``stream-json`` stream translated into Neo events.

**Written against Google's published documentation rather than recorded output** --
the binary was not installed when this was written, and the Antigravity section of
``docs/external-agents/cli-surface.md`` lists what a real run still has to confirm.
So the translator skips what it does not recognise instead of assuming.

Three events carry the whole stream -- ``init``, ``step_update``, ``result`` -- and
two properties of that shape decide the code:

* **A step is identified by its index, not by an id.** ``step_update`` arrives
  repeatedly for the same ``step_index`` with ``state`` moving ``ACTIVE`` to
  ``DONE``, so the index is what correlates a tool call with its result. Neo's
  ``call_id`` is therefore synthesised from it rather than read.
* **Text and tool work share one event.** A step carrying ``text_delta`` is the
  agent talking; a step carrying ``tool_name`` is the agent doing. The same event
  type means the branch has to be on content rather than on kind.

``--print-timeout`` is not optional. It defaults to five minutes, against Neo's
hour-long step budget, so a long coding run would otherwise be killed by the CLI
itself with nothing in the transcript explaining why.
"""

from __future__ import annotations

import math
from typing import Any

from app.services.agent_core import events
from app.services.external_agents.types import ExternalEvent, Invocation, InvocationContext

#: A step that is finished. Anything else is still running.
_DONE = "DONE"


def build_argv(
    binary: str,
    prompt: str,
    *,
    mode: str,
    resume_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    unsafe: bool = False,
    timeout_seconds: int | None = None,
) -> list[str]:
    """The command line for one turn."""

    argv = [binary, "-p", prompt, "--output-format", "stream-json"]
    if resume_id:
        argv += ["--conversation", resume_id]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    # The CLI's own five-minute default would end a real coding task long before
    # Neo's budget does, so Neo's budget is passed down rather than left implied.
    if timeout_seconds and timeout_seconds > 0:
        argv += ["--print-timeout", f"{max(1, math.ceil(timeout_seconds / 60))}m"]
    if unsafe and mode == "auto":
        argv.append("--dangerously-skip-permissions")
    return argv


class Translator:
    """Turns Antigravity's JSONL into Neo events, correlating steps by index."""

    def __init__(self) -> None:
        self._open: set[str] = set()

    def feed(self, record: dict[str, Any]) -> list[ExternalEvent]:
        kind = record.get("type")

        if kind == "init":
            conversation = record.get("conversation_id")
            return [
                ExternalEvent(
                    type=events.RUN_STARTED,
                    payload={"objective": "", "executor": "antigravity"},
                    external_session_id=conversation,
                    meta={
                        "conversation_id": conversation,
                        "model": record.get("model"),
                    },
                )
            ]

        if kind == "step_update":
            return self._step(record)

        if kind == "result":
            status = str(record.get("status") or "")
            failed = bool(record.get("error")) or status.lower() not in {"", "success", "ok"}
            text = record.get("response") if isinstance(record.get("response"), str) else ""
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
            meta = {
                "usage": usage or None,
                "num_turns": record.get("num_turns"),
                "conversation_id": record.get("conversation_id"),
                # Under the names the rest of Neo counts in, so an external turn's
                # totals mean the same thing as a local one's.
                "prompt_tokens": int(usage.get("input_tokens") or 0) or None,
                "completion_tokens": int(usage.get("output_tokens") or 0) or None,
            }
            return [
                ExternalEvent(
                    outcome="failed" if failed else "completed",
                    final_text=text or None,
                    meta={key: value for key, value in meta.items() if value is not None},
                    error=(record.get("error") or status or "the run failed") if failed else None,
                )
            ]

        return []

    def _step(self, record: dict[str, Any]) -> list[ExternalEvent]:
        delta = record.get("text_delta")
        if isinstance(delta, str) and delta.strip():
            return [
                ExternalEvent(
                    type=events.CHUNK,
                    payload={
                        "content": delta,
                        "provider_name": "antigravity",
                        "route_name": "external",
                    },
                    final_text=delta,
                )
            ]

        tool = record.get("tool_name")
        if not isinstance(tool, str) or not tool.strip():
            return []

        # No id on a step, so the index is the correlation key. Prefixed rather
        # than used bare: it shares a namespace with every other engine's ids in
        # the event log, and a lone "3" there would be indistinguishable.
        call_id = f"step-{record.get('step_index')}"
        info = record.get("tool_info") if isinstance(record.get("tool_info"), dict) else {}

        out: list[ExternalEvent] = []
        if call_id not in self._open:
            self._open.add(call_id)
            out.append(
                ExternalEvent(
                    type=events.TOOL_CALL,
                    payload={
                        "call_id": call_id,
                        "name": tool,
                        "arguments": info,
                        "summary": _summarize(tool, info),
                    },
                )
            )
        if str(record.get("state") or "").upper() == _DONE:
            self._open.discard(call_id)
            out.append(
                ExternalEvent(
                    type=events.TOOL_RESULT,
                    payload={
                        "call_id": call_id,
                        "status": "error" if info.get("error") else "ok",
                        "content": _output(info),
                    },
                )
            )
        return out


def _output(info: dict[str, Any]) -> str:
    for key in ("output", "result", "content", "message"):
        value = info.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _summarize(tool: str, info: dict[str, Any]) -> str:
    for key in ("path", "file_path", "command", "query", "url"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool}: {' '.join(value.split())[:120]}"
    return tool


def translate(record: dict[str, Any]) -> list[ExternalEvent]:
    """Stateless single-record translation, for tests and one-off inspection."""

    return Translator().feed(record)


def invocation(context: InvocationContext) -> Invocation:
    """This CLI's command line and stream reader for one turn."""

    from app.core.config import get_settings

    argv = build_argv(
        context.binary,
        context.prompt,
        mode=context.mode,
        resume_id=context.resume_id,
        model=context.model,
        effort=context.effort,
        unsafe=context.unsafe,
        timeout_seconds=getattr(get_settings(), "external_agent_timeout_seconds", None),
    )
    return Invocation(argv=argv, translate=Translator().feed, session_id=None)


__all__ = ["Translator", "build_argv", "invocation", "translate"]

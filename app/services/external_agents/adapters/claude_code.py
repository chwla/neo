"""Claude Code: argv, and its ``stream-json`` stream translated into Neo events.

Written against recorded output, not documentation -- see
``docs/external-agents/cli-surface.md`` and the fixtures beside these tests. The
notable shapes:

* ``system``/``init`` opens every run and echoes the ``--session-id`` we chose,
  which is why Neo assigns the conversation id rather than scraping it. Assigning
  it means a resume is possible even if the run dies before saying anything.
* an ``assistant`` message carries a *list* of content blocks -- text, thinking
  and tool_use are siblings within one message, not separate events.
* a tool result comes back as a ``user`` message, because that is literally how
  the model sees it.
* ``result`` closes the run with the final text, cost, and token usage.
* ``rate_limit_event`` reports how much of the user's subscription window is
  spent. It is recorded as metadata and streams nothing.
"""

from __future__ import annotations

from typing import Any

from app.services.agent_core import events
from app.services.external_agents.types import ExternalEvent, Invocation, InvocationContext

#: Claude Code's own checklist tool. Its input is shaped like Neo's todo items,
#: so a run's checklist can drive the existing todo UI rather than a second one.
_TODO_TOOL = "TodoWrite"

#: Permission modes, by Neo mode. `manual` is not usable under `--print` -- there
#: is nobody to answer the prompt -- so `normal` and `auto` both map to
#: acceptEdits. That is the closest faithful mapping the CLI offers, and it is
#: recorded here rather than hidden so the difference is visible in review.
_MODES = {"plan": "plan", "normal": "acceptEdits", "auto": "acceptEdits"}


def build_argv(
    binary: str,
    prompt: str,
    *,
    mode: str,
    session_id: str,
    resume: bool,
    disabled_tools: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
    unsafe: bool = False,
) -> list[str]:
    """The command line for one turn.

    ``--verbose`` is not optional: without it ``stream-json`` emits only the
    result, and the trace Neo draws would be empty.
    """

    # Refused here as well as at the caller: an unsafe flag paired with a
    # read-only mode is a contradiction, and the safe reading of a contradiction
    # is the restrictive one.
    unsafe = unsafe and mode == "auto"
    argv = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions" if unsafe else _MODES.get(mode, "acceptEdits"),
    ]
    # Resuming names the conversation; a fresh run *assigns* it. Passing both
    # would be contradictory, and --resume is the one that carries history.
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    if disabled_tools:
        argv += ["--disallowedTools", *disabled_tools]
    if model:
        argv += ["--model", model]
    # `--effort <level>`, its own flag with its own documented levels. Absent
    # unless asked for, so `effortLevel` in the user's settings still applies.
    if effort:
        argv += ["--effort", effort]
    if unsafe:
        argv.append("--dangerously-skip-permissions")
    return argv


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _usage(record: dict[str, Any]) -> dict[str, Any]:
    usage = (record.get("message") or {}).get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("input_tokens") or 0),
        "completion_tokens": int(usage.get("output_tokens") or 0),
    }


def translate(record: dict[str, Any]) -> list[ExternalEvent]:
    """One parsed JSONL line to zero or more Neo events."""

    kind = record.get("type")

    if kind == "system" and record.get("subtype") == "init":
        return [
            ExternalEvent(
                type=events.RUN_STARTED,
                payload={"objective": "", "executor": "claude_code"},
                external_session_id=record.get("session_id"),
                meta={
                    "session_id": record.get("session_id"),
                    "model": record.get("model"),
                    "permission_mode": record.get("permissionMode"),
                    "cli_version": record.get("claude_code_version"),
                },
            )
        ]

    if kind == "rate_limit_event":
        # Metadata only: how much of the subscription window is spent is worth
        # recording, but it is not a step in the run and must not draw one.
        info = record.get("rate_limit_info") or {}
        return [ExternalEvent(meta={"rate_limit": info})] if info else []

    if kind == "assistant":
        out: list[ExternalEvent] = []
        for block in _blocks(record):
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text") or ""
                if text.strip():
                    out.append(
                        ExternalEvent(
                            type=events.CHUNK,
                            payload={
                                "content": text,
                                "provider_name": "claude_code",
                                "model_name": (record.get("message") or {}).get("model"),
                                "route_name": "external",
                                **_usage(record),
                            },
                            final_text=text,
                        )
                    )
            elif block_type == "thinking":
                # Extended thinking is often returned signed but redacted, i.e.
                # a signature with an empty body. Emitting that would render a
                # blank "thinking" panel, so only real text is forwarded.
                thinking = block.get("thinking") or ""
                if thinking.strip():
                    out.append(ExternalEvent(type=events.THINKING, payload={"content": thinking}))
            elif block_type == "tool_use":
                name = block.get("name") or "tool"
                arguments = block.get("input") or {}
                out.append(
                    ExternalEvent(
                        type=events.TOOL_CALL,
                        payload={
                            "call_id": block.get("id") or "",
                            "name": name,
                            "arguments": arguments,
                            "summary": _summarize(name, arguments),
                        },
                    )
                )
                if name == _TODO_TOOL:
                    items = _todo_items(arguments)
                    if items:
                        out.append(
                            ExternalEvent(type=events.TODO_UPDATED, payload={"items": items})
                        )
        return out

    if kind == "user":
        out = []
        for block in _blocks(record):
            if block.get("type") != "tool_result":
                continue
            # `is_error` is sometimes absent rather than false, so the truthiness
            # test is the correct one here.
            failed = bool(block.get("is_error"))
            out.append(
                ExternalEvent(
                    type=events.TOOL_RESULT,
                    payload={
                        "call_id": block.get("tool_use_id") or "",
                        "status": "error" if failed else "ok",
                        "content": _text(block.get("content")),
                    },
                )
            )
        return out

    if kind == "result":
        failed = bool(record.get("is_error")) or record.get("subtype") != "success"
        text = record.get("result") if isinstance(record.get("result"), str) else ""
        usage = record.get("usage") or {}
        meta = {
            "total_cost_usd": record.get("total_cost_usd"),
            "num_turns": record.get("num_turns"),
            "duration_ms": record.get("duration_ms"),
            "usage": usage,
            # Under the names the rest of Neo counts in, so the anchor's totals
            # mean the same thing for an external turn as for a local one.
            "prompt_tokens": int(usage.get("input_tokens") or 0) or None,
            "completion_tokens": int(usage.get("output_tokens") or 0) or None,
            "terminal_reason": record.get("terminal_reason"),
            "permission_denials": record.get("permission_denials") or [],
        }
        return [
            ExternalEvent(
                outcome="failed" if failed else "completed",
                final_text=text or None,
                meta={key: value for key, value in meta.items() if value is not None},
                error=(text or record.get("subtype") or "the run failed") if failed else None,
            )
        ]

    # An unrecognised line is not a reason to abandon a run that is otherwise
    # going fine; new event kinds get added to CLIs.
    return []


def _text(content: Any) -> str:
    """Tool result bodies arrive as a string or as a list of content blocks."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text") or ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _todo_items(arguments: dict[str, Any]) -> list[dict[str, str]]:
    """Claude Code's checklist, in the shape Neo's todo panel already renders."""

    raw = arguments.get("todos")
    if not isinstance(raw, list):
        return []
    allowed = {"pending", "in_progress", "completed"}
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("content") or entry.get("title") or "").strip()
        if not title:
            continue
        status = str(entry.get("status") or "pending")
        items.append(
            {"title": title[:200], "status": status if status in allowed else "pending"}
        )
    return items


def _summarize(name: str, arguments: dict[str, Any]) -> str:
    """A one-line description for the trace, mirroring Neo's own tool summaries."""

    for key in ("file_path", "path", "pattern", "command", "url", "query"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            compact = " ".join(value.split())
            return f"{name}: {compact[:120]}"
    return name


def invocation(context: InvocationContext) -> Invocation:
    """This CLI's command line and stream reader for one turn.

    The uniform entry point every adapter provides, so the service layer can
    dispatch through a table instead of branching on the engine. What differs
    between CLIs -- and it differs a lot -- stays behind this signature.

    Claude Code is the executor Neo *assigns* a conversation id to, so a resume
    is possible even if the run dies before announcing itself.
    """

    conversation = context.resume_id or context.new_session_id
    argv = build_argv(
        context.binary,
        context.prompt,
        mode=context.mode,
        session_id=conversation,
        resume=bool(context.resume_id),
        disabled_tools=context.disabled_tools,
        # None means "send no --model", and then whatever this CLI's own
        # configuration names still applies. Forwarded rather than dropped: the
        # field went unread here for as long as its only source was a
        # Codex-shaped environment variable, so the omission was invisible.
        model=context.model,
        effort=context.effort,
        unsafe=context.unsafe,
    )
    return Invocation(argv=argv, translate=translate, session_id=conversation)


__all__ = ["build_argv", "invocation", "translate"]
